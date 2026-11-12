"""Bounded, full-page redo records. All routines operate on locked unbuffered files."""

import hashlib
import os
import struct
import uuid
import zlib
from dataclasses import dataclass
from typing import BinaryIO

from .binary import PAGE_SIZE, U32, seal, verify
from .errors import Corruption, StorageError

MAGIC = b"MINIDB\r\n"
VERSION = 3
ANCHOR = struct.Struct(">8sHH16s")
STAMP = struct.Struct(">QI16s")  # generation, logical page count, lineage token
PREFIX = struct.Struct(">4s16s16s")
MAX_FRAMES = 4096
FRAME_SIZE = 4 + PAGE_SIZE
HEAD_SIZE = PREFIX.size + 2 * STAMP.size + 8
MARK = struct.Struct(">8sQ32s")
MAX_LOG = PAGE_SIZE + HEAD_SIZE + MAX_FRAMES * FRAME_SIZE + MARK.size


def boundary(name: str) -> None:
    """Storage-operation boundary for deterministic test instrumentation."""


def put(stream: BinaryIO, offset: int, data: bytes) -> None:
    stream.seek(offset)
    if stream.write(data) != len(data):
        raise OSError("short storage write")


def get(stream: BinaryIO, offset: int, size: int) -> bytes:
    stream.seek(offset)
    return stream.read(size)


def sync(stream: BinaryIO, phase: str) -> None:
    os.fsync(stream.fileno())
    boundary(phase)


def length(stream: BinaryIO) -> int:
    return os.fstat(stream.fileno()).st_size


def file_identity(stream: BinaryIO) -> bytes:
    st = os.fstat(stream.fileno())
    if not st.st_ino:
        raise StorageError("WAL requires a stable filesystem file identifier")
    return hashlib.sha256(f"{st.st_dev}/{st.st_ino}".encode("ascii")).digest()[:16]


@dataclass(frozen=True)
class Stamp:
    seq: int
    count: int
    token: bytes

    def encode(self) -> bytes:
        return STAMP.pack(self.seq, self.count, self.token)

    def next(self, count: int) -> "Stamp":
        return Stamp(self.seq + 1, count, uuid.uuid4().bytes)


def stamp_page(tag: bytes, stamp: Stamp) -> bytes:
    return seal(tag + stamp.encode())


def read_stamp(stream: BinaryIO, physical: int, tag: bytes) -> Stamp | None:
    try:
        body = verify(get(stream, physical * PAGE_SIZE, PAGE_SIZE))
    except Corruption:
        return None
    if body[:4] != tag or any(body[4 + STAMP.size :]):
        return None
    stamp = Stamp(*STAMP.unpack_from(body, 4))
    return stamp if stamp.count else None


def preamble(identity: bytes) -> bytes:
    return seal(b"MDBWAL01" + identity)


@dataclass
class Redo:
    base: Stamp
    target: Stamp
    pages: dict[int, bytes]

    def header(self, identity: bytes, owner: bytes) -> bytes:
        body = (
            PREFIX.pack(b"TX01", identity, owner)
            + self.base.encode()
            + self.target.encode()
            + U32.pack(len(self.pages))
        )
        return body + U32.pack(zlib.crc32(body))

    def prepare(
        self, stream: BinaryIO, identity: bytes, owner: bytes
    ) -> tuple[int, bytes]:
        if length(stream) != PAGE_SIZE:
            raise Corruption("WAL already contains a transaction")
        boundary("before_prepare")
        header = self.header(identity, owner)
        put(stream, PAGE_SIZE, header)
        digest = hashlib.sha256(header)
        offset = PAGE_SIZE + len(header)
        for page in sorted(self.pages):
            frame = U32.pack(page) + self.pages[page]
            put(stream, offset, frame)
            offset += len(frame)
            digest.update(frame)
            boundary("frame_written")
        sync(stream, "prepare_synced")
        return offset, MARK.pack(b"COMMIT01", self.target.seq, digest.digest())

    def checkpoint(self, stream: BinaryIO, *, recovering: bool = False) -> None:
        # The other stamp survives a torn stamp write. Data writes begin only
        # after the intent is durable, and completion follows durable data.
        # During redo the intent may be the only surviving lineage stamp.
        # Never rewrite that good copy while DONE could still be torn.
        if not recovering or read_stamp(stream, 2, b"INTN") != self.target:
            put(stream, 2 * PAGE_SIZE, stamp_page(b"INTN", self.target))
        sync(stream, "intent_synced")
        for page, data in self.pages.items():
            put(stream, (page + 2) * PAGE_SIZE, data)
            boundary("page_installed")
        sync(stream, "pages_synced")
        put(stream, PAGE_SIZE, stamp_page(b"DONE", self.target))
        boundary("checkpoint_header_written")
        sync(stream, "checkpoint_synced")
        if recovering:
            if (
                read_stamp(stream, 1, b"DONE") != self.target
                or read_stamp(stream, 2, b"INTN") != self.target
                or length(stream) != (self.target.count + 2) * PAGE_SIZE
            ):
                raise Corruption("recovered checkpoint metadata differs from WAL")
            for page, image in self.pages.items():
                if get(stream, (page + 2) * PAGE_SIZE, PAGE_SIZE) != image:
                    raise Corruption("recovered page differs from validated WAL image")


