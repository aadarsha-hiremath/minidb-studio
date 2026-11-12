"""Optional native MiniDB Studio launcher."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def main():
    try:
        from PySide6.QtCore import QStandardPaths
        from PySide6.QtWidgets import QApplication
    except ImportError:
        message = 'Install the Studio extra with: python -m pip install ".[gui]"'
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        elif sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "MiniDB Studio", 0x10)
        return 1
    from .window import Window

    app = QApplication(sys.argv)
    app.setOrganizationName("MiniDB")
    app.setApplicationName("MiniDB Studio")
    app.setStyle("Fusion")
    directory = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "studio.log", maxBytes=100000, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logging.getLogger("minidb.studio").addHandler(handler)
    except OSError:
        pass
    window = Window()
    window.show()
    return app.exec()
