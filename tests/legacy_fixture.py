"""Synthetic v1 files encoded directly from docs/format.md with stdlib only."""

import argparse
import struct
import zlib
from pathlib import Path

PAGE_SIZE = 4096
BODY_SIZE = PAGE_SIZE - 4


def page(body: bytes | bytearray) -> bytes:
    padded = bytes(body).ljust(BODY_SIZE, b"\0")
    return padded + struct.pack(">I", zlib.crc32(padded))


def data(page_id: int, rows: list[tuple[int, str, bool] | None]) -> bytes:
    body = bytearray(BODY_SIZE)
    struct.pack_into(">4sIIH", body, 0, b"DATA", page_id, 1, len(rows))
    cursor = BODY_SIZE
    for slot, row in enumerate(rows):
        if row is None:
            continue
        number, label, enabled = row
        encoded = label.encode("utf-8")
        record = struct.pack(">qH", number, len(encoded)) + encoded + bytes([enabled])
        cursor -= len(record)
        body[cursor : cursor + len(record)] = record
        struct.pack_into(">HH", body, 14 + slot * 4, cursor, len(record))
    return page(body)


def entry(value: int, page_id: int, slot: int) -> bytes:
    return struct.pack(">HQIH", 8, value + 2**63, page_id, slot)


def fixture(*, indexed: bool) -> bytes:
    pages = [
        page(
            b"SCHM"
            + struct.pack(">I", 1)
            + b"\x07records\x03\x01n\x01\x01s\x02\x07enabled\x03"
        ),
        data(2, [(-7, "café\0", True), None, (4, "雪", False)]),
        data(3, [(4, "🙂", True), (9, "", False)]),
    ]
    if indexed:
        fence = entry(4, 3, 0)
        pages.extend(
            [
                page(struct.pack(">4sIIIBBB", b"IDXM", 4, 1, 5, 0, 1, 4) + b"by_n"),
                page(
                    struct.pack(">4sIIIH", b"IBRN", 5, 4, 6, 1)
                    + fence
                    + struct.pack(">I", 7)
                ),
                page(
                    struct.pack(">4sIIIH", b"ILEF", 6, 4, 7, 2)
                    + entry(-7, 2, 0)
                    + entry(4, 2, 2)
                ),
                page(
                    struct.pack(">4sIIIH", b"ILEF", 7, 4, 0, 2) + fence + entry(9, 3, 1)
                ),
            ]
        )
    return page(
        struct.pack(">8sHHI", b"MINIDB\r\n", 1, PAGE_SIZE, len(pages) + 1)
    ) + b"".join(pages)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--indexed", action="store_true")
    args = parser.parse_args()
    with args.destination.open("xb") as stream:
        stream.write(fixture(indexed=args.indexed))
