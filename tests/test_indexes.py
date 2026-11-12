import operator
import random
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minidb import (
    Busy,
    Corruption,
    Database,
    NoSpace,
    NotFound,
    StorageError,
    ValidationError,
)
from minidb.binary import PAGE_SIZE, seal


class IndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "tree.mdb"
        self.db = Database.create(self.path)
        self.addCleanup(lambda: self.db.close())
        self.db.execute("CREATE TABLE things(k text, n int, flag bool)")

    def reopen(self):
        self.db.close()
        self.db = Database.open(self.path)

    def add(self, key, n=0):
        return self.db.insert("things", {"k": key, "n": n, "flag": n % 2 == 0})

    def test_legacy_before_and_after_index_creation(self):
        # Independent v1 encoding of header, single bool schema and one live row.
        self.db.close()
        header = struct.pack(">8sHHI", b"MINIDB\r\n", 1, 4096, 3)
        schema = b"SCHM" + struct.pack(">I", 1) + b"\x01t\x01\x01b\x03"
        body = bytearray(4092)
        struct.pack_into(">4sIIHHH", body, 0, b"DATA", 2, 1, 1, 4091, 1)
        body[-1] = 1
        original = seal(header) + seal(schema) + seal(body)
        self.path.write_bytes(original)
        self.reopen()
        self.assertEqual(self.db.execute("SELECT b FROM t").rows, [{"b": True}])
        self.db.close()
        self.assertEqual(self.path.read_bytes(), original)
        source = self.path
        self.path = source.with_name("migrated.mdb")
        Database.migrate(source, self.path)
        self.reopen()
        self.db.create_index("ix", "t", "b")
        self.reopen()
        self.assertEqual(
            self.db.execute("SELECT b FROM t WHERE b=true").rows, [{"b": True}]
        )
        self.db.close()
        self.assertEqual(self.path.read_bytes()[12288:20480], original[4096:])
        self.assertEqual(source.read_bytes(), original)

    def test_key_boundaries_and_validation_before_write(self):
        rid = self.add("é" * 120)
        self.db.create_index("ix", "things", "k")
        self.assertEqual(
            len(self.db.execute("SELECT * FROM things WHERE k<?", ("🙂" * 241,)).rows),
            1,
        )
        for action in [
            lambda: self.add("é" * 121),
            lambda: self.db.update(
                "things", rid, {"k": "a" * 241, "n": 1, "flag": False}
            ),
        ]:
            with self.assertRaises(NoSpace):
                action()
        self.assertEqual(self.db.get("things", rid)["k"], "é" * 120)
        self.db.drop_index("ix")
        self.add("🙂" * 61)
        size = self.path.stat().st_size
        with self.assertRaises(NoSpace):
            self.db.create_index("ix", "things", "k")
        self.assertEqual(self.path.stat().st_size, size)
        self.reopen()
        self.assertEqual(len(list(self.db.scan("things"))), 2)

    def test_tree_height_duplicates_empty_leaves_and_reinsertion(self):
        self.db.create_index("ix", "things", "k")
        tree = self.db._indexes["ix"]
        self.assertEqual(tree.capacity, 16)
        key = "界" * 80
        for i in range(tree.capacity):
            self.add(key, i)
        self.assertFalse(tree.node(tree.info.root).children)
        self.add(key, 16)
        self.assertEqual(len(tree.node(tree.info.root).children), 2)
        for i in range(17, 850):
            self.add(key, i)
        self.assertTrue(tree.node(tree.node(tree.info.root).children[0]).children)
        self.reopen()
        self.assertEqual(
            self.db.execute("SELECT n FROM things WHERE k=?", (key,)).rows,
            [{"n": i} for i in range(850)],
        )
        self.assertEqual(
            self.db.execute("DELETE FROM things WHERE k=?", (key,)).rowcount, 850
        )
        self.reopen()
        self.assertEqual(
            self.db.execute("SELECT * FROM things WHERE k>=?", ("",)).rows, []
        )
        for k in ("", key, "🙂", "z", "न", "\0", "e\u0301", "é"):
            self.add(k)
        self.reopen()
        rows = self.db.execute("SELECT k FROM things WHERE k>='' ORDER BY k").rows
        self.assertEqual(
            [r["k"] for r in rows],
            sorted(["", key, "🙂", "z", "न", "\0", "e\u0301", "é"]),
        )

    def test_index_definitions_drop_and_recreate(self):
        self.db.create_index("keys", "things", "k")
        with self.assertRaises(Busy):
            Database.open(self.path)
        for args in [
            ("keys", "things", "n"),
            ("different", "things", "k"),
            ("bad!", "things", "n"),
            ("other", "things", "missing"),
        ]:
            with self.assertRaises(ValidationError):
                self.db.create_index(*args)
        self.db.execute("DROP INDEX keys")
        self.reopen()
        self.assertIn(
            "Sequential Scan",
            self.db.execute("EXPLAIN SELECT * FROM things WHERE k=''").plan,
        )
        with self.assertRaises(NotFound):
            self.db.drop_index("keys")
        self.db.create_index("keys", "things", "n")
        self.add("", -(2**63))
        self.add("x", 2**63 - 1)
        self.reopen()
        self.assertEqual(
            self.db.execute(
                "SELECT n FROM things WHERE n>=-9223372036854775808 ORDER BY n"
            ).rows,
            [{"n": -(2**63)}, {"n": 2**63 - 1}],
        )

    def test_seeded_index_scan_and_model_agreement(self):
        rng = random.Random(229)
        keys = ["", "A", "中", "a", "a\0", "🙂", "é", "e\u0301"]
        self.db.execute("CREATE TABLE heap(k text,n int,flag bool)")
        for col in ("n", "k", "flag"):
            self.db.create_index("ix_" + col, "things", col)
        model = {}
        for step in range(500):
            n = rng.randrange(-20, 21)
            row = {"k": rng.choice(keys), "n": n, "flag": bool(n % 2)}
            mode = rng.randrange(5)
            if not model or mode < 3:
                rid = self.db.insert("things", row)
                other = self.db.insert("heap", row)
                model[rid] = (other, row)
            else:
                rid = rng.choice(list(model))
                other, _ = model[rid]
                if mode == 3:
                    try:
                        self.db.update("things", rid, row)
                    except NoSpace:
                        with self.assertRaises(NoSpace):
                            self.db.update("heap", other, row)
                    else:
                        self.db.update("heap", other, row)
                        model[rid] = (other, row)
                else:
                    self.db.delete("things", rid)
                    self.db.delete("heap", other)
                    del model[rid]
            if step % 40 == 0:
                self.reopen()
                for col, probe in row.items():
                    for op, func in [
                        ("=", operator.eq),
                        ("!=", operator.ne),
                        ("<", operator.lt),
                        ("<=", operator.le),
                        (">", operator.gt),
                        (">=", operator.ge),
                    ]:
                        tail = f" WHERE {col}{op}? AND n<=? ORDER BY n DESC LIMIT 12"
                        actual = self.db.execute(
                            "SELECT * FROM things" + tail, (probe, 15)
                        ).rows
                        oracle = self.db.execute(
                            "SELECT * FROM heap" + tail, (probe, 15)
                        ).rows
                        expected = [
                            r
                            for _, (_, r) in sorted(model.items())
                            if func(r[col], probe) and r["n"] <= 15
                        ]
                        self.assertEqual(
                            actual,
                            sorted(expected, key=lambda r: r["n"], reverse=True)[:12],
                        )
                        self.assertEqual(actual, oracle)
                self.assertFalse(
                    self.db.execute("SELECT * FROM things WHERE n>3 AND n<0").rows
                )

    def test_interrupted_row_plus_index_write(self):
        self.add("before")
        self.db.create_index("idx", "things", "n")
        tree = self.db._indexes["idx"]
        with (
            patch.object(tree, "_save", side_effect=StorageError("disk write failed")),
            self.assertRaisesRegex(StorageError, "disk write failed"),
        ):
            self.add("after", 1)
        self.reopen()
        self.assertEqual(
            self.db.execute("SELECT k FROM things WHERE n=0").rows, [{"k": "before"}]
        )
        self.assertEqual(
            self.db.execute("SELECT k FROM things").rows, [{"k": "before"}]
        )

    def test_interruption_at_each_root_split_write(self):
        self.db.create_index("ix", "things", "k")
        for i in range(16):
            self.add(str(i), i)
        self.db.close()
        baseline = self.path.read_bytes()
        self.reopen()
        with patch.object(
            self.db._pager, "write", wraps=self.db._pager.write
        ) as writer:
            self.add("new", 17)
            writes = writer.call_count
        self.assertGreaterEqual(writes, 5)
        for stop in range(1, writes + 1):
            with self.subTest(stop=stop):
                self.db.close()
                self.path.write_bytes(baseline)
                self.reopen()
                real_write = self.db._pager.write
                calls = 0

                def fail_at(page, data, stop=stop, real_write=real_write):
                    nonlocal calls
                    calls += 1
                    if calls == stop:
                        raise StorageError("interrupted split")
                    real_write(page, data)

                with (
                    patch.object(self.db._pager, "write", side_effect=fail_at),
                    self.assertRaisesRegex(StorageError, "interrupted split"),
                ):
                    self.add("new", 17)
                self.reopen()
                self.assertEqual(len(list(self.db.scan("things"))), 16)
                self.assertEqual(
                    self.db.execute("SELECT k FROM things WHERE k='new'").rows, []
                )

    def test_shuffled_deep_tree_range_bounds(self):
        keys = [f"key-{i:04}" for i in range(1200)]
        random.Random(104).shuffle(keys)
        for i, key in enumerate(keys):
            self.add(key, i)
        self.db.create_index("ix", "things", "k")
        self.reopen()
        tree = self.db._indexes["ix"]
        self.assertTrue(tree.node(tree.node(tree.info.root).children[0]).children)
        for start in range(0, 1200, 37):
            low, high = f"key-{start:04}", f"key-{start + 47:04}"
            expected = [{"k": k} for k in keys if low < k <= high]
            actual = self.db.execute(
                "SELECT k FROM things WHERE k>? AND k<=?", (low, high)
            )
            self.assertEqual(actual.rows, expected)

    def test_corruption_with_valid_checksums(self):
        for i in range(40):
            self.add(f"{i:03}", i)
        self.db.create_index("idx", "things", "k")
        tree = self.db._indexes["idx"]
        root = tree.node(tree.info.root)
        meta, top, leaf = tree.info.page, root.page, root.children[0]
        self.db.close()
        original = self.path.read_bytes()

        def edit(page, offset, value):
            data = bytearray(original)
            start = (page + 2) * PAGE_SIZE
            data[start + offset : start + offset + len(value)] = value
            data[start : start + PAGE_SIZE] = seal(data[start : start + 4092])
            return data

        for name, data in [
            ("truncated", original[:-20]),
            ("owner", edit(leaf, 8, struct.pack(">I", 123))),
            ("metadata root", edit(meta, 12, struct.pack(">I", 1))),
            ("metadata type", edit(meta, 17, b"\x01")),
            ("orphan subtree", edit(meta, 12, struct.pack(">I", leaf))),
            ("cycle", edit(top, 12, struct.pack(">I", top))),
            ("link", edit(leaf, 12, struct.pack(">I", leaf))),
            ("count", edit(leaf, 16, b"\xff\xff")),
            ("key length", edit(leaf, 18, b"\xff\xff")),
            ("key encoding", edit(leaf, 20, b"\xff")),
            ("row mismatch", edit(leaf, 20, b" 00")),
            ("missing rid", edit(leaf, 23, struct.pack(">I", 1))),
            ("invalid slot", edit(leaf, 27, b"\xff\xff")),
            ("fence", edit(top, 20, b"999")),
        ]:
            with self.subTest(name=name):
                self.path.write_bytes(data)
                with self.assertRaises(Corruption), Database.open(self.path):
                    pass
        self.path.write_bytes(original)
        self.reopen()
