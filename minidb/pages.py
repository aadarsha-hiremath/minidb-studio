"""Canonical slotted pages. Payload compaction never changes slot numbers."""

import struct
from dataclasses import dataclass, field

from .binary import BODY_SIZE, seal
from .errors import Corruption, NoSpace, NotFound

HEADER = struct.Struct(">4sIIH")
SLOT = struct.Struct(">HH")
TAG = b"DATA"
MAX_RECORD = BODY_SIZE - HEADER.size - SLOT.size


@dataclass
class DataPage:
    page_id: int
    table_id: int
    slots: list[bytes | None] = field(default_factory=list)

    @property
    def free(self) -> int:
        directory = HEADER.size + SLOT.size * len(self.slots)
        return (
            BODY_SIZE
            - directory
            - sum(len(row) for row in self.slots if row is not None)
        )

    def get(self, slot: int) -> bytes:
        if not 0 <= slot < len(self.slots) or (record := self.slots[slot]) is None:
            raise NotFound("record slot is absent or deleted")
        return record

    def replace(self, slot: int, record: bytes | None) -> None:
        old = self.get(slot)
        if record is not None and len(record) > self.free + len(old):
            raise NoSpace("updated record cannot fit on its original page")
        self.slots[slot] = record

    def pack(self) -> bytes:
        if self.free < 0:
            raise NoSpace("data page is full")
        body = bytearray(BODY_SIZE)
        HEADER.pack_into(body, 0, TAG, self.page_id, self.table_id, len(self.slots))
        tail = BODY_SIZE
        for slot, record in enumerate(self.slots):
            if record is not None:
                tail -= len(record)
                body[tail : tail + len(record)] = record
                SLOT.pack_into(body, HEADER.size + slot * SLOT.size, tail, len(record))
        return seal(body)

    @classmethod
    def unpack(cls, body: bytes, page_id: int) -> "DataPage":
        tag, stored_id, table_id, count = HEADER.unpack_from(body)
        directory_end = HEADER.size + count * SLOT.size
        if (
            tag != TAG
            or stored_id != page_id
            or directory_end > BODY_SIZE
            or count == 0
        ):
            raise Corruption("invalid data page header")
        page = cls(page_id, table_id)
        tail = BODY_SIZE
        for index in range(count):
            start, size = SLOT.unpack_from(body, HEADER.size + index * SLOT.size)
            if (start, size) == (0, 0):
                page.slots.append(None)
            elif size and start >= directory_end and start + size == tail:
                page.slots.append(body[start:tail])
                tail = start
            else:
                raise Corruption("invalid slot bounds or payload order")
        if any(body[directory_end:tail]):
            raise Corruption("nonzero free space")
        return page
