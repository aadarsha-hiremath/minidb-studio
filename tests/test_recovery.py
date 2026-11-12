"""Real process exits and independent commit-state oracles for the redo protocol."""

import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import minidb
from minidb import Busy, CommitError, Corruption, Database, StorageError, wal
from minidb.binary import PAGE_SIZE


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def seed(self, name):
        path = self.folder / (name + ".mdb")
        with Database.create(path) as db, db.transaction():
            db.execute("CREATE TABLE accounts(name text,balance int)")
            db.execute("CREATE INDEX balance_idx ON accounts(balance)")
            db.execute("INSERT INTO accounts VALUES('Ada',100)")
            db.execute("INSERT INTO accounts VALUES('Lin',100)")
        return path

    def crash(self, path, stage, mode="write"):
        worker = Path(__file__).with_name("crash_worker.py")
        env = dict(os.environ, PYTHONPATH=str(Path(minidb.__file__).parent.parent))
        p = subprocess.run(
            [sys.executable, str(worker), str(path), mode, stage],
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(p.returncode, 86, p.stderr)

    def verify(self, path, committed):
        with Database.open(path) as db:
            actual = db.execute(
                "SELECT * FROM accounts WHERE balance>=0 ORDER BY name"
            ).rows
            self.assertEqual(
                actual,
                [
                    {"name": "Ada", "balance": 90 if committed else 100},
                    {"name": "Lin", "balance": 110 if committed else 100},
                ],
            )
            self.assertEqual(
                actual, db.execute("SELECT * FROM accounts ORDER BY name").rows
            )
            name = "by_balance" if committed else "balance_idx"
            self.assertIn(
                "using " + name,
                db.execute("EXPLAIN SELECT * FROM accounts WHERE balance>=0").plan,
            )
            self.assertEqual(
                db.tables(), ("accounts", "events") if committed else ("accounts",)
            )
            if committed:
                self.assertEqual(
                    db.execute("SELECT n FROM events WHERE k>='' ORDER BY k").rows,
                    [{"n": i} for i in range(170)],
                )
        self.assertEqual(Path(str(path) + ".wal").stat().st_size, PAGE_SIZE)

    def test_process_termination_matrix(self):
        for stage, outcome in [
            ("staged", "old"),
            ("before_prepare", "old"),
            ("frame_written", "old"),
            ("partial_frame", "old"),
            ("partial_marker", "old"),
            ("prepare_synced", "old"),
            ("before_marker", "old"),
            ("marker_written", "uncertain"),
            ("commit_synced", "new"),
            ("partial_intent", "new"),
            ("partial_done", "new"),
            ("partial_heap", "new"),
            ("intent_synced", "new"),
            ("page_installed", "new"),
            ("pages_synced", "new"),
            ("checkpoint_header_written", "new"),
            ("checkpoint_synced", "new"),
            ("before_retire", "new"),
            ("wal_retired", "new"),
        ]:
            with self.subTest(stage=stage):
                path = self.seed(stage)
                self.crash(path, stage)
                if outcome == "uncertain":
                    # A caller lacking sync acknowledgement must allow either complete state.
                    with Database.open(path) as db:
                        committed = "events" in db.tables()
                else:
                    committed = outcome == "new"
                self.verify(path, committed)
                self.verify(path, committed)

    def test_exclusive_lock_and_external_kill_during_recovery(self):
        path = self.seed("locked-recovery")
        self.crash(path, "commit_synced")
        args = [
            # Windows venv python.exe can be a redirector. Kill the process
            # that owns the lock and wait for that exact process to terminate.
            getattr(sys, "_base_executable", sys.executable)
            if os.name == "nt"
            else sys.executable,
            str(Path(__file__).with_name("crash_worker.py")),
            str(path),
            "hold",
            "page_installed",
        ]
        env = dict(os.environ, PYTHONPATH=str(Path(minidb.__file__).parent.parent))
        with subprocess.Popen(
            args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        ) as child:
            ready = queue.Queue()
            reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()))
            reader.start()
            try:
                self.assertEqual(ready.get(timeout=30).strip(), f"locked {child.pid}")
                with self.assertRaises(Busy):
                    Database.open(path)
            finally:
                child.kill()
                child.wait(timeout=10)
                reader.join(timeout=10)
        self.verify(path, True)

    def test_second_crash_during_recovery_and_repeated_redo(self):
        path = self.seed("twice")
        self.crash(path, "commit_synced")
        journal = Path(str(path) + ".wal")
        saved = journal.read_bytes()
        for stage in (
            "recovery_wal_synced",
            "intent_synced",
            "page_installed",
            "checkpoint_synced",
        ):
            self.crash(path, stage, "recover")
            self.assertEqual(journal.read_bytes(), saved)
        self.verify(path, True)
        main = path.read_bytes()
        journal.write_bytes(saved)
        self.verify(path, True)
        self.assertEqual(path.read_bytes(), main)

    def test_torn_header_or_page_after_durable_commit(self):
        for name, offset, stage in [
            ("intent", 2 * PAGE_SIZE, "intent_synced"),
            ("done", PAGE_SIZE, "checkpoint_header_written"),
            ("heap", 4 * PAGE_SIZE, "page_installed"),
        ]:
            with self.subTest(name=name):
                path = self.seed(name)
                self.crash(path, stage)
                with path.open("r+b") as f:
                    f.seek(offset + 7)
                    f.write(b"!" * 30)
                self.verify(path, True)

    def test_corrupt_wal_rejected_before_any_replay(self):
        path = self.seed("malformed")
        self.crash(path, "commit_synced")
        log = Path(str(path) + ".wal")
        original = log.read_bytes()
        main = path.read_bytes()
        for offset in (
            0,
            7,
            PAGE_SIZE + 3,
            PAGE_SIZE + wal.HEAD_SIZE - 1,
            PAGE_SIZE + wal.HEAD_SIZE + 9,
            len(original) - 1,
        ):
            data = bytearray(original)
            data[offset] ^= 0x40
            log.write_bytes(data)
            with self.subTest(offset=offset), self.assertRaises(Corruption):
                Database.open(path)
            self.assertEqual(path.read_bytes(), main)
            self.assertEqual(log.read_bytes(), bytes(data))
        log.write_bytes(original)
        self.verify(path, True)

    def test_torn_done_preserves_only_good_stamp_across_failed_recovery(self):
        path = self.seed("one-stamp")
        self.crash(path, "checkpoint_header_written")
        with path.open("r+b") as f:
            f.seek(PAGE_SIZE)
            f.write(bytes(80))
        original = wal.put

        def fail(stream, offset, data):
            self.assertFalse(data.startswith(b"INTN"), "rewrote the only good stamp")
            if offset > 2 * PAGE_SIZE:
                raise OSError("recovery interrupted")
            original(stream, offset, data)

        with (
            patch.object(wal, "put", side_effect=fail),
            self.assertRaises(StorageError),
        ):
            Database.open(path)
        self.verify(path, True)

    def test_truncated_uncommitted_tail_after_prior_commits(self):
        path = self.seed("prefix")
        with Database.open(path) as db:
            for _ in range(3):
                db.execute("UPDATE accounts SET balance=100")
        self.crash(path, "commit_synced")
        log = Path(str(path) + ".wal")
        original = log.read_bytes()
        for size in (
            PAGE_SIZE + 2,
            PAGE_SIZE + wal.HEAD_SIZE - 1,
            PAGE_SIZE + wal.HEAD_SIZE + 90,
            len(original) - wal.MARK.size,
            len(original) - 8,
            len(original) - 1,
        ):
            with self.subTest(size=size):
                log.write_bytes(original[:size])
                self.verify(path, False)

    def test_incomplete_log_cannot_hide_partial_checkpoint(self):
        path = self.seed("ambiguous")
        self.crash(path, "page_installed")
        log = Path(str(path) + ".wal")
        data = log.read_bytes()
        for size in (PAGE_SIZE, len(data) - 1):
            log.write_bytes(data[:size])
            with self.assertRaises(Corruption):
                Database.open(path)
        log.write_bytes(data)
        self.verify(path, True)

    def test_wrong_file_lineage_and_missing_wal(self):
        a = self.seed("a")
        b = self.seed("b")
        self.crash(a, "commit_synced")
        log = Path(str(a) + ".wal").read_bytes()
        second = Path(str(b) + ".wal")
        second.write_bytes(log)
        with self.assertRaises(Corruption):
            Database.open(b)
        b.write_bytes(
            a.read_bytes()
        )  # Same UUID and base generation, another file identity.
        with self.assertRaisesRegex(Corruption, "file identity"):
            Database.open(b)
        self.verify(a, True)
        with Database.open(a) as db:
            db.execute("UPDATE accounts SET balance=123")
        Path(str(a) + ".wal").write_bytes(log)
        with self.assertRaisesRegex(Corruption, "stale"):
            Database.open(a)
        second.unlink()
        with self.assertRaises(StorageError):
            Database.open(b)

    def test_full_length_truncated_database_rejected(self):
        path = self.seed("short")
        self.crash(path, "commit_synced")
        path.write_bytes(path.read_bytes()[:-PAGE_SIZE])
        with self.assertRaisesRegex(Corruption, "truncated"):
            Database.open(path)

    def test_recovery_readback_failure_keeps_committed_record(self):
        path = self.seed("readback")
        self.crash(path, "commit_synced")
        journal = Path(str(path) + ".wal")
        original = journal.read_bytes()
        write = wal.put

        def miss_heap_page(stream, offset, image):
            if not image.startswith(b"DATA"):
                write(stream, offset, image)

        with (
            patch.object(wal, "put", side_effect=miss_heap_page),
            self.assertRaises(Corruption),
        ):
            Database.open(path)
        self.assertEqual(journal.read_bytes(), original)
        self.verify(path, True)

    def test_sync_failures_report_commit_outcomes(self):
        for call, outcome in enumerate(
            (
                "uncommitted",
                "unknown",
                "committed",
                "committed",
                "committed",
                "committed",
            ),
            1,
        ):
            with self.subTest(call=call):
                path = self.seed(str(call))
                with Database.open(path) as db:
                    real = os.fsync
                    calls = 0

                    def fail(fd, stop=call, real=real):
                        nonlocal calls
                        calls += 1
                        if calls == stop:
                            raise OSError("sync fault")
                        real(fd)

                    with (
                        patch.object(os, "fsync", side_effect=fail),
                        self.assertRaises(CommitError) as raised,
                    ):
                        db.execute("UPDATE accounts SET balance=20")
                    self.assertEqual(raised.exception.outcome, outcome)
                    with self.assertRaises(StorageError):
                        db.tables()
                with Database.open(path) as db:
                    balances = [
                        r["balance"]
                        for r in db.execute(
                            "SELECT balance FROM accounts WHERE balance>=0"
                        ).rows
                    ]
                    allowed = {
                        "uncommitted": [[100, 100]],
                        "unknown": [[100, 100], [20, 20]],
                        "committed": [[20, 20]],
                    }
                    self.assertIn(balances, allowed[outcome])

    def test_partial_marker_write_and_cleanup_error(self):
        for phase in ("marker", "retire"):
            path = self.seed(phase)
            real = wal.put

            def short(stream, offset, data, real=real):
                if data.startswith(b"COMMIT01"):
                    real(stream, offset, data[:12])
                    raise OSError("torn marker")
                real(stream, offset, data)

            fault = (
                patch.object(wal, "put", side_effect=short)
                if phase == "marker"
                else patch.object(wal, "retire", side_effect=OSError("cannot truncate"))
            )
            with Database.open(path) as db, fault, self.assertRaises(CommitError):
                db.execute("UPDATE accounts SET balance=42")
            with Database.open(path) as db:
                self.assertEqual(
                    db.execute("SELECT balance FROM accounts").rows,
                    [{"balance": 100 if phase == "marker" else 42}] * 2,
                )
