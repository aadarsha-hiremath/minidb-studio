"""Installed-package Qt integration tests. All files and preferences are disposable."""

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from minidb import Busy, Database, wal
from minidb.binary import PAGE_SIZE, seal
from minidb.pager import HEADER
from minidb_studio.settings import Settings
from minidb_studio.window import Window


def until(predicate):
    from time import monotonic

    end = monotonic() + 30
    while not predicate() and monotonic() < end:
        QApplication.processEvents()
        QTest.qWait(4)
    if not predicate():
        raise AssertionError("timed out waiting for Studio")


class StudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.app.setStyle("Fusion")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "studio.mdb"
        self.win = Window(Settings(self.root / "settings.json"))
        self.win.show()
        QApplication.processEvents()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        if not self.win.stopped:
            until(lambda: not self.win.busy)
            self.win.editor.document().setModified(False)
            with patch.object(self.win, "confirm", return_value=True):
                self.win.close()
                until(lambda: self.win.stopped)

    def go(self, name, *args, ok=True):
        self.assertTrue(self.win.submit(name, *args))
        until(lambda: not self.win.busy)
        if ok:
            self.assertFalse(self.win.notice.text(), self.win.notice.text())
        else:
            self.assertTrue(self.win.notice.text())

    def create(self):
        self.go("open", str(self.path), True)
        self.go("sql", "CREATE TABLE notes(id INT, body TEXT, flag BOOL)")

    def query(self, text):
        self.win.editor.setPlainText(text)
        self.win.editor.setFocus()
        QTest.keyClick(
            self.win.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier
        )
        until(lambda: not self.win.busy)

    def test_empty_and_sql_shortcut_selection_and_invalid_sql(self):
        self.assertEqual(self.win.stack.currentIndex(), 0)
        self.assertFalse(self.win.run_btn.isEnabled())
        self.create()
        self.query("INSERT INTO notes VALUES(9, 'A', TRUE)")
        self.query("SELECT * FROM notes")
        self.assertEqual(
            self.win.results.dataset.rows, [{"id": 9, "body": "A", "flag": True}]
        )
        self.query("SELECT * FROM notes; DELETE FROM notes")
        self.assertIn("expected", self.win.notice.text())
        self.assertIn("DELETE", self.win.editor.toPlainText())
        self.win.editor.selectAll()
        self.win.editor.insertPlainText("SELECT id FROM notes\nDELETE")
        cursor = self.win.editor.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(20, cursor.MoveMode.KeepAnchor)
        self.win.editor.setTextCursor(cursor)
        self.win.sel_btn.click()
        until(lambda: not self.win.busy)
        self.assertEqual(self.win.results.dataset.rows, [{"id": 9}])

    def test_lock_and_failed_switch_preserves_session(self):
        self.create()
        with self.assertRaises(Busy):
            Database.open(self.path)
        bad = self.root / "bad.mdb"
        bad.write_bytes(b"bad")
        self.go("open", str(bad), False, False, ok=False)
        self.assertEqual(self.win.snap["path"], str(self.path))
        self.go("open", str(self.path), True, ok=False)
        self.go("sql", "SELECT * FROM notes")
        self.assertEqual(self.win.results.dataset.columns, ["id", "body", "flag"])

    def test_transaction_buttons_rollback_only_switch_and_rollback(self):
        self.create()
        self.win.tx_buttons["Begin"].click()
        until(lambda: not self.win.busy)
        self.go("sql", "INSERT INTO notes VALUES(1,'pending',TRUE)")
        other = self.root / "other.mdb"
        with Database.create(other):
            pass
        with patch.object(self.win, "confirm", return_value=False):
            self.win.open_path(str(other))
        self.assertEqual(self.win.snap["state"], "active")
        self.go("sql", "bad statement", ok=False)
        self.assertEqual(self.win.snap["state"], "rollback-only")
        self.assertFalse(self.win.tx_buttons["Commit"].isEnabled())
        with patch.object(self.win, "confirm", return_value=True):
            self.win.tx_buttons["Rollback"].click()
        until(lambda: not self.win.busy)
        self.go("sql", "SELECT * FROM notes")
        self.assertEqual(self.win.results.dataset.rows, [])
        self.go("sql", "BEGIN")
        self.go("sql", "INSERT INTO notes VALUES(2,'kept',TRUE)")
        self.win.tx_buttons["Commit"].click()
        until(lambda: not self.win.busy)
        self.assertIn("Committed", self.win.last.text())
        self.go("open", str(other))
        with Database.open(self.path) as db:
            self.assertEqual(db.execute("SELECT id FROM notes").rows, [{"id": 2}])

    def test_real_unknown_commit_requires_reopen(self):
        self.create()
        self.go("sql", "BEGIN")
        self.go("sql", "INSERT INTO notes VALUES(7,'uncertain',FALSE)")

        def fail(stage):
            if stage == "marker_written":
                raise OSError("injected failure before marker sync")

        with patch.object(wal, "boundary", fail):
            self.go("sql", "COMMIT", ok=False)
        self.assertIn("outcome: unknown", self.win.notice.text())
        self.assertFalse(self.win.run_btn.isEnabled())
        self.go("open", str(self.path))
        self.assertTrue(self.win.snap["recovered"])
        self.go("sql", "SELECT id FROM notes")
        self.assertEqual(self.win.results.dataset.rows, [{"id": 7}])

    def test_explorer_index_plan_check_and_unicode_exports(self):
        self.create()
        s = 'line\nsecond, "naïve"'
        self.go("sql", f"INSERT INTO notes VALUES(2,'{s}',TRUE)")
        self.go("sql", "CREATE INDEX note_id ON notes(id)")
        self.assertEqual(self.win.index_grid.dataset.rows[0]["Column"], "id")
        item = self.win.tree.findItems(
            "notes", Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive
        )[0]
        self.win.navigate(item)
        until(lambda: not self.win.busy)
        self.assertEqual(self.win.data.dataset.rows[0]["Record ID"], "2:0")
        self.win.navigate(item.child(1))
        self.assertEqual(self.win.tabs.currentIndex(), 2)
        self.assertIn("body", self.win.inspect_text.toPlainText())
        self.go("sql", "EXPLAIN SELECT * FROM notes WHERE id=2")
        self.assertIn("using note_id", self.win.plan.toPlainText())
        self.go("sql", "SELECT * FROM notes")
        g = self.win.results
        g.setCurrentIndex(g.dataset.index(0, 1))
        g.copy_value()
        self.assertEqual(QApplication.clipboard().text(), s)
        g.copy_rows()
        self.assertIn("naïve", QApplication.clipboard().text())
        for kind in ("JSON", "CSV"):
            path = self.root / f"out.{kind}"
            self.go("export", str(path), g.dataset.columns, g.dataset.rows, kind)
            with path.open(encoding="utf-8", newline="") as f:
                if kind == "JSON":
                    self.assertEqual(json.load(f), g.dataset.rows)
                else:
                    self.assertEqual(next(iter(csv.DictReader(f)))["body"], s)
            self.go("export", str(path), g.dataset.columns, [], kind, ok=False)
        self.go("sql", "DROP INDEX note_id")
        self.go("check")
        self.assertEqual(self.win.snap["indexes"], [])
        self.assertIn("Passed explicit", self.win.health.toPlainText())

    def test_large_grid_windows_and_bounded_sorted_index_results(self):
        with Database.create(self.path) as db, db.transaction():
            db.execute("CREATE TABLE numbers(i INT, rank INT)")
            for i in range(2010):
                db.insert("numbers", {"i": i, "rank": i % 5})
            db.execute("CREATE INDEX by_rank ON numbers(rank)")
            sql = "SELECT i FROM numbers WHERE rank>=0 ORDER BY rank DESC LIMIT 19"
            wanted = db.execute(sql).rows
        self.go("open", str(self.path))
        self.go("browse", "numbers")
        self.assertEqual(len(self.win.data.dataset.rows), 128)
        self.go("browse", "numbers", True)
        self.assertEqual(self.win.data.dataset.rows[0]["i"], 128)
        self.go("sql", "SELECT * FROM numbers", ok=False)
        self.assertIn("2000", self.win.notice.text())
        self.go("sql", sql)
        self.assertEqual(self.win.results.dataset.rows, wanted)

    def test_legacy_migration_preserves_original_and_verifies_destination(self):
        legacy = seal(HEADER.pack(b"MINIDB\r\n", 1, PAGE_SIZE, 1))
        self.path.write_bytes(legacy)
        self.go("open", str(self.path))
        self.assertTrue(self.win.migrate_btn.isVisible())
        destination = self.root / "current.mdb"
        self.go("migrate", str(destination))
        self.assertEqual(self.win.snap["format"], 3)
        self.assertEqual(self.path.read_bytes(), legacy)
        self.assertEqual(self.win.snap["path"], str(destination))

    def test_close_during_operation_and_skip_rapid_duplicate(self):
        self.assertTrue(self.win.submit("open", str(self.path), True))
        self.assertFalse(self.win.submit("sql", "CREATE TABLE raced(i INT)"))
        self.win.close()
        until(lambda: self.win.stopped)
        with Database.open(self.path) as db:
            self.assertEqual(db.tables(), ())

    def test_cancel_shutdown_with_unsaved_sql(self):
        self.create()
        self.win.editor.insertPlainText("unfinished")
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel
        ):
            self.win.close()
        self.assertTrue(self.win.isVisible())
        self.assertEqual(self.win.editor.toPlainText(), "unfinished")

    def test_exit_while_disconnect_pending_still_protects_sql(self):
        self.create()
        self.win.editor.insertPlainText("preserve this draft")
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel
        ) as prompt:
            self.win.disconnect()
            self.win.close()
            until(lambda: prompt.called)
        self.assertFalse(self.win.stopped)
        self.assertEqual(self.win.editor.toPlainText(), "preserve this draft")

    def test_committed_process_crash_replays_on_studio_open(self):
        with Database.create(self.path) as db:
            db.execute("CREATE TABLE pending_samples(sample INT)")
        demo = Path(__file__).resolve().parents[2] / "benchmarks" / "studio.py"
        child = subprocess.run(
            [sys.executable, str(demo), "--recover-child", str(self.path)],
            check=False,
            timeout=20,
        )
        self.assertEqual(child.returncode, 86)
        self.go("open", str(self.path))
        self.assertIn("Recovered committed WAL", self.win.last.text())
        self.go("sql", "SELECT sample FROM pending_samples")
        self.assertEqual(self.win.results.dataset.rows, [{"sample": 42}])

    def test_preferences_restart_process_and_reset(self):
        helper = Path(__file__).with_name("restart.py")
        for operation in ("save", "read", "reset", "defaults"):
            subprocess.run(
                [sys.executable, str(helper), str(self.root), operation],
                env={**os.environ, "QT_SCALE_FACTOR": "1.5"},
                check=True,
                timeout=40,
            )

    def test_one_invalid_preference_keeps_valid_preferences(self):
        path = self.root / "prefs.json"
        path.write_text('{"theme":"Light","font_size":"bad","recent":null}')
        prefs = Settings(path).values
        self.assertEqual(
            (prefs["theme"], prefs["font_size"], prefs["recent"]), ("Light", 10, [])
        )
