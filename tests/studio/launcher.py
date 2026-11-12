"""Start the installed GUI entry point with its actual event loop and close it cleanly."""

from importlib.metadata import distribution
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QTimer

from minidb_studio.settings import Settings
from minidb_studio.window import Window


def main():
    entry = next(
        e
        for e in distribution("minidb-engine").entry_points
        if e.name == "minidb-gui" and e.group == "gui_scripts"
    )
    windows = []
    with TemporaryDirectory() as root:

        class StartupWindow(Window):
            def __init__(self):
                super().__init__(Settings(Path(root) / "prefs.json"))
                windows.append(self)
                QTimer.singleShot(0, self.inspect_and_close)

            def inspect_and_close(self):
                assert self.isVisible() and self.stack.currentIndex() == 0
                self.close()

        with patch("minidb_studio.window.Window", StartupWindow):
            assert entry.load()() == 0
    # The event loop may exit before an independent observer of finished()
    # receives a queued callback. Inspect the actual window and owner thread.
    (window,) = windows
    assert window.stopped and not window.isVisible()
    assert window.thread.wait(5000) and window.thread.isFinished()


if __name__ == "__main__":
    main()