def inspect(
    stream: BinaryIO,
    identity: bytes,
    owner: bytes,
    done: Stamp | None,
    intent: Stamp | None,
) -> Redo | None:
    """Return committed redo or an incomplete prefix. Reject malformed full records."""
    size = length(stream)
    if not PAGE_SIZE <= size <= MAX_LOG or get(stream, 0, PAGE_SIZE) != preamble(
        identity
    ):
        raise Corruption("invalid, missing or foreign WAL preamble or length")
    n = size - PAGE_SIZE
    if n == 0:
        return None

    def available(offset: int, requested: int) -> bytes:
        amount = min(requested, max(0, size - offset))
        result = get(stream, offset, amount)
        if len(result) != amount:
            raise Corruption("unexpected EOF while reading a locked WAL")
        return result

    head = available(PAGE_SIZE, HEAD_SIZE)
    prefix = PREFIX.pack(b"TX01", identity, owner)
    if head[: len(prefix)] != prefix[: len(head)]:
        raise Corruption(
            "wrong database, file identity or transaction signature in WAL"
        )
    if len(head) < HEAD_SIZE:
        return None
    if zlib.crc32(head[:-4]) != U32.unpack(head[-4:])[0]:
        raise Corruption("WAL header checksum mismatch")
    base = Stamp(*STAMP.unpack_from(head, PREFIX.size))
    target = Stamp(*STAMP.unpack_from(head, PREFIX.size + STAMP.size))
    frames = U32.unpack(head[-8:-4])[0]
    if not (
        0 < base.count <= target.count
        and target.seq == base.seq + 1
        and base.token != target.token
        and 0 < frames <= MAX_FRAMES
        and target.count - base.count <= frames
    ):
        raise Corruption("invalid WAL generations or frame count")
    if done not in (base, target, None) or intent not in (base, target, None):
        raise Corruption("stale or divergent WAL generation")
    expected = HEAD_SIZE + frames * FRAME_SIZE + MARK.size
    if n > expected:
        raise Corruption("unexpected trailing WAL data")
    digest = hashlib.sha256(head)
    pages = {}
    last = 0
    for i in range(frames):
        frame = available(PAGE_SIZE + HEAD_SIZE + i * FRAME_SIZE, FRAME_SIZE)
        if len(frame) != FRAME_SIZE:
            return None
        page = U32.unpack_from(frame)[0]
        if not last < page < target.count:
            raise Corruption("unordered, duplicate or out-of-range WAL page")
        last = page
        image = frame[4:]
        verify(image)
        if U32.unpack_from(image, 4)[0] != page:
            raise Corruption("WAL frame identifier disagrees with page image")
        pages[page] = image
        digest.update(frame)
    if any(page not in pages for page in range(base.count, target.count)):
        raise Corruption("missing newly allocated page in WAL")
    tail = available(PAGE_SIZE + expected - MARK.size, MARK.size)
    marker = MARK.pack(b"COMMIT01", target.seq, digest.digest())
    if not marker.startswith(tail):
        raise Corruption("invalid WAL commit marker or transaction digest")
    return Redo(base, target, pages) if len(tail) == MARK.size else None


def retire(stream: BinaryIO) -> None:
    boundary("before_retire")
    stream.truncate(PAGE_SIZE)
    sync(stream, "wal_retired")
