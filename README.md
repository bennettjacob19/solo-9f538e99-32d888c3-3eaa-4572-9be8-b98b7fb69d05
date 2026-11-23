# 三盘 XOR 阵列掉电演练服务

一块数据盘在检修时不在位时，阵列仍能读回掉电前最后确认的内容。服务用 FastAPI
实现了一个三盘 XOR 阵列（`data0`、`data1`、`parity`），三块盘各是一个持久文件，
每条带每盘 4 KiB。`/docs` 里可以按块读写，并能模拟"数据块已落盘而校验块未落盘"或
相反方向的掉电；重启时指定任一数据盘离线，阵列进入只读降级模式。

阵列同时充当采集程序的暂存区：每条带带一个单调递增的**版本号**。归档程序按条带号
一次读出两个数据块和该条带的版本，归档完成后凭这个版本清空同一条带（乐观并发：
版本不匹配返回 409，数据原样保留）。版本记录放在独立的 `versions.img` 里，空间只随
条带数增长，盘镜像和日志格式与旧卷完全一致。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

数据保存在 compose 命名卷 `array-data` 中，`docker compose down && docker compose up`
之后仍在。默认 16 条带（每盘 64 KiB），可用环境变量调整：

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
| `GET` | `/api/status` | 模式、离线盘、序列号、日志与版本文件状态、最近一次恢复结果 |
| `GET` | `/api/journal` | 恢复日志的占用与当前状态（只读元数据） |
| `GET` | `/api/verify` | 逐条带校验 `data0 XOR data1 XOR parity == 0` |
| `GET` | `/api/disks` | 盘列表与块大小 |
| `GET` | `/api/disks/{disk}/blocks` | 列出该盘所有块的摘要（可选 `include_data`） |
| `GET` | `/api/disks/{disk}/blocks/{index}` | 读一个 4 KiB 块（base64 + SHA-256） |
| `POST` / `PUT` | `/api/disks/{disk}/blocks/{index}` | 写一个 4 KiB 块（JSON 或 `application/octet-stream`） |
| `POST` | `/api/write` | 同上，参数全部放在 JSON body |
| `GET` | `/api/stripes` | 每条带的版本号与占用情况 |
| `GET` | `/api/stripes/{index}` | 一次读出该条带两个数据块 + 版本号 |
| `POST` | `/api/stripes/{index}/clear` | 凭 `expected_version` 清空整条带（CAS） |
| `GET` | `/api/disks/{disk}/blocks/{index}/raw` | 读原始字节 |
| `GET` | `/healthz` | 存活探针；模拟掉电后返回 503 |

写入的 `power_loss` 取值：

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

## 条带版本与归档流程

采集程序把待归档的数据暂存在阵列里，归档程序按下面的流程搬运：

1. `GET /api/stripes/{index}` 一次读出该条带的两个数据块和版本号（`version`）；
2. 数据归档完成后，`POST /api/stripes/{index}/clear`，body 带上读到的版本：
   `{"expected_version": 3}`；
3. 版本仍匹配 → 两个数据块和校验块一起清零，返回 200 和新版本号；
   版本不匹配（期间有别的写入或清空生效）→ 返回 409 `version_conflict`，
   数据原样保留，归档程序应重新读取再决定。

版本语义：

* 每次**生效**的写入或清空把该条带版本 +1 —— 包括同值覆盖，也包括掉电后
  由恢复流程重放的写入（重放只计一次）；
* 写别的条带、正常重启都不影响该条带的版本；版本随 `versions.img` 持久化；
* 清空也是一次条带写入（内容全零），走同一条日志路径，因此 `power_loss`
  选项同样可以用来演练"清空中断"。用原卷重启后，条带内容和版本要么都保持
  原样，要么都切到清空后的状态 —— 恢复把内容和版本一起补齐，不会只更新一边；
* 清空在降级模式下同样被拒绝（409 `degraded_read_only`）。

版本记录放在独立的 `versions.img`（`app/versions.py`）：64 字节文件头 +
每条带 2 个 32 字节槽位，总共 `64 + 条带数 × 64` 字节，只随条带数增长。
每条带双槽位交替写入，槽内带 CRC32 和世代号，写槽撕裂时旧槽仍然有效；
槽里还记录该版本归属的日志序列号，使恢复重放幂等。盘镜像
（`data0/1/parity.img`）和日志（`journal.img`）的格式完全不变，旧卷打开时
会自动补建一个全零版本的 `versions.img`。

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
离线盘读到重建值）→ 恢复健康重启并校验 → 演练归档流程（版本化读取、
过期版本清空被拒、用 `power_loss=both` 中断清空）→ 验证重启后条带内容与版本
一起收敛到清空态、同值覆盖仍推进版本、版本跨重启保持。它自己创建临时数据目录，
不会碰 `./data`。

测试覆盖：健康读写与 XOR 校验、六种掉电场景的恢复收敛、降级只读与重建读、
连续两次降级重启幂等、日志头撕裂、日志负载损坏拒启、日志占用上限、
缺盘重建、并发写后逐条带校验；以及条带版本：读取返回两个数据块与版本、
同值覆盖推进版本、版本不匹配清空返回 409 且数据保留、六种掉电场景下
清空中断的原子恢复、降级重启中恢复幂等、掉电重放只推进一次版本、
旧卷无版本文件时从零开始、版本文件只随条带数增长、版本槽撕裂回退。
