"""A queued worker owns the sole database session from open through close."""

import logging
from queue import SimpleQueue
from time import perf_counter

from PySide6.QtCore import QObject, QThread, Signal, Slot

from minidb import CommitError, Corruption, DatabaseError

from .service import Session, export


class SessionThread(QThread):
    """Keep one Python thread state alive for the entire engine session."""

    def __init__(self, worker, parent):
        super().__init__(parent)
        self.worker = worker
        self.commands = SimpleQueue()

    @Slot(str, object)
    def enqueue(self, name, args):
        self.commands.put((name, args))

    def run(self):
        while (command := self.commands.get()) is not None:
            self.worker.run(*command)

    def stop(self):
        self.commands.put(None)


class Worker(QObject):
    completed = Signal(object)

    def __init__(self):
        super().__init__()
        self.session = Session()

    @Slot(str, object)
    def run(self, name, args):
        start = perf_counter()
        reply = {"name": name, "error": "", "data": None}
        try:
            if name == "export":
                export(*args)
                reply["data"] = "Export complete"
            else:
                action = {
                    "open": self.session.connect,
                    "close": self.session.close,
                    "sql": self.session.sql,
                    "browse": self.session.browse,
                    "check": self.session.check,
                    "migrate": self.session.migrate,
                    "refresh": lambda: "Refreshed metadata",
                }[name]
                reply["data"] = action(*args)
        except Exception as exc:  # noqa: BLE001 - always return control to the event loop
            logging.getLogger("minidb.studio").warning(
                "%s failed (%s)", name, type(exc).__name__
            )
            reply["error"] = (
                str(exc)
                if isinstance(exc, (OSError, DatabaseError, ValueError))
                else "Operation failed unexpectedly. See the diagnostic log."
            )
            if isinstance(exc, CommitError):
                self.session.failed = True
                reply["error"] = (
                    f"Commit outcome: {exc.outcome}. Reopen and inspect the database before retrying."
                )
            if isinstance(exc, Corruption):
                reply["error"] = "Integrity failure: " + str(exc)
                if name not in ("open", "migrate"):
                    self.session.failed = bool(self.session.db)
        try:
            reply["snapshot"] = self.session.snapshot()
        except (OSError, DatabaseError):
            self.session.failed = True
            reply["snapshot"] = self.session.snapshot()
            reply["error"] = (
                reply["error"] or "Database status unavailable. Reopen for recovery."
            )
        reply["ms"] = (perf_counter() - start) * 1000
        self.completed.emit(reply)
