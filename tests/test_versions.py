from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.errors import VolumeCorrupt
from app.versions import ENTRY_SIZE, HEADER_SIZE, STRIPE_BYTES, VersionStore

from conftest import block, make_settings, read_bytes, write


def read_stripe(client: TestClient, index: int) -> dict:
    response = client.get(f"/api/stripes/{index}")
    assert response.status_code == 200, response.text
    return response.json()


def version_of(client: TestClient, index: int) -> int:
    return read_stripe(client, index)["version"]


def clear(client: TestClient, index: int, expected_version: int, **kwargs):
    return client.post(
        f"/api/stripes/{index}/clear",
        json={"expected_version": expected_version, **kwargs},
    )


def test_stripe_read_returns_both_blocks_and_version(client):
    a, b = block(0x41), block(0x42)
    assert write(client, 2, "data0", a).status_code == 200
    assert write(client, 2, "data1", b).status_code == 200

    stripe = read_stripe(client, 2)
    assert stripe["version"] == 2
    assert stripe["zero"] is False
    assert stripe["consistent"] is True
    assert base64.b64decode(stripe["data0"]["data_b64"]) == a
    assert base64.b64decode(stripe["data1"]["data_b64"]) == b
    assert stripe["data0"]["source"] == "native"

    untouched = read_stripe(client, 1)
    assert untouched["version"] == 0
    assert untouched["zero"] is True


def test_every_effective_write_bumps_version_including_same_value(client):
    payload = block(0x5A)
    first = write(client, 0, "data0", payload).json()
    assert first["version"] == 1
    # a same-value overwrite is still an effective write
    second = write(client, 0, "data0", payload).json()
    assert second["version"] == 2
    assert version_of(client, 0) == 2
    # writing the sibling data block of the same stripe bumps it too
    third = write(client, 0, "data1", block(1)).json()
    assert third["version"] == 3


def test_other_stripes_and_restart_leave_version_untouched(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 0, "data0", block(1))
        write(c, 1, "data0", block(2))
        write(c, 1, "data1", block(3))
        assert version_of(c, 0) == 1
        assert version_of(c, 1) == 2

    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert version_of(c, 0) == 1
        assert version_of(c, 1) == 2
        write(c, 2, "data0", block(4))
        assert version_of(c, 0) == 1
        assert version_of(c, 1) == 2
        assert version_of(c, 2) == 1


def test_clear_with_matching_version_zeroes_the_whole_stripe(client):
    write(client, 1, "data0", block(0x11))
    write(client, 1, "data1", block(0x22))
    version = version_of(client, 1)

    response = clear(client, 1, version)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cleared"] is True
    assert body["previous_version"] == version
    assert body["version"] == version + 1
    assert body["journal_cleared"] is True

    for disk in ("data0", "data1", "parity"):
        assert read_bytes(client, 1, disk) == block(0)
    stripe = read_stripe(client, 1)
    assert stripe["zero"] is True
    assert stripe["version"] == version + 1
    assert client.get("/api/verify").json()["consistent"] is True


def test_clear_with_stale_version_returns_409_and_keeps_data(client):
    write(client, 1, "data0", block(0x77))
    current = version_of(client, 1)

    refused = clear(client, 1, current + 1)
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "version_conflict"
    assert error["expected_version"] == current + 1
    assert error["current_version"] == current

    # data and version are untouched by the rejected clear
    assert read_bytes(client, 1, "data0") == block(0x77)
    assert version_of(client, 1) == current
    assert client.get("/api/verify").json()["consistent"] is True

    # the correct version still clears
    assert clear(client, 1, current).status_code == 200


def test_double_clear_with_the_same_version_fails_the_second_time(client):
    write(client, 0, "data0", block(1))
    assert clear(client, 0, 1).status_code == 200
    again = clear(client, 0, 1)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "version_conflict"
    assert version_of(client, 0) == 2


def test_clear_on_an_already_zero_stripe_still_bumps_version(client):
    assert version_of(client, 3) == 0
    response = clear(client, 3, 0)
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert version_of(client, 3) == 1


