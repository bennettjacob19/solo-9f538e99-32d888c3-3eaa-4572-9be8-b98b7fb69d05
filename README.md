# 三盘 XOR 阵列掉电演练服务

一块数据盘在检修时不在位时，阵列仍能读回掉电前最后确认的内容。服务用 FastAPI
实现了一个三盘 XOR 阵列（`data0`、`data1`、`parity`），三块盘各是一个持久文件，
每条带每盘 4 KiB。`/docs` 里可以按块读写，并能模拟"数据块已落盘而校验块未落盘"或
相反方向的掉电；重启时指定任一数据盘离线，阵列进入只读降级模式。

阵列同时充当归档采集程序的暂存区：健康模式下可以按条带号一次读出两个数据块和该
条带的版本，归档完成后凭这个版本清空同一条带（见"条带版本与凭版本清空"）。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

数据保存在 compose 命名卷 `array-data` 中（三块盘镜像、`journal.img`、
`versions.img`），`docker compose down && docker compose up` 之后仍在。
默认 16 条带（每盘 64 KiB），可用环境变量调整：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `STRIPES` | `16` | 每条盘上的 4 KiB 块数 |
| `OFFLINE_DISK` | `none` | 重启时离线的数据盘：`data0` / `data1` / `none` |
| `FSYNC` | `1` | 每次块写、日志写后 fsync |

本地不用 Docker 也可以：

```bash
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --reload
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/status` | 模式、离线盘、序列号、日志状态、最近一次恢复结果 |
| `GET` | `/api/journal` | 恢复日志的占用与当前状态（只读元数据） |
| `GET` | `/api/verify` | 逐条带校验 `data0 XOR data1 XOR parity == 0` |
| `GET` | `/api/disks` | 盘列表与块大小 |
| `GET` | `/api/disks/{disk}/blocks` | 列出该盘所有块的摘要（可选 `include_data`） |
| `GET` | `/api/disks/{disk}/blocks/{index}` | 读一个 4 KiB 块（base64 + SHA-256） |
| `POST` / `PUT` | `/api/disks/{disk}/blocks/{index}` | 写一个 4 KiB 块（JSON 或 `application/octet-stream`） |
| `POST` | `/api/write` | 同上，参数全部放在 JSON body |
| `GET` | `/api/stripes/{index}` | 一次读出一个条带的两个数据块和当前版本 |
| `POST` | `/api/stripes/{index}/clear` | 凭版本清空整个条带（两个数据块 + 校验块一起清零） |
| `GET` | `/api/versions` | 所有条带的版本与版本文件的占用 |
| `GET` | `/api/disks/{disk}/blocks/{index}/raw` | 读原始字节 |
| `GET` | `/healthz` | 存活探针；模拟掉电后返回 503 |

写入和清空都支持 `power_loss` 掉电选项：

| 值 | 效果 |
| --- | --- |
| `none`（默认） | 数据块、校验块、日志全部落盘后才返回 200 |
| `data` | 两个数据块落盘、校验块保持旧值，服务停止（503） |
| `data0` / `data1` | 只有对应那一块数据块落盘、校验块保持旧值，服务停止（503） |
| `parity` | 只有校验块落盘，数据块保持旧值，服务停止（503） |
| `neither` | 只有日志意图落盘，任何磁盘块都未改动，服务停止（503） |
| `both` | 三块都落盘但日志未清除，服务停止（503） |

模拟掉电后服务进入 `crashed` 状态，所有块读写返回 503，重启进程即触发恢复。
`data`/`neither`/`both`、以及 `parity` 场景下，被中断那条带的**数据块**允许
恢复成旧值或新值；其它条带、以及同一条带里未参与本次写入的块必须保持掉电前最后
确认的值。

## 盘阵与写入路径

```
stripe i:  parity[i] = data0[i] XOR data1[i]        (偏移 i * 4096)
```

一次写入是整条带更新，受单槽重做日志保护（`app/journal.py`）：

1. 把新的 `data0`、`data1` 两个块写入 `journal.img` 并 fsync；
2. 在日志头（512 B，含 CRC32）写入 `prepared` 意图并 fsync —— 此后才允许碰盘；
3. 写三条带的对应块；
4. 日志头改回 `clear` 并 fsync，才向客户端返回 200。

记录的块数最多两个：`parity = data0 XOR data1` 是纯函数，恢复时用两个新数据块
重算即可，不需要为校验盘再存一份。因此日志固定为 `512 B + 2 × 4 KiB = 8704 B`，
与条带数无关；`GET /api/journal` 会报告 `header_bytes`、`payload_blocks`、
`max_bytes`。两个数据块各带 SHA-256，日志头带 CRC32。

