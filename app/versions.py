"""Per-stripe version counters persisted in ``versions.img``.

The archival collector stages data in the array and later clears a stripe *by
version* (compare-and-clear).  Every confirmed stripe mutation — a normal
write, a same-value overwrite, a clear, or a write replayed by crash
recovery — stores the mutation's journal sequence number as the stripe's
version, so the version changes on every effective mutation and never
otherwise.

Layout: a flat array of ``stripes`` little-endian uint64 counters, so the
file is exactly ``stripes * 8`` bytes — it grows only with the stripe count,
never with the number of operations.  The disk images and the journal keep
their existing formats; this file is the only on-disk addition, and volumes
created before versioning simply start with every version at 0.

Crash atomicity: the version write is ordered *after* the stripe blocks and
*before* the journal is cleared.  Recovery replays the journaled record by
re-writing the blocks and re-assigning the same version (an idempotent
assignment, not an increment), so after any interruption the stripe's content
and version are either both the old state or both the new state.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from .errors import VolumeCorrupt

ENTRY = struct.Struct("<Q")
VERSION_BYTES = ENTRY.size  # 8


class VersionStore:
    """Fixed-size ``stripes * 8`` byte file of per-stripe versions."""

    def __init__(self, path: Path, stripes: int, *, fsync: bool = True) -> None:
        self.path = Path(path)
        self.stripes = stripes
        self.fsync = fsync
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._versions = self._open_or_create()

    @property
    def size_bytes(self) -> int:
        return self.stripes * VERSION_BYTES

    def _open_or_create(self) -> list[int]:
        expected = self.size_bytes
        if not self.path.exists():
            # A pre-versioning volume: every stripe starts at version 0.
            versions = [0] * self.stripes
            self._write_all(versions)
            return versions
        size = self.path.stat().st_size
        if size != expected:
            raise VolumeCorrupt(
                f"version file {self.path.name} has an unexpected size",
                detail={"expected_bytes": expected, "actual_bytes": size},
            )
        with open(self.path, "rb") as handle:
            raw = handle.read()
        if len(raw) != expected:
            raise VolumeCorrupt(
                f"could not read version file {self.path.name}",
                detail={"expected_bytes": expected, "actual_bytes": len(raw)},
            )
        return [ENTRY.unpack_from(raw, i * VERSION_BYTES)[0] for i in range(self.stripes)]

    def _write_all(self, versions: list[int]) -> None:
        payload = b"".join(ENTRY.pack(v) for v in versions)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                n = os.pwrite(fd, view[written:], written)
                if n <= 0:  # pragma: no cover - defensive, regular files do not do this
                    raise OSError("short write to version file")
                written += n
            if self.fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        if self.fsync:
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def get(self, index: int) -> int:
        return self._versions[index]

    def all(self) -> list[int]:
        return list(self._versions)

    def set(self, index: int, version: int) -> None:
        """Assign (not increment) the stripe's version and make it durable."""

        payload = ENTRY.pack(version)
        fd = os.open(self.path, os.O_RDWR)
        try:
            n = os.pwrite(fd, payload, index * VERSION_BYTES)
            if n != len(payload):  # pragma: no cover - defensive
                raise OSError("short write to version file")
            if self.fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        self._versions[index] = version
