from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.journal import HEADER_SIZE

from conftest import block, make_settings, read_bytes, write


def read_stripe(client: TestClient, index: int) -> dict:
    response = client.get(f"/api/stripes/{index}")
    assert response.status_code == 200, response.text
    return response.json()


def clear(client: TestClient, index: int, version: int, **kwargs):
    return client.post(
        f"/api/stripes/{index}/clear", json={"version": version, **kwargs}
    )


def versions(client: TestClient) -> list[int]:
    response = client.get("/api/versions")
    assert response.status_code == 200, response.text
    return response.json()["versions"]


def test_stripe_read_returns_both_data_blocks_and_version(client):
    assert write(client, 2, "data0", block(0x41)).status_code == 200
    assert write(client, 2, "data1", block(0x42)).status_code == 200

    result = read_stripe(client, 2)
    assert result["index"] == 2
    assert base64.b64decode(result["data0"]["data_b64"]) == block(0x41)
    assert base64.b64decode(result["data1"]["data_b64"]) == block(0x42)
    assert result["data0"]["source"] == "native"
    assert result["version"] == 2  # two confirmed writes, sequence starts at 1
    assert result["consistent"] is True


def test_clear_with_matching_version_zeroes_all_three_blocks(client):
    write(client, 1, "data0", block(0x51))
    write(client, 1, "data1", block(0x52))
    write(client, 2, "data0", block(0x33))  # a neighbour that must survive
    version = read_stripe(client, 1)["version"]

    response = clear(client, 1, version)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cleared"] is True
    assert body["previous_version"] == version
    assert body["version"] > version
    assert body["journal_cleared"] is True

    for disk in ("data0", "data1", "parity"):
        assert read_bytes(client, 1, disk) == block(0)
    assert read_stripe(client, 1)["version"] == body["version"]
    assert client.get("/api/verify").json()["consistent"] is True
    assert read_bytes(client, 2, "data0") == block(0x33)


def test_clear_with_stale_version_returns_409_and_keeps_data(client):
    write(client, 0, "data0", block(0x61))
    stale = read_stripe(client, 0)["version"]
    write(client, 0, "data1", block(0x62))  # someone else touches the stripe

    response = clear(client, 0, stale)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "version_conflict"
    assert error["expected_version"] == stale
    assert error["current_version"] == stale + 1

    # the rejected clear must not have changed anything
    assert read_bytes(client, 0, "data0") == block(0x61)
    assert read_bytes(client, 0, "data1") == block(0x62)
    assert read_stripe(client, 0)["version"] == stale + 1
    assert client.get("/api/status").json()["journal"]["state"] == "clear"


def test_same_value_overwrite_still_bumps_version(client):
    write(client, 3, "data0", block(0x77))
    first = read_stripe(client, 3)["version"]

    again = write(client, 3, "data0", block(0x77))
    assert again.status_code == 200
    second = read_stripe(client, 3)["version"]
    assert second > first
    assert read_bytes(client, 3, "data0") == block(0x77)

    # the older version can no longer clear the stripe
    assert clear(client, 3, first).status_code == 409


def test_clearing_an_empty_stripe_still_bumps_version(client):
    assert read_stripe(client, 1)["version"] == 0
    response = clear(client, 1, 0)
    assert response.status_code == 200, response.text
    assert read_stripe(client, 1)["version"] == 1
    assert read_bytes(client, 1, "data0") == block(0)


def test_other_stripes_and_clean_restart_leave_version_alone(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 1, "data0", block(0x21))
        target = read_stripe(c, 1)["version"]
        for index in (0, 2, 3):
            write(c, index, "data1", block(0x30 + index))
        assert read_stripe(c, 1)["version"] == target
        before = versions(c)

    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert read_stripe(c, 1)["version"] == target
        assert versions(c) == before
        assert read_bytes(c, 1, "data0") == block(0x21)


@pytest.mark.parametrize(
    "scenario", ["data", "data0", "data1", "parity", "neither", "both"]
)
def test_interrupted_clear_recovers_content_and_version_together(tmp_path, scenario):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 1, "data0", block(0x5A))
        write(c, 1, "data1", block(0x5B))
        write(c, 2, "data0", block(0x33))  # a neighbour that must survive
        version = read_stripe(c, 1)["version"]

    with TestClient(create_app(make_settings(tmp_path))) as c:
        response = clear(c, 1, version, power_loss=scenario)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "simulated_power_loss"
        assert response.json()["error"]["operation"] == "clear"
        clear_seq = response.json()["error"]["sequence"]

    with TestClient(create_app(make_settings(tmp_path))) as c:
        # content and version both switched to the cleared state
        for disk in ("data0", "data1", "parity"):
            assert read_bytes(c, 1, disk) == block(0)
        new_version = read_stripe(c, 1)["version"]
        assert new_version == clear_seq
        assert new_version > version
        assert c.get("/api/verify").json()["consistent"] is True
        assert read_bytes(c, 2, "data0") == block(0x33)
        # the stale version can no longer clear
        assert clear(c, 1, version).status_code == 409

    # a clean restart changes neither content nor version
    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert read_stripe(c, 1)["version"] == new_version
        for disk in ("data0", "data1", "parity"):
            assert read_bytes(c, 1, disk) == block(0)