恢复规则：

* 日志头完好且为 `prepared` → 用日志里的两个数据块重算整条带并写回所有在线盘，
  然后视情况清除日志。磁盘块状态不影响结果，所以任何中断点都收敛到同一内容。
* 日志头损坏（意图落盘阶段就断了）→ 说明第 3 步没有开始，所有盘保持原值；
  被中断的写入整体视为旧值，仍满足"旧值或新值"的约束。
* 日志负载校验失败或日志头状态字节非法 → 拒绝启动（`VolumeCorrupt`），不猜测数据。

## 条带版本与凭版本清空（归档采集）

采集程序的典型流程：

1. `GET /api/stripes/{index}` 一次读出该条带的 `data0`、`data1` 两个数据块和
   当前 `version`；
2. 数据归档完成后，`POST /api/stripes/{index}/clear`，body 为
   `{"version": <读到的版本>}`；
3. 版本匹配 → 同一条带的两个数据块和校验块一起清零，该条带版本更新；
   版本不匹配 → 409 `version_conflict`，原数据保留。

版本语义：

* 每次生效的写入或清空都会把该条带的版本更新为本次操作的日志序列号 ——
  包括同值覆盖（字节没变版本也会变）和掉电后由恢复重放的写入；
* 写别的条带、正常重启都不影响该条带的版本；
* 清空同样支持 `power_loss` 掉电选项。清空中断后用原卷重启，条带内容和版本
  要么都保持原样（日志意图没落盘），要么都切到清空后的状态，不会只变一半 ——
  版本在日志清除之前落盘，恢复重放时按同一序列号幂等赋值。

版本持久化在 `versions.img`：每条带一个 8 字节小端计数器，文件大小恒为
`条带数 × 8 B`，只随条带数增长，不随操作次数增长。盘镜像与日志格式不变；
旧卷没有该文件，首次启动时按全 0 创建（所有条带版本为 0）。

## 降级模式

`OFFLINE_DISK=data0|data1`（或等价别名 `0`/`1`）启动时：

* 模式为 `degraded`，所有块写入返回 409 `degraded_read_only`；
* 离线盘的读请求由 `另一块数据盘 XOR parity` 重建，响应里 `source` 为
  `reconstructed`；离线盘的物理文件**完全不会被打开**（可以把文件删掉或写坏来验证）；
* `verify` 报告 `checked: false`，因为无法对三盘做 XOR 校验；
* 若启动时日志里有 `prepared` 记录，恢复会把日志内容提交到**所有在线盘**（含校验盘），
  但保留日志不清理，这样之后不带离线参数重启还能把离线盘补齐。同一离线盘连续重启
  的结果逐字节一致（幂等）。

之后以 `OFFLINE_DISK=none` 重启，恢复会补齐所有盘并清除日志。若某块盘的文件确实
缺失（换盘场景）而服务以健康模式启动，缺失的盘会由另外两块重建，`/api/status` 的
`rebuilt_disks` 会列出它。

## 错误格式

统一为 `{"error": {"code": ..., "message": ..., ...}}`：

| HTTP | code |
| --- | --- |
| 404 | `block_out_of_range` |
| 409 | `degraded_read_only` / `version_conflict` |
| 422 | `invalid_configuration` / `invalid_block_payload` |
| 503 | `simulated_power_loss` / `service_crashed` |
| 500 | `volume_corrupt` |

## 测试与演练

```bash
pip install -r requirements-dev.txt
python -m pytest          # 单元/集成测试
python scripts/drill.py   # 真实 uvicorn 子进程的四阶段演练
```

`scripts/drill.py` 会：健康写入 → 模拟"数据块已落盘、校验块未落盘"掉电并把进程
SIGKILL → 指定 `data1` 离线重启两次（比对两次逻辑视图逐字节一致、只读、
离线盘读到重建值）→ 恢复健康重启并校验 → 演练采集流程（同值覆盖抬版本、
`power_loss=parity` 的清空中断、恢复后版本与内容一起到位、旧版本清空被拒）。
它自己创建临时数据目录，不会碰 `./data`。

测试覆盖：健康读写与 XOR 校验、六种掉电场景的恢复收敛、降级只读与重建读、
连续两次降级重启幂等、日志头撕裂、日志负载损坏拒启、日志占用上限、
缺盘重建、并发写后逐条带校验，以及版本化条带：凭版本清空、409 保留原数据、
同值覆盖抬版本、清空中断后内容与版本一起收敛、版本文件只随条带数增长、
旧卷（无 `versions.img`）兼容启动。
