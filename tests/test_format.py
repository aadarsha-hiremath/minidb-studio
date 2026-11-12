"""Exercise structural checks even when a malformed page carries a valid CRC."""

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from minidb import Corruption, Database, UnsupportedVersion
from minidb.binary import seal


class FormatTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "format.mdb"
        # Exercise the original byte contract against the compatible v1 reader.
        header = struct.pack(">8sHHI", b"MINIDB\r\n", 1, 4096, 3)
        schema = b"SCHM\0\0\0\x01\x01t\x02\x01b\x03\x01s\x02"
        data = bytearray(4092)
        struct.pack_into(">4sIIHHHHH", data, 0, b"DATA", 2, 1, 2, 4088, 4, 4084, 4)
        data[-8:] = b"\0\0\x01B\x01\0\x01A"
        self.path.write_bytes(seal(header) + seal(schema) + seal(data))
        self.original = self.path.read_bytes()

    def replace(self, page, offset, value):
        data = bytearray(self.original)
        start = page * 4096
        data[start + offset : start + offset + len(value)] = value
        crc = zlib.crc32(data[start : start + 4092])
        struct.pack_into(">I", data, start + 4092, crc)
        self.path.write_bytes(data)

    def assert_rejected(self, exc=Corruption):
        with self.assertRaises(exc), Database.open(self.path):
            pass
        # Windows also exercises descriptor cleanup after an unsuccessful open.
        self.path.write_bytes(self.original)
        with Database.open(self.path) as db:
            self.assertEqual(len(list(db.scan("t"))), 2)

    def test_format_fixture(self):
        # Compare the current encoder's actual persisted pages to the v1 fixture.
        modern = self.path.with_name("encoded.mdb")
        with Database.create(modern) as db, db.transaction():
            db.execute("CREATE TABLE t (b bool, s text)")
            db.execute("INSERT INTO t VALUES (true,'A')")
            db.execute("INSERT INTO t VALUES (false,'B')")
        self.assertEqual(modern.read_bytes()[3 * 4096 :], self.original[4096:])
        self.assertEqual(
            self.original[:16], b"MINIDB\r\n\x00\x01\x10\x00\x00\x00\x00\x03"
        )
        self.assertEqual(
            self.original[4096:4113], b"SCHM\0\0\0\x01\x01t\x02\x01b\x03\x01s\x02"
        )
        self.assertEqual(
            self.original[8192:8214],
            struct.pack(">4sIIHHHHH", b"DATA", 2, 1, 2, 4088, 4, 4084, 4),
        )
        self.assertEqual(self.original[-12:-4], b"\0\0\x01B\x01\0\x01A")

    def test_header_fields(self):
        for offset, value in [(0, b"X"), (10, b"\0\0"), (12, b"\0" * 4), (60, b"X")]:
            with self.subTest(offset=offset):
                self.replace(0, offset, value)
                self.assert_rejected()
        self.replace(0, 8, b"\0\x02")
        self.assert_rejected(UnsupportedVersion)

    def test_truncation_extra_bytes_and_checksum(self):
        for data in (
            b"",
            self.original[:-4096],
            self.original[:-1],
            self.original + b"x",
            self.original[:-1] + bytes([self.original[-1] ^ 1]),
        ):
            with self.subTest(length=len(data)):
                self.path.write_bytes(data)
                self.assert_rejected()

    def test_catalog_fields(self):
        for offset, value in [
            (0, b"NONE"),
            (7, b"\x02"),
            (9, b"9"),
            (10, b"\0"),
            (13, b"\xff"),
            (15, b"b"),
            (20, b"x"),
        ]:
            with self.subTest(offset=offset):
                self.replace(1, offset, value)
                self.assert_rejected()

    def test_data_page_directory_and_owner(self):
        for offset, value in [
            (0, b"NONE"),
            (7, b"\x01"),
            (11, b"\0"),
            (11, b"\x02"),
            (12, b"\xff\xff"),
            (12, b"\0\0"),
            (14, b"\0\x07"),
            (16, b"\0\0"),
            (18, b"\x0f\xf8"),
            (40, b"x"),
        ]:
            with self.subTest(offset=offset, value=value):
                self.replace(2, offset, value)
                self.assert_rejected()

    def test_malformed_row_values(self):
        for offset, value in [
            (4088, b"\x02"),
            (4089, b"\xff\xff"),
            (4089, b"\0\0"),
            (4091, b"\xff"),
        ]:
            with self.subTest(offset=offset, value=value):
                self.replace(2, offset, value)
                self.assert_rejected()
