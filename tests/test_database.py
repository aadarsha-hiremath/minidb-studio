import os
import random
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from minidb import (
    Busy,
    Column,
    Database,
    NoSpace,
    NotFound,
    RecordID,
    Schema,
    StorageError,
    ValidationError,
)
from minidb.pages import MAX_RECORD

TEXT = Schema("items", (Column("value", "text"),))
MIXED = Schema(
    "mixed", (Column("n", "int"), Column("flag", "bool"), Column("label", "text"))
)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "example.mdb"
        self.db = Database.create(self.path)
        self.addCleanup(lambda: self.db.close())

    def reopen(self):
        self.db.close()
        self.db = Database.open(self.path)

    def test_create_empty_and_no_clobber(self):
        self.reopen()
        self.assertEqual(self.path.stat().st_size, 3 * 4096)
        self.assertEqual(self.db.tables(), ())
        with self.assertRaises(StorageError):
            Database.create(self.path)

    def test_interleaved_catalog_and_data(self):
        self.db.create_table(TEXT)
        a = self.db.insert("items", {"value": "a"})
        self.db.create_table(MIXED)
        row = {"n": -(2**63), "flag": True, "label": "नमस्ते🙂\0"}
        b = self.db.insert("mixed", row)
        self.reopen()
        self.assertEqual(self.db.tables(), ("items", "mixed"))
        self.assertEqual(self.db.schema("mixed"), MIXED)
        self.assertEqual(self.db.get("mixed", b), row)
        with self.assertRaises(NotFound):
            self.db.get("mixed", a)
        self.db.update("mixed", b, {**row, "n": 2**63 - 1})
        self.db.delete("items", a)
        self.reopen()
        self.assertEqual(list(self.db.scan("items")), [])
        self.assertEqual(self.db.get("mixed", b)["n"], 2**63 - 1)

    def test_names_and_schema_validation(self):
        for name in ("", "7bad", "a.b", "../x", "é", "x" * 64, None):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                Schema(name, TEXT.columns)
        for columns in (
            (),
            TEXT.columns * 2,
            [Column("a", "int")],
            (None,),
            TEXT.columns * 64,
        ):
            with self.subTest(columns=columns), self.assertRaises(ValidationError):
                Schema("t", columns)
        for name, kind in (("1x", "int"), ("x", "float")):
            with self.assertRaises(ValidationError):
                Column(name, kind)
        self.db.create_table(TEXT)
        with self.assertRaises(ValidationError):
            self.db.create_table(TEXT)
        with self.assertRaises(NotFound):
            self.db.schema("other")

    def test_catalog_page_limit(self):
        columns = tuple(Column("a" * 60 + f"{n:03d}", "int") for n in range(63))
        schema = Schema("x" * 63, columns)
        with self.assertRaises(NoSpace):
            self.db.create_table(schema)
        self.assertEqual(self.db.tables(), ())
        self.db.create_table(Schema("a", columns[:-1]))
        self.reopen()
        self.assertEqual(self.db.schema("a").columns, columns[:-1])

    def test_row_validation_preserves_existing_record(self):
        self.db.create_table(MIXED)
        row = {"n": 0, "flag": False, "label": ""}
        rid = self.db.insert("mixed", row)
        for changes in (
            {"n": True},
            {"n": 2**63},
            {"n": -(2**63) - 1},
            {"n": 1.0},
            {"flag": 1},
            {"label": None},
            {"label": "\ud800"},
            {"new": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.db.update("mixed", rid, {**row, **changes})
        for invalid in (None, {}, ["n", "flag", "label"]):
            with self.assertRaises(ValidationError):
                self.db.insert("mixed", invalid)
        self.assertEqual(self.db.get("mixed", rid), row)

    def test_maximum_record_and_utf8_bytes(self):
        self.db.create_table(TEXT)
        text = "é" * ((MAX_RECORD - 2) // 2)
        rid = self.db.insert("items", {"value": text})
        for oversized in (text + "x", "a" * 65536):
            with self.assertRaises(NoSpace):
                self.db.insert("items", {"value": oversized})
        other = self.db.insert("items", {"value": ""})
        self.assertGreater(other.page, rid.page)
        self.reopen()
        self.assertEqual(self.db.get("items", rid), {"value": text})

    def test_growth_compaction_and_tombstones(self):
        self.db.create_table(TEXT)
        a, b = [self.db.insert("items", {"value": "a" * 2000}) for _ in range(2)]
        self.assertEqual(a.page, b.page)
        with self.assertRaises(NoSpace):
            self.db.update("items", a, {"value": "b" * 3000})
        self.assertEqual(self.db.get("items", a)["value"], "a" * 2000)
        self.db.delete("items", b)
        self.db.update("items", a, {"value": "b" * 3000})
        c = self.db.insert("items", {"value": "c" * 1000})
        self.assertEqual(c, RecordID(b.page, b.slot + 1))
        self.reopen()
        with self.assertRaises(NotFound):
            self.db.get("items", b)
        self.assertEqual([rid for rid, _ in self.db.scan("items")], [a, c])

    def test_directory_full_of_tombstones(self):
        self.db.create_table(TEXT)
        # Two-byte empty strings plus one four-byte descriptor per historical insert.
        for _ in range(1020):
            rid = self.db.insert("items", {"value": ""})
            self.db.delete("items", rid)
        self.assertEqual(rid.page, 3)
        self.assertEqual(list(self.db.scan("items")), [])
        self.reopen()

    def test_record_id_validation(self):
        self.db.create_table(TEXT)
        rid = self.db.insert("items", {"value": "live"})
        for parts in ((True, 0), (0, 0), (1, -1), (2**32, 0), (1, 65536), ("2", 0)):
            with self.assertRaises(ValidationError):
                RecordID(*parts)
        with self.assertRaises(ValidationError):
            self.db.get("items", (rid.page, rid.slot))
        for wrong in (RecordID(1, 0), RecordID(rid.page, 1), RecordID(1000, 0)):
            with self.assertRaises(NotFound):
                self.db.delete("items", wrong)
        self.assertEqual(self.db.get("items", rid)["value"], "live")

    def test_seeded_crud_reference_model(self):
        self.db.create_table(TEXT)
        rng = random.Random(1957)
        model = {}
        deleted = set()
        for i in range(800):
            value = {"value": "À" * rng.randrange(0, 240) + str(i)}
            op = rng.randrange(5)
            if not model or op <= 1:
                rid = self.db.insert("items", value)
                self.assertNotIn(rid, deleted | model.keys())
                model[rid] = value
            else:
                rid = rng.choice(list(model))
                if op == 2:
                    self.db.delete("items", rid)
                    del model[rid]
                    deleted.add(rid)
                elif op == 3:
                    try:
                        self.db.update("items", rid, value)
                    except NoSpace:
                        self.assertEqual(self.db.get("items", rid), model[rid])
                    else:
                        model[rid] = value
                else:
                    self.assertEqual(self.db.get("items", rid), model[rid])
            if i % 50 == 0:
                self.reopen()
                self.assertEqual(list(self.db.scan("items")), sorted(model.items()))
        self.reopen()
        self.assertEqual(list(self.db.scan("items")), sorted(model.items()))

    def test_large_collection_and_repeated_reopen(self):
        self.db.create_table(MIXED)
        records = []
        for i in range(2000):
            row = {"n": i, "flag": bool(i % 2), "label": str(i)}
            records.append((self.db.insert("mixed", row), row))
        self.assertGreater(records[-1][0].page, records[0][0].page)
        for _ in range(3):
            self.reopen()
            self.assertEqual(list(self.db.scan("mixed")), records)

    def test_lock_and_lifecycle(self):
        with self.assertRaises(Busy):
            Database.open(self.path)
        self.db.close()
        self.db.close()
        with self.assertRaises(StorageError):
            self.db.tables()
        self.reopen()

    def test_thread_confinement(self):
        errors = []

        def use_handle():
            try:
                self.db.create_table(TEXT)
            except StorageError as exc:
                errors.append(exc)

        thread = threading.Thread(target=use_handle)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.db.tables(), ())

    def test_scan_detects_mutation_and_close(self):
        self.db.create_table(TEXT)
        self.db.insert("items", {"value": "a"})
        for action in (lambda: self.db.insert("items", {"value": "b"}), self.db.close):
            scan = self.db.scan("items")
            next(scan)
            action()
            with self.assertRaises(StorageError):
                next(scan)

    def test_invalid_paths(self):
        with self.assertRaises(StorageError):
            Database.open(self.db._pager.file.fileno())
        self.assertEqual(self.db.tables(), ())
        for path in (self.path.parent, self.path.parent / "absent" / "x"):
            for opener in (Database.open, Database.create):
                with self.assertRaises(StorageError):
                    opener(path)
        with self.assertRaises(StorageError):
            Database.open(self.path.with_suffix(".missing"))
        self.assertFalse(self.path.with_suffix(".missing").exists())

    def test_read_only_file(self):
        self.db.close()
        self.path.chmod(stat.S_IREAD)
        self.addCleanup(self.path.chmod, stat.S_IREAD | stat.S_IWRITE)
        if os.access(self.path, os.W_OK):
            self.skipTest("account can override file write permissions")
        with self.assertRaises(StorageError):
            Database.open(self.path)

    def test_failed_write_disables_handle_and_releases_lock(self):
        self.db.create_table(TEXT)
        real = self.db._pager.journal
        with patch.object(self.db._pager, "journal", wraps=real) as stream:
            stream.closed = False
            stream.write.side_effect = OSError("simulated disk full")
            with self.assertRaisesRegex(StorageError, "disk full"):
                self.db.insert("items", {"value": "abc"})
            with self.assertRaises(StorageError):
                self.db.tables()
        self.reopen()
        self.assertEqual(list(self.db.scan("items")), [])

    def test_short_write_and_header_failure(self):
        self.db.create_table(TEXT)
        real = self.db._pager.file
        calls = 0

        def incomplete(data):
            nonlocal calls
            calls += 1
            return real.write(data) if calls == 1 else 0

        proxy = Mock(wraps=real)
        proxy.closed = False
        proxy.write.side_effect = incomplete
        with (
            patch.object(self.db._pager, "file", proxy),
            self.assertRaisesRegex(StorageError, "short storage write"),
        ):
            self.db.insert("items", {"value": "abc"})
        self.reopen()
        self.assertEqual([r for _, r in self.db.scan("items")], [{"value": "abc"}])

    def test_partial_overwrite_is_detected_on_reopen(self):
        self.db.create_table(TEXT)
        rid = self.db.insert("items", {"value": "old"})
        real = self.db._pager.file
        with patch.object(self.db._pager, "file", wraps=real) as stream:
            stream.closed = False
            stream.write.side_effect = lambda data: real.write(data[:20])
            with self.assertRaisesRegex(StorageError, "short storage write"):
                self.db.update("items", rid, {"value": "different length"})
        self.reopen()
        self.assertEqual(self.db.get("items", rid), {"value": "different length"})

    def test_read_failure_and_context_exception(self):
        self.db.create_table(TEXT)
        rid = self.db.insert("items", {"value": "old"})
        with patch.object(self.db._pager, "file", wraps=self.db._pager.file) as stream:
            stream.closed = False
            stream.read.side_effect = OSError("simulated read failure")
            with self.assertRaisesRegex(StorageError, "read failure"):
                self.db.get("items", rid)
        with self.assertRaisesRegex(ValueError, "application error"), self.db:
            self.db.update("items", rid, {"value": "persisted"})
            raise ValueError("application error")
        self.reopen()
        self.assertEqual(self.db.get("items", rid), {"value": "persisted"})

    def test_sync_failure_releases_lock(self):
        with (
            patch(
                "minidb.pager.os.fsync", side_effect=OSError("simulated sync failure")
            ),
            self.assertRaisesRegex(StorageError, "sync failure"),
        ):
            self.db.create_table(TEXT)
        self.db.close()
        self.assertTrue(self.db._pager.file.closed)
        self.reopen()
