"""Disposable end-to-end smoke check inside the actual frozen Windows executable.

This is intentionally executed in a new process, with no personal databases.
It tests real Qt widgets, the owner thread, file-backed MiniDB operations,
transaction rollback, query plans, settings persistence and clean shutdown.
"""

import json
import logging
import time
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtWidgets import QApplication

from .settings import Settings
from .window import Window


def until(app, predicate, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.015)
    raise TimeoutError("Frozen Studio operation or shutdown did not finish")


def exercise():
    with TemporaryDirectory(prefix="minidb-portable-smoke-") as directory:
        root = Path(directory)
        app = QApplication.instance() or QApplication([])
        app.setOrganizationName("MiniDB")
        app.setApplicationName("MiniDB Studio")
        app.setQuitOnLastWindowClosed(False)
        prefs_file = root / "studio.json"
        win = Window(Settings(prefs_file))
        win.show()

        try:
            until(app, lambda: win.isVisible())
            if win.stack.currentIndex() != 0 or win.snap["path"]:
                raise AssertionError("First launch is not an empty workspace")

            def command(name, *args):
                if not win.submit(name, *args):
                    raise AssertionError(f"Command refused: {name}")
                until(app, lambda: not win.busy)
                if win.notice.text():
                    raise AssertionError(f"{name}: {win.notice.text()}")

            database = root / "smoke.mdb"
            command("open", str(database), True, True)
            command("sql", "CREATE TABLE accounts (id INT, balance INT)")
            command("sql", "INSERT INTO accounts VALUES (1, 100)")
            command("sql", "BEGIN")
            command("sql", "UPDATE accounts SET balance=0 WHERE id=1")
            command("sql", "ROLLBACK")
            command("sql", "CREATE INDEX accounts_id ON accounts(id)")
            command("sql", "EXPLAIN SELECT balance FROM accounts WHERE id=1")
            if "accounts_id" not in win.plan.toPlainText():
                raise AssertionError("Index plan did not identify accounts_id")
            command("sql", "SELECT balance FROM accounts WHERE id=1")
            rows = win.results.dataset.rows
            if rows != [{"balance": 100}]:
                raise AssertionError(f"Unexpected post-rollback rows: {rows!r}")
            command("check")
            if win.snap["state"] != "idle":
                raise AssertionError("Session not idle after integrity check")

            win.settings.values["theme"] = "Light"
            win.persist()
            win.close()
            until(app, lambda: win.stopped)
            if win.thread.isRunning() or win.isVisible():
                raise AssertionError("GUI thread or window survived closing")
            if Settings(prefs_file).values["theme"] != "Light":
                raise AssertionError("Preferences were not persisted")
            if not database.exists():
                raise AssertionError("Database file was not created")

            return {
                "passed": True,
                "checks": [
                    "frozen_executable_startup",
                    "real_window_and_empty_workspace",
                    "database_create_and_sql",
                    "transaction_rollback",
                    "persistent_btree_explain",
                    "query_results",
                    "integrity_check",
                    "preferences_persistence",
                    "clean_qt_and_database_thread_shutdown",
                ],
            }
        finally:
            if not win.stopped:
                if win.snap.get("state") in ("active", "rollback-only"):
                    # A failed test must not forcibly commit or destroy user data.
                    # Only disposable test data exists in this temporary folder.
                    try:
                        win.submit("sql", "ROLLBACK")
                        until(app, lambda: not win.busy, 5)
                    except Exception:
                        logging.getLogger("minidb.studio.smoke").exception(
                            "Rollback cleanup failed"
                        )
                win.editor.document().setModified(False)
                win.close()
                try:
                    until(app, lambda: win.stopped, 8)
                except TimeoutError:
                    pass


def run(report_path):
    result = None
    try:
        result = exercise()
    except Exception as exc:  # noqa: BLE001 - emit structured failure for any test exception
        result = {
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        # A failure to write an artifact is itself a failed smoke verification.
        return 1
    return 0 if result["passed"] else 1