def test_clear_payload_validation(client):
    missing = client.post("/api/stripes/0/clear", json={})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_block_payload"

    negative = client.post("/api/stripes/0/clear", json={"expected_version": -1})
    assert negative.status_code == 422

    out_of_range = client.post("/api/stripes/99/clear", json={"expected_version": 0})
    assert out_of_range.status_code == 404
    assert out_of_range.json()["error"]["code"] == "block_out_of_range"


def test_clear_requires_healthy_mode(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 0, "data0", block(1))
    with TestClient(create_app(make_settings(tmp_path, offline="data0"))) as c:
        response = clear(c, 0, 1)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "degraded_read_only"
        # the stripe read still works in degraded mode (reconstructed)
        stripe = read_stripe(c, 0)
        assert stripe["version"] == 1
        assert stripe["data0"]["source"] == "reconstructed"
        assert base64.b64decode(stripe["data0"]["data_b64"]) == block(1)


def test_replayed_write_bumps_version_exactly_once(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 0, "data0", block(0x10))
        assert version_of(c, 0) == 1
    with TestClient(create_app(make_settings(tmp_path))) as c:
        crashed = write(c, 0, "data0", block(0x20), power_loss="data")
        assert crashed.status_code == 503
    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert read_bytes(c, 0, "data0") == block(0x20)
        assert version_of(c, 0) == 2  # not 3: the replay is counted once
        status = c.get("/api/status").json()
        assert status["last_recovery"]["version"] == 2


@pytest.mark.parametrize(
    "scenario", ["data", "data0", "data1", "parity", "neither", "both"]
)
def test_interrupted_clear_recovers_atomically(tmp_path, scenario):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 1, "data0", block(0x31))
        write(c, 1, "data1", block(0x32))
        write(c, 2, "data0", block(0x77))
        before = {
            (disk, index): read_bytes(c, index, disk)
            for index in range(4)
            for disk in ("data0", "data1", "parity")
        }
        versions_before = [version_of(c, i) for i in range(4)]

    with TestClient(create_app(make_settings(tmp_path))) as c:
        response = clear(c, 1, versions_before[1], power_loss=scenario)
        assert response.status_code == 503
        assert response.json()["error"]["operation"] == "clear"
        # the crashed service refuses further stripe reads until restarted
        assert c.get("/api/stripes/1").status_code == 503

    with TestClient(create_app(make_settings(tmp_path))) as c:
        # content and version converged together to the cleared state
        for disk in ("data0", "data1", "parity"):
            assert read_bytes(c, 1, disk) == block(0)
        assert version_of(c, 1) == versions_before[1] + 1
        # every other stripe kept both its content and its version
        for index in (0, 2, 3):
            assert version_of(c, index) == versions_before[index]
            for disk in ("data0", "data1", "parity"):
                assert read_bytes(c, index, disk) == before[(disk, index)]
        assert c.get("/api/verify").json()["consistent"] is True
        assert c.get("/api/status").json()["journal"]["state"] == "clear"
        # an archiver retrying with the pre-clear version is rejected
        assert clear(c, 1, versions_before[1]).status_code == 409


def test_interrupted_clear_recovers_through_degraded_restarts(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 1, "data0", block(0x31))
        write(c, 1, "data1", block(0x32))
    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert clear(c, 1, 2, power_loss="data").status_code == 503

    for _ in range(2):
        with TestClient(create_app(make_settings(tmp_path, offline="data1"))) as c:
            stripe = read_stripe(c, 1)
            assert stripe["zero"] is True
            assert stripe["version"] == 3
            assert stripe["data1"]["source"] == "reconstructed"
            assert c.get("/api/status").json()["journal"]["state"] == "prepared"

    with TestClient(create_app(make_settings(tmp_path))) as c:
        stripe = read_stripe(c, 1)
        assert stripe["zero"] is True
        assert stripe["version"] == 3  # bumped exactly once across all replays
        assert c.get("/api/status").json()["journal"]["state"] == "clear"
        assert c.get("/api/verify").json()["consistent"] is True


