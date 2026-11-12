import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minidb import Busy, Database, NoSpace, TransactionError, ValidationError, wal


class TransactionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "bank.mdb"
        self.db = Database.create(self.path)
        self.addCleanup(lambda: self.db.close())
        self.db.execute("CREATE TABLE t(k int, s text)")
        self.db.execute("CREATE INDEX ix ON t(k)")

    def reopen(self):
        self.db.close()
        self.db = Database.open(self.path)

    def test_sql_controls_and_visibility(self):
        self.db.execute("begin;")
        self.db.execute("INSERT INTO t VALUES (1,'A')")
        self.assertEqual(self.db.execute("SELECT k FROM t WHERE k=1").rows, [{"k": 1}])
        self.assertEqual(self.db.transaction_state, "active")
        with self.assertRaises(Busy):
            Database.open(self.path)
        self.db.execute("rollback")
        self.assertEqual(self.db.execute("SELECT * FROM t").rows, [])
        self.db.begin()
        self.db.execute("INSERT INTO t VALUES(2,'B')")
        self.db.execute("COMMIT;")
        self.reopen()
        self.assertEqual(
            self.db.execute("SELECT s FROM t WHERE k=2").rows, [{"s": "B"}]
        )

    def test_invalid_transitions_and_managed_scope(self):
        for action in (self.db.commit, self.db.rollback):
            with self.assertRaises(TransactionError):
                action()
        with self.db.transaction():
            for action in (
                self.db.begin,
                self.db.commit,
                self.db.rollback,
                self.db.close,
            ):
                with self.assertRaises(TransactionError):
                    action()
        self.assertEqual(self.db.transaction_state, "idle")

    def test_rollback_only_and_context_exception(self):
        with (
            self.assertRaisesRegex(TransactionError, "rollback-only"),
            self.db.transaction(),
        ):
            self.db.execute("INSERT INTO t VALUES(1,'one')")
            with self.assertRaises(ValidationError):
                self.db.execute("SELECT missing FROM t")
            self.assertEqual(self.db.transaction_state, "rollback-only")
        with self.assertRaises(KeyError), self.db.transaction():
            self.db.execute("DROP INDEX ix")
            raise KeyError("application error")
        self.db.begin()
        with self.assertRaises(ValidationError):
            self.db.execute("SELECT ! malformed")
        with self.assertRaises(TransactionError):
            self.db.commit()
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT * FROM t").rows, [])
        self.assertIn(
            "using ix", self.db.execute("EXPLAIN SELECT * FROM t WHERE k=1").plan
        )

    def test_transaction_control_during_record_serialization(self):
        db = self.db

        class ReentrantRow(dict):
            def __getitem__(self, key):
                db.commit()
                return super().__getitem__(key)

        with self.assertRaises(TransactionError):
            db.insert("t", ReentrantRow(k=5, s="five"))
        self.assertEqual(db.transaction_state, "idle")
        self.reopen()
        self.assertEqual(self.db.execute("SELECT * FROM t").rows, [])

    def test_close_discards_and_rids_reused_after_abort(self):
        self.db.begin()
        first = self.db.insert("t", {"k": 1, "s": "old"})
        self.reopen()
        self.assertEqual(self.db.execute("SELECT * FROM t").rows, [])
        self.assertEqual(self.db.insert("t", {"k": 2, "s": "new"}), first)

    def test_ddl_allocations_heap_and_wal_unchanged_after_rollback(self):
        self.db.close()
        before = self.path.read_bytes(), Path(str(self.path) + ".wal").read_bytes()
        self.reopen()
        self.db.begin()
        self.db.execute("CREATE INDEX names ON t(s)")
        for n in range(220):
            self.db.insert("t", {"k": n, "s": "界" * 66 + str(n)})
        self.db.execute("DROP INDEX ix")
        self.db.execute("CREATE TABLE tmp (v bool)")
        self.assertGreater(self.db._pager.count, 20)
        self.db.rollback()
        self.reopen()
        self.assertEqual(self.db.tables(), ("t",))
        self.assertEqual(self.db.execute("SELECT * FROM t").rows, [])
        self.db.close()
        self.assertEqual(
            before, (self.path.read_bytes(), Path(str(self.path) + ".wal").read_bytes())
        )

    def test_delete_and_key_updates_are_atomic(self):
        with self.db.transaction():
            for i in range(10):
                self.db.insert("t", {"k": i, "s": str(i)})
        for method, stmt in [
            ("delete", "DELETE FROM t WHERE k>=0"),
            ("update", "UPDATE t SET k=42 WHERE k>=0"),
        ]:
            original = getattr(self.db, method)
            calls = 0

            def interrupt(*args, original=original):
                nonlocal calls
                calls += 1
                original(*args)
                if calls == 3:
                    raise OSError("stop between rows")

            with (
                patch.object(self.db, method, side_effect=interrupt),
                self.assertRaises(OSError),
            ):
                self.db.execute(stmt)
            self.reopen()
            self.assertEqual(
                self.db.execute("SELECT k FROM t WHERE k>=0").rows,
                [{"k": i} for i in range(10)],
            )

    def test_overlay_limit_rolls_back(self):
        with patch.object(wal, "MAX_FRAMES", 1), self.assertRaises(NoSpace):
            self.db.execute("INSERT INTO t VALUES(1,'x')")
        self.assertEqual(self.db.transaction_state, "idle")
        self.reopen()
        self.assertEqual(self.db.execute("SELECT * FROM t").rowcount, 0)

    def test_seeded_commit_abort_model(self):
        rng = random.Random(1403)
        model = {}
        for batch in range(36):
            self.db.begin()
            tentative = model.copy()
            for n in range(7):
                key = batch * 10 + n
                value = "頁🙂" * rng.randrange(1, 200)
                self.db.insert("t", {"k": key, "s": value})
                tentative[key] = value
            if model:
                key = rng.choice(list(model))
                self.db.execute("DELETE FROM t WHERE k=?", (key,))
                del tentative[key]
            if rng.choice((True, False)):
                self.db.commit()
                model = tentative
            else:
                self.db.rollback()
            self.reopen()
            self.assertEqual(
                self.db.execute("SELECT k,s FROM t WHERE k>=0 ORDER BY k").rows,
                [{"k": k, "s": v} for k, v in sorted(model.items())],
            )

    def test_cli_session_eof_and_single_invocation(self):
        self.db.close()

        def cli(command, *args, script=None):
            return subprocess.run(
                [sys.executable, "-m", "minidb", command, str(self.path), *args],
                input=script,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

        good = cli(
            "session",
            script="BEGIN\nINSERT INTO t VALUES(3,'x')\nCOMMIT\nBEGIN\nDELETE FROM t\nROLLBACK\n",
        )
        self.assertEqual(good.returncode, 0, good.stderr)
        for command in [
            cli("sql", "BEGIN"),
            cli("session", script="BEGIN\nDELETE FROM t\n"),
        ]:
            self.assertEqual(command.returncode, 1)
        self.assertEqual(
            json.loads(cli("sql", "SELECT k FROM t").stdout)["rows"], [{"k": 3}]
        )
        self.assertEqual(json.loads(cli("status").stdout)["state"], "idle")

    def test_failed_creation_preserves_unrelated_sidecar(self):
        other = self.path.with_name("other.mdb")
        journal = Path(str(other) + ".wal")
        journal.write_bytes(b"keep me")
        from minidb import StorageError

        with self.assertRaises(StorageError):
            Database.create(other)
        self.assertEqual(journal.read_bytes(), b"keep me")
        path = self.path.with_name("bootstrap.mdb")

        def fail(phase):
            if phase == "before_wal_create":
                raise OSError("no file handles")

        with (
            patch.object(wal, "boundary", side_effect=fail),
            self.assertRaises(StorageError),
        ):
            Database.create(path)
        self.assertFalse(Path(str(path) + ".wal").exists())
