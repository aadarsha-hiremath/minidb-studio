"""One complete application lifetime, invoked in four separate processes."""

import faulthandler
import json
import sys
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog
from test_studio import until

from minidb_studio.dialogs import Preferences
from minidb_studio.settings import DEFAULTS, Settings
from minidb_studio.window import Window


def main():
    faulthandler.enable()
    root = Path(sys.argv[1])
    mode = sys.argv[2]
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    prefs = Settings(root / "restart.json")
    win = Window(prefs)
    win.show()
    app.processEvents()
    path = str(root / "prefs.mdb")

    def go(name, *args):
        assert win.submit(name, *args)
        until(lambda: not win.busy)
        assert not win.notice.text(), win.notice.text()

    if mode == "save":
        go("open", path, True)
        go("sql", "CREATE TABLE settings_test(n INT, s TEXT)")
        go("sql", "INSERT INTO settings_test VALUES(1,'record-secret')")
        go("sql", "SELECT * FROM settings_test")

        def choose(dialog):
            dialog.theme.setCurrentText("Light")
            dialog.density.setCurrentText("Compact")
            dialog.wrap.setChecked(True)
            dialog.motion.setChecked(False)
            dialog.size.setValue(12)
            return QDialog.DialogCode.Accepted

        with patch.object(Preferences, "exec", choose):
            win.preferences()
        win.resize(900, 570)
        win.inspect("picked")
        win.panels.setSizes([175, 525, 180])
        win.sql_split.setSizes([180, 225])
        win.results.setColumnWidth(0, 192)
        win.results.visibility(1, False)
        win.persist()
        (root / "geometry.json").write_text(
            json.dumps([win.panels.sizes(), win.sql_split.sizes()])
        )
    elif mode == "read":
        assert prefs.values["recent"] == [path]
        assert win.stack.currentIndex() == 0
        assert prefs.values["theme"] == "Light" and prefs.values["density"] == "Compact"
        assert (
            prefs.values["font_size"] == 12
            and prefs.values["wrap"]
            and not prefs.values["reduced_motion"]
        )
        assert win.editor.font().family() == prefs.values["editor_font"]
        assert [win.width(), win.height()] == [900, 570]
        assert "record-secret" not in prefs.path.read_text()
        previous = json.loads((root / "geometry.json").read_text())
        assert all(
            abs(a - b) < 5 for a, b in zip(prefs.values["sql_split"], previous[1])
        )
        assert abs(prefs.values["panels"][2] - previous[0][2]) < 5
        go("open", path)
        go("sql", "SELECT * FROM settings_test")
        assert win.results.columnWidth(0) == 192 and win.results.isColumnHidden(1)
    elif mode == "reset":

        def reset(dialog):
            dialog.reset_requested = True
            return QDialog.DialogCode.Accepted

        with patch.object(Preferences, "exec", reset):
            win.preferences()
    else:
        for key in (
            "theme",
            "density",
            "font_size",
            "editor_font",
            "wrap",
            "reduced_motion",
            "recent",
            "sidebar",
            "inspector",
            "columns",
            "window",
            "sql_split",
        ):
            assert prefs.values[key] == DEFAULTS[key], (key, prefs.values[key])
    win.close()
    until(lambda: win.stopped)
    assert win.thread.wait(5000) and win.thread.isFinished()
    # Dispose the stopped Qt window before QApplication/interpreter teardown.
    # Relying on Python's shutdown order can leave binding-owned Qt objects
    # alive after their QApplication has begun destruction.
    win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


if __name__ == "__main__":
    main()