def test_torn_journal_during_clear_keeps_old_content_and_version(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 0, "data0", block(0x66))
        version = read_stripe(c, 0)["version"]

    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert clear(c, 0, version, power_loss="neither").status_code == 503

    # Tear the intent away: the clear never became durable, so both the
    # content and the version must stay at their pre-clear state.
    with open(tmp_path / "journal.img", "r+b") as handle:
        handle.write(b"\x00" * HEADER_SIZE)

    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert read_bytes(c, 0, "data0") == block(0x66)
        assert read_stripe(c, 0)["version"] == version
        # and the version token is still valid for a later clear
        assert clear(c, 0, version).status_code == 200
        assert read_bytes(c, 0, "data0") == block(0)


def test_replayed_write_after_power_loss_bumps_version(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 2, "data0", block(0x10))
        version = read_stripe(c, 2)["version"]

    with TestClient(create_app(make_settings(tmp_path))) as c:
        crashed = write(c, 2, "data0", block(0x99), power_loss="data")
        assert crashed.status_code == 503

    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert read_bytes(c, 2, "data0") == block(0x99)
        assert read_stripe(c, 2)["version"] == version + 1
        status = c.get("/api/status").json()
        assert status["last_recovery"]["version"] == version + 1


def test_interrupted_clear_with_offline_disk_recovers_consistently(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 1, "data0", block(0x6B))
        version = read_stripe(c, 1)["version"]

    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert clear(c, 1, version, power_loss="data").status_code == 503

    # Degraded restarts keep the journal but still converge the logical view
    # and the version; two consecutive starts are byte-identical.
    for _ in range(2):
        with TestClient(create_app(make_settings(tmp_path, offline="data0"))) as c:
            assert read_bytes(c, 1, "data0") == block(0)  # reconstructed
            assert read_bytes(c, 1, "data1") == block(0)
            assert read_stripe(c, 1)["version"] == version + 1
            assert clear(c, 1, version + 1).status_code == 409  # read-only

    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert read_bytes(c, 1, "data0") == block(0)
        assert read_stripe(c, 1)["version"] == version + 1
        assert c.get("/api/verify").json()["consistent"] is True


def test_version_file_grows_only_with_stripe_count(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, stripes=4))) as c:
        for index in range(4):
            write(c, index, "data0", block(index + 1))
            clear(c, index, read_stripe(c, index)["version"])
        info = c.get("/api/versions").json()
        assert info["bytes_per_stripe"] == 8
        assert info["file_bytes"] == 4 * 8
        assert info["versions"] == [2, 4, 6, 8]

    path = tmp_path / "versions.img"
    assert path.stat().st_size == 4 * 8

    # Many more operations on the same volume must not grow the file.
    with TestClient(create_app(make_settings(tmp_path))) as c:
        for i in range(20):
            write(c, i % 4, "data1", block(i))
    assert path.stat().st_size == 4 * 8


def test_volume_created_before_versioning_starts_with_zero_versions(tmp_path):
    # Simulate a pre-versioning volume: disk images + journal, no versions.img.
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 1, "data0", block(0x44))
    (tmp_path / "versions.img").unlink()

    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert read_bytes(c, 1, "data0") == block(0x44)
        assert read_stripe(c, 1)["version"] == 0
        # a clear keyed on that read works and starts the version counter
        assert clear(c, 1, 0).status_code == 200
        assert read_bytes(c, 1, "data0") == block(0)
        assert read_stripe(c, 1)["version"] > 0


def test_clear_in_degraded_mode_is_read_only(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 0, "data0", block(0x31))

    with TestClient(create_app(make_settings(tmp_path, offline="data0"))) as c:
        version = read_stripe(c, 0)["version"]  # reads still work degraded
        response = clear(c, 0, version)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "degraded_read_only"
        assert read_bytes(c, 0, "data0") == block(0x31)
        assert read_stripe(c, 0)["version"] == version


def test_versions_endpoint_lists_all_stripes(client):
    write(client, 0, "data0", block(1))
    write(client, 2, "data1", block(2))

    info = client.get("/api/versions").json()
    assert info["stripes"] == 4
    assert info["versions"] == [1, 0, 2, 0]
    assert info["file_bytes"] == 4 * 8


def test_docs_and_openapi_expose_stripe_endpoints(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/stripes/{index}" in paths
    assert "get" in paths["/api/stripes/{index}"]
    assert "/api/stripes/{index}/clear" in paths
    assert "post" in paths["/api/stripes/{index}/clear"]
    assert "/api/versions" in paths


def test_clear_rejects_bad_payload(client):
    missing = client.post("/api/stripes/0/clear", json={})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_block_payload"

    negative = client.post("/api/stripes/0/clear", json={"version": -1})
    assert negative.status_code == 422

    unknown = client.post(
        "/api/stripes/0/clear", json={"version": 0, "power_loss": "bogus"}
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_configuration"


def test_clear_out_of_range_index(client):
    response = client.post("/api/stripes/99/clear", json={"version": 0})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "block_out_of_range"