def test_old_volume_without_versions_file_starts_at_zero(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        write(c, 0, "data0", block(0x10))
        write(c, 0, "data1", block(0x20))
        expected0 = read_bytes(c, 0, "data0")
        expected1 = read_bytes(c, 0, "data1")

    # simulate a volume created before stripe versions existed
    (tmp_path / "versions.img").unlink()

    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert read_bytes(c, 0, "data0") == expected0
        assert read_bytes(c, 0, "data1") == expected1
        assert version_of(c, 0) == 0  # versions start fresh, data is intact
        assert c.get("/api/verify").json()["consistent"] is True
        write(c, 0, "data0", block(0x30))
        assert version_of(c, 0) == 1


def test_versions_file_grows_only_with_stripe_count(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        for index in range(4):
            write(c, index, "data0", block(index + 1))
        info = c.get("/api/status").json()["versions"]
    path = tmp_path / "versions.img"
    size = path.stat().st_size
    assert size == HEADER_SIZE + 4 * STRIPE_BYTES
    assert info["file_bytes"] == size
    assert info["max_bytes"] == size
    assert info["bytes_per_stripe"] == STRIPE_BYTES

    # further writes and clears never grow the file
    with TestClient(create_app(make_settings(tmp_path))) as c:
        for index in range(4):
            write(c, index, "data1", block(index + 2))
            assert clear(c, index, 2).status_code == 200
    assert path.stat().st_size == size

    # a larger array gets a proportionally larger file, nothing more
    other = tmp_path / "other"
    with TestClient(create_app(make_settings(other, stripes=9))) as c:
        c.get("/api/status")
    assert (other / "versions.img").stat().st_size == HEADER_SIZE + 9 * STRIPE_BYTES


def test_stripe_listing_reports_versions_and_occupancy(client):
    write(client, 0, "data0", block(9))
    write(client, 2, "data1", block(8))
    listing = client.get("/api/stripes").json()
    assert listing["count"] == 4
    by_index = {stripe["index"]: stripe for stripe in listing["stripes"]}
    assert by_index[0]["version"] == 1 and by_index[0]["zero"] is False
    assert by_index[1]["version"] == 0 and by_index[1]["zero"] is True
    assert by_index[2]["version"] == 1 and by_index[2]["zero"] is False


def test_openapi_exposes_stripe_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/stripes" in paths
    assert "/api/stripes/{index}" in paths
    assert "/api/stripes/{index}/clear" in paths


# ----------------------------------------------------------- VersionStore unit


def test_version_store_is_idempotent_per_sequence(tmp_path):
    store = VersionStore(tmp_path / "versions.img", 4, fsync=False)
    assert store.apply(0, seq=1) == 1
    assert store.apply(0, seq=1) == 1  # replay of the same journal record
    assert store.apply(0, seq=2) == 2
    assert store.version(0) == 2
    assert store.version(1) == 0


def test_version_store_persists_across_reopen(tmp_path):
    path = tmp_path / "versions.img"
    store = VersionStore(path, 4, fsync=False)
    store.apply(1, seq=3)
    store.apply(1, seq=4)
    store.apply(3, seq=5)
    reloaded = VersionStore(path, 4, fsync=False)
    assert reloaded.snapshot() == [0, 2, 0, 1]


def test_version_store_survives_a_torn_slot_write(tmp_path):
    path = tmp_path / "versions.img"
    store = VersionStore(path, 4, fsync=False)
    assert store.apply(2, seq=1) == 1  # generation 1 lands in slot 1

    # simulate a torn write of generation 2 into slot 0
    with open(path, "r+b") as handle:
        handle.seek(HEADER_SIZE + 2 * STRIPE_BYTES)
        handle.write(b"\xff" * ENTRY_SIZE)

    reloaded = VersionStore(path, 4, fsync=False)
    assert reloaded.version(2) == 1  # torn slot rejected, older slot wins
    # recovery can re-apply the interrupted bump
    assert reloaded.apply(2, seq=2) == 2
    assert VersionStore(path, 4, fsync=False).version(2) == 2


def test_version_store_refuses_foreign_geometry_and_dead_stripes(tmp_path):
    path = tmp_path / "versions.img"
    VersionStore(path, 4, fsync=False)
    with pytest.raises(VolumeCorrupt):
        VersionStore(path, 5, fsync=False)  # belongs to another stripe count

    # corrupt both slots of stripe 1
    with open(path, "r+b") as handle:
        handle.seek(HEADER_SIZE + 1 * STRIPE_BYTES)
        handle.write(b"\xff" * (2 * ENTRY_SIZE))
    with pytest.raises(VolumeCorrupt):
        VersionStore(path, 4, fsync=False)
