"""Public, self-contained migration regression for the documented v1 format."""

import hashlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from legacy_fixture import fixture

from minidb import (
    Corruption,
    Database,
    NotFound,
    RecordID,
    StorageError,
    TransactionError,
    wal,
)

SHA256 = {
    False: "51ab8e1a13465db9e249af72a4bcc59ab02368dea1a866ce1eb24d6c0e4d301d",
    True: "5c92ccc3725e5409ee23d688c29d73ccabd0bab2fffa53e17e6725aada6d3419",
}
ROWS = [
    (RecordID(2, 0), {"n": -7, "s": "café\0", "enabled": True}),
    (RecordID(2, 2), {"n": 4, "s": "雪", "enabled": False}),
    (RecordID(3, 0), {"n": 4, "s": "🙂", "enabled": True}),
    (RecordID(3, 1), {"n": 9, "s": "", "enabled": False}),
]


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "old.mdb"
        self.destination = self.root / "new.mdb"
        self.journal = self.destination.with_suffix(".mdb.wal")

    def check_contents(self, db, *, indexed):
        self.assertEqual(db.tables(), ("records",))
        self.assertEqual(
            [(c.name, c.kind) for c in db.schema("records").columns],
            [("n", "int"), ("s", "text"), ("enabled", "bool")],
        )
        self.assertEqual(list(db.scan("records")), ROWS)
        for rid, row in ROWS:
            self.assertEqual(db.get("records", rid), row)
        with self.assertRaises(NotFound):
            db.get("records", RecordID(2, 1))
        self.assertEqual(db.indexes("records"), (("by_n", "n", 5),) if indexed else ())
        query = "SELECT s FROM records WHERE n>=? AND n<=?"
        self.assertEqual(
            db.execute(query, (4, 9)).rows, [{"s": "雪"}, {"s": "🙂"}, {"s": ""}]
        )
        path = "Index Range Scan using by_n" if indexed else "Sequential Scan"
        self.assertIn(path, db.execute("EXPLAIN " + query, (4, 9)).plan)

    def test_migrates_heap_and_indexed_v1(self):
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                source = self.root / f"legacy-{indexed}.mdb"
                destination = self.root / f"current-{indexed}.mdb"
                original = fixture(indexed=indexed)
                self.assertEqual(hashlib.sha256(original).hexdigest(), SHA256[indexed])
                source.write_bytes(original)
                with Database.open(source) as db:
                    self.assertEqual(db.storage_status()["format"], 1)
                    self.check_contents(db, indexed=indexed)
                    with self.assertRaises(TransactionError):
                        db.execute("DELETE FROM records")
                Database.migrate(source, destination)
                self.assertFalse(os.path.samefile(source, destination))
                self.assertEqual(destination.read_bytes()[3 * 4096 :], original[4096:])
                self.assertEqual(
                    destination.with_suffix(".mdb.wal").stat().st_size, 4096
                )
                for _ in range(2):
                    with Database.open(destination) as db:
                        self.assertEqual(db.storage_status()["format"], 3)
                        self.check_contents(db, indexed=indexed)
                with Database.open(destination) as db:
                    db.execute("UPDATE records SET n=10 WHERE n=9")
                with Database.open(destination) as db:
                    self.assertEqual(db.get("records", RecordID(3, 1))["n"], 10)
                    self.assertEqual(
                        db.execute("SELECT s FROM records WHERE n=10").rows, [{"s": ""}]
                    )
                    self.assertEqual(
                        db.execute("SELECT s FROM records WHERE n=9").rows, []
                    )
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(source.with_suffix(".mdb.wal").exists())

    def test_bad_legacy_page_rejected_before_creating_output(self):
        damaged = bytearray(fixture(indexed=True))
        damaged[-20] ^= 1
        self.source.write_bytes(damaged)
        with self.assertRaises(Corruption):
            Database.migrate(self.source, self.destination)
        self.assertEqual(self.source.read_bytes(), damaged)
        self.assertEqual(set(self.root.iterdir()), {self.source})

    def test_preserves_preexisting_destination_or_wal(self):
        original = fixture(indexed=False)
        self.source.write_bytes(original)
        for exists in (self.destination, self.journal):
            with self.subTest(exists=exists.name):
                exists.write_bytes(b"keep")
                with self.assertRaises(StorageError):
                    Database.migrate(self.source, self.destination)
                self.assertEqual(exists.read_bytes(), b"keep")
                self.assertEqual(set(self.root.iterdir()), {self.source, exists})
                exists.unlink()
        with self.assertRaises(StorageError):
            Database.migrate(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_failed_copy_removes_owned_outputs_and_releases_locks(self):
        original = fixture(indexed=True)
        self.source.write_bytes(original)
        neighbor = self.root / "unrelated.mdb"
        neighbor.write_bytes(b"keep")
        for stage in (
            "before_wal_create",
            "preamble_synced",
            "bootstrap_synced",
            "anchor_synced",
        ):
            with self.subTest(stage=stage):

                def fail(boundary, stop=stage):
                    if boundary == stop:
                        raise OSError("injected migration I/O failure")

                with (
                    patch.object(wal, "boundary", fail),
                    self.assertRaisesRegex(StorageError, "injected migration"),
                ):
                    Database.migrate(self.source, self.destination)
                self.assertEqual(set(self.root.iterdir()), {self.source, neighbor})
                self.assertEqual(self.source.read_bytes(), original)
                self.assertEqual(neighbor.read_bytes(), b"keep")
        Database.migrate(self.source, self.destination)
        with Database.open(self.destination) as db:
            self.check_contents(db, indexed=True)


if __name__ == "__main__":
    unittest.main()
