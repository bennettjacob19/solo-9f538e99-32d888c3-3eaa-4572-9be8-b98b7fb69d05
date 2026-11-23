"""Per-stripe version counters persisted in ``versions.img``.

The archive workflow reads a stripe together with its version and later clears
the stripe *conditional on that version* (compare-and-swap).  Every committed
stripe update — a normal write, a same-value overwrite, a clear, or a write
replayed by crash recovery — advances the stripe's version by exactly one.

Layout (everything is little-endian, CRC32 protects each record)::

    header  (64 B):  magic "XORVER01", format, header size, stripe count,
                     entry size, CRC32
    per stripe:      2 slots x 32 B = 64 B
    slot  (32 B):    version u64, last_seq u64, generation u64, CRC32

The file is ``64 + stripes * 64`` bytes: it grows only with the stripe count,
never with the number of writes.  The three disk images and the journal keep
their existing formats, so volumes created before this feature open unchanged
(a missing ``versions.img`` is created with every stripe at version 0).

Crash safety mirrors the journal's design:

* each stripe owns **two** slots; an update writes the *inactive* slot with a
  higher generation and fsyncs it.  A torn slot fails its CRC at load time and
  the older slot stays authoritative, so a version never disappears;
* ``last_seq`` records the journal sequence the version bump belongs to.  The
  bump is applied *before* the journal is cleared, so recovery of a prepared
  journal record re-applies it — and the sequence check keeps that replay
  idempotent (a write interrupted after the version landed is not counted
  twice).  Stripe content and version therefore converge together: after a
  restart both are either the old or the new state.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from .errors import VolumeCorrupt

MAGIC = b"XORVER01"
FORMAT_VERSION = 1
HEADER_SIZE = 64
ENTRY_SIZE = 32
SLOTS_PER_STRIPE = 2
STRIPE_BYTES = ENTRY_SIZE * SLOTS_PER_STRIPE

_HEADER = struct.Struct("<8sHHII")  # magic, format, header_size, stripes, entry_size
_ENTRY = struct.Struct("<QQQ")  # version, last_seq, generation
_CRC = struct.Struct("<I")
assert HEADER_SIZE >= _HEADER.size + _CRC.size
assert ENTRY_SIZE >= _ENTRY.size + _CRC.size


@dataclass(frozen=True)
class Entry:
    """One durable version record for a stripe."""

    version: int
    last_seq: int
    generation: int


def _pack_header(stripes: int) -> bytes:
    body = _HEADER.pack(MAGIC, FORMAT_VERSION, HEADER_SIZE, stripes, ENTRY_SIZE)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + _CRC.pack(crc) + b"\x00" * (HEADER_SIZE - _HEADER.size - _CRC.size)


def _parse_header(raw: bytes) -> int | None:
    """Return the stripe count from a well-formed header, else ``None``."""

    if len(raw) < _HEADER.size + _CRC.size:
        return None
    try:
        magic, fmt, header_size, stripes, entry_size = _HEADER.unpack_from(raw, 0)
        (crc,) = _CRC.unpack_from(raw, _HEADER.size)
    except struct.error:
        return None
    if (
        magic != MAGIC
        or fmt != FORMAT_VERSION
        or header_size != HEADER_SIZE
        or entry_size != ENTRY_SIZE
    ):
        return None
    if crc != zlib.crc32(raw[: _HEADER.size]) & 0xFFFFFFFF:
        return None
    return stripes


def _pack_entry(entry: Entry) -> bytes:
    body = _ENTRY.pack(entry.version, entry.last_seq, entry.generation)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + _CRC.pack(crc) + b"\x00" * (ENTRY_SIZE - _ENTRY.size - _CRC.size)


def _parse_entry(raw: bytes) -> Entry | None:
    if len(raw) < _ENTRY.size + _CRC.size:
        return None
    try:
        version, last_seq, generation = _ENTRY.unpack_from(raw, 0)
        (crc,) = _CRC.unpack_from(raw, _ENTRY.size)
    except struct.error:
        return None
    if crc != zlib.crc32(raw[: _ENTRY.size]) & 0xFFFFFFFF:
        return None
    return Entry(version=version, last_seq=last_seq, generation=generation)


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        n = os.pwrite(fd, view[written:], offset + written)
        if n <= 0:  # pragma: no cover - defensive, regular files do not do this
            raise OSError("short write to versions file")
        written += n


class VersionStore:
    """Crash-safe per-stripe versions backed by ``versions.img``.

    All mutating calls are expected to be serialized by the owning array's
    lock (the write path) or to happen during single-threaded recovery.
    """

    def __init__(self, path: Path, stripes: int, *, fsync: bool = True) -> None:
        self.path = Path(path)
        self.stripes = stripes
        self.fsync = fsync
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[Entry] = []
        self._active_slot: list[int] = []
        self._open_or_init()

    # ------------------------------------------------------------------ file

    @property
    def file_bytes(self) -> int:
        return HEADER_SIZE + self.stripes * STRIPE_BYTES

    def _slot_offset(self, stripe: int, slot: int) -> int:
        return HEADER_SIZE + stripe * STRIPE_BYTES + slot * ENTRY_SIZE

    def _open_or_init(self) -> None:
        expected = self.file_bytes
        size = self.path.stat().st_size if self.path.exists() else 0
        if size == expected:
            self._load()
            return
        if size >= HEADER_SIZE:
            # A readable header with a different geometry means the file
            # belongs to another STRIPES setting; refuse rather than reset.
            with open(self.path, "rb") as handle:
                stripes = _parse_header(handle.read(HEADER_SIZE))
            if stripes is not None:
                raise VolumeCorrupt(
                    "versions file does not match the configured stripe count",
                    detail={"file_stripes": stripes, "configured_stripes": self.stripes},
                )
        # Fresh volume (or an older volume without a versions file), or a
        # file left short/garbled by a crash during its own initialization:
        # lay down a complete image.  A file that never reached full size
        # cannot hold committed versions, because the image is written in
        # full exactly once at creation.
        self._initialize()
        self._load()

    def _initialize(self) -> None:
        zero = _pack_entry(Entry(version=0, last_seq=0, generation=0))
        image = bytearray(self.file_bytes)
        header = _pack_header(self.stripes)
        image[: len(header)] = header
        for stripe in range(self.stripes):
            for slot in range(SLOTS_PER_STRIPE):
                start = self._slot_offset(stripe, slot)
                image[start : start + ENTRY_SIZE] = zero
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _pwrite_all(fd, bytes(image), 0)
            os.ftruncate(fd, self.file_bytes)
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

    def _load(self) -> None:
        fd = os.open(self.path, os.O_RDWR)
        try:
            stripes = _parse_header(os.pread(fd, HEADER_SIZE, 0))
            if stripes is None:
                raise VolumeCorrupt(
                    "versions file header is invalid",
                    detail={"path": str(self.path)},
                )
            if stripes != self.stripes:
                raise VolumeCorrupt(
                    "versions file does not match the configured stripe count",
                    detail={"file_stripes": stripes, "configured_stripes": self.stripes},
                )
            entries: list[Entry] = []
            active: list[int] = []
            for stripe in range(self.stripes):
                best: Entry | None = None
                best_slot = -1
                for slot in range(SLOTS_PER_STRIPE):
                    raw = os.pread(fd, ENTRY_SIZE, self._slot_offset(stripe, slot))
                    entry = _parse_entry(raw)
                    if entry is not None and (best is None or entry.generation > best.generation):
                        best, best_slot = entry, slot
                if best is None:
                    raise VolumeCorrupt(
                        "both version slots are unreadable for a stripe",
                        detail={"stripe": stripe, "path": str(self.path)},
                    )
                entries.append(best)
                active.append(best_slot)
        finally:
            os.close(fd)
        self._entries = entries
        self._active_slot = active

    # ------------------------------------------------------------------ API

    def version(self, stripe: int) -> int:
        return self._entries[stripe].version

    def snapshot(self) -> list[int]:
        return [entry.version for entry in self._entries]

    def apply(self, stripe: int, seq: int) -> int:
        """Record one committed stripe update and return the new version.

        Idempotent per journal sequence: replaying the recovery of a write
        whose version bump already landed (``last_seq == seq``) is a no-op, so
        an interrupted operation is counted exactly once no matter where the
        crash happened.
        """

        current = self._entries[stripe]
        if current.last_seq == seq:
            return current.version
        updated = Entry(
            version=current.version + 1,
            last_seq=seq,
            generation=current.generation + 1,
        )
        slot = (self._active_slot[stripe] + 1) % SLOTS_PER_STRIPE
        fd = os.open(self.path, os.O_RDWR)
        try:
            _pwrite_all(fd, _pack_entry(updated), self._slot_offset(stripe, slot))
            if self.fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        self._entries[stripe] = updated
        self._active_slot[stripe] = slot
        return updated.version

    def info(self) -> dict:
        """Metadata-only view used by ``GET /api/status`` (never raises)."""

        try:
            size: int | None = self.path.stat().st_size
        except OSError:
            size = None
        return {
            "path": str(self.path),
            "file_bytes": size,
            "header_bytes": HEADER_SIZE,
            "entry_bytes": ENTRY_SIZE,
            "slots_per_stripe": SLOTS_PER_STRIPE,
            "bytes_per_stripe": STRIPE_BYTES,
            "stripes": self.stripes,
            "max_bytes": self.file_bytes,
        }
