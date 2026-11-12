"""Big-endian primitives and the common page checksum trailer."""

import struct
import zlib

from .errors import Corruption

PAGE_SIZE = 4096
BODY_SIZE = PAGE_SIZE - 4
U8 = struct.Struct(">B")
U16 = struct.Struct(">H")
U32 = struct.Struct(">I")
I64 = struct.Struct(">q")


def seal(body: bytes | bytearray) -> bytes:
    if len(body) > BODY_SIZE:
        raise ValueError("page body exceeds capacity")
    padded = bytes(body).ljust(BODY_SIZE, b"\0")
    return padded + U32.pack(zlib.crc32(padded))


def verify(page: bytes) -> bytes:
    if len(page) != PAGE_SIZE:
        raise Corruption("incomplete page")
    body = page[:BODY_SIZE]
    if zlib.crc32(body) != U32.unpack_from(page, BODY_SIZE)[0]:
        raise Corruption("page checksum mismatch")
    return body


class Reader:
    """Bounded cursor over an encoded catalog entry or row."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        end = self.pos + size
        if end > len(self.data):
            raise Corruption("field extends past end of record")
        field = self.data[self.pos : end]
        self.pos = end
        return field

    def number(self, layout: struct.Struct) -> int:
        return layout.unpack(self.take(layout.size))[0]

    def string(self, length_type: struct.Struct) -> str:
        try:
            return self.take(self.number(length_type)).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Corruption("invalid UTF-8") from exc
