"""Exclusive ownership, transaction-local page images, and synchronous redo checkpoints."""

import errno
import os
import stat
import struct
import sys
import threading
import uuid
from pathlib import Path
from typing import BinaryIO, Literal

from . import wal
from .binary import PAGE_SIZE, U32, seal, verify
from .errors import (
    Busy,
    CommitError,
    Corruption,
    NoSpace,
    StorageError,
    TransactionError,
    UnsupportedVersion,
)

HEADER = struct.Struct(">8sHHI")


def _lock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise Busy("database is in use") from exc
        raise


def _regular(path: str, flags: int) -> int:
    fd = os.open(path, flags | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StorageError("database path must refer to a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _directory_sync(path: Path) -> None:
    if sys.platform != "win32":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Pager:
    def __init__(
        self, path: str | Path, *, create: bool, source: "Pager | None" = None
    ):
        self.failed = False
        self.pid = os.getpid()
        self.thread = threading.current_thread()
        self.legacy = False
        self.count = 1
        self.overlay: dict[int, bytes] | None = None
        self.recovery: wal.Redo | None = None
        self.journal: BinaryIO | None = None
        self.stamp = wal.Stamp(0, 1, bytes(16))
        try:
            supplied = Path(path)
            location = supplied.absolute() if create else supplied.resolve()
            mode: Literal["x+b", "r+b"] = "x+b" if create else "r+b"
            self.file = open(location, mode, buffering=0, opener=_regular)  # noqa: SIM115
            try:
                _lock(self.file.fileno())
                if create:
                    self.identity = uuid.uuid4().bytes
                else:
                    self._identify()
                if not self.legacy:
                    self.owner = wal.file_identity(self.file)
                    if os.fstat(self.file.fileno()).st_nlink > 1:
                        raise StorageError(
                            "transactional databases require one main-file name"
                        )
                    wal_path = Path(str(location) + ".wal")
                    if wal_path.is_symlink():
                        raise StorageError("WAL must be a regular companion file")
                    if create:
                        wal.boundary("before_wal_create")
                    self.journal = open(wal_path, mode, buffering=0, opener=_regular)  # noqa: SIM115
                    _lock(self.journal.fileno())
                    if os.fstat(self.journal.fileno()).st_nlink > 1:
                        raise StorageError("WAL must have one file-system link")
                    if create:
                        _directory_sync(location.parent)
                        self._bootstrap(source)
                    else:
                        self._inspect()
            except BaseException:
                self.close()
                raise
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(
                f"cannot {'create' if create else 'open'} database: {exc}"
            ) from exc

    def _bootstrap(self, source: "Pager | None") -> None:
        assert self.journal is not None
        self.count = source.count if source is not None else 1
        self.stamp = wal.Stamp(0, self.count, uuid.uuid4().bytes)
        wal.put(self.journal, 0, wal.preamble(self.identity))
        wal.sync(self.journal, "preamble_synced")
        wal.put(self.file, PAGE_SIZE, wal.stamp_page(b"DONE", self.stamp))
        wal.put(self.file, 2 * PAGE_SIZE, wal.stamp_page(b"INTN", self.stamp))
        if source is not None:
            for page in range(1, self.count):
                wal.put(self.file, (page + 2) * PAGE_SIZE, seal(source.read(page)))
        wal.sync(self.file, "bootstrap_synced")
        wal.put(
            self.file,
            0,
            seal(wal.ANCHOR.pack(wal.MAGIC, wal.VERSION, PAGE_SIZE, self.identity)),
        )
        wal.sync(self.file, "anchor_synced")

    def _identify(self) -> None:
        body = verify(wal.get(self.file, 0, PAGE_SIZE))
        magic, version, size, identity = wal.ANCHOR.unpack_from(body)
        if magic != wal.MAGIC:
            raise Corruption("invalid MiniDB signature")
        if version not in (1, wal.VERSION):
            raise UnsupportedVersion(f"unsupported format version {version}")
        if size != PAGE_SIZE:
            raise Corruption("invalid page size")
        if version == 1:
            self.legacy = True
            self.count = HEADER.unpack_from(body)[3]
            if (
                not self.count
                or any(body[HEADER.size :])
                or wal.length(self.file) != self.count * PAGE_SIZE
            ):
                raise Corruption("invalid legacy header or file length")
        elif any(body[wal.ANCHOR.size :]) or not any(identity):
            raise Corruption("invalid database identity anchor")
        self.identity = identity

    def _inspect(self) -> None:
        assert self.journal is not None
        done = wal.read_stamp(self.file, 1, b"DONE")
        intent = wal.read_stamp(self.file, 2, b"INTN")
        tx = wal.inspect(self.journal, self.identity, self.owner, done, intent)
        size = wal.length(self.file)
        if tx is None:
            if done is None or done != intent or size != (done.count + 2) * PAGE_SIZE:
                raise Corruption(
                    "checkpoint stamps or file length need a committed WAL"
                )
            self.stamp = done
        else:
            if done is None and intent != tx.target:
                raise Corruption("cannot establish checkpoint lineage")
            if done == tx.target and intent == tx.base:
                raise Corruption("inconsistent checkpoint ordering")
            if (
                not (tx.base.count + 2) * PAGE_SIZE
                <= size
                <= (tx.target.count + 2) * PAGE_SIZE
            ):
                raise Corruption("database truncated outside replay range")
            self.stamp = tx.target
            self.overlay = tx.pages
            self.recovery = tx
        self.count = self.stamp.count

    def finish_open(self) -> None:
        """Called only after validation of the complete prospective database."""
        if self.journal is None:
            return
        try:
            if self.recovery is not None:
                wal.sync(self.journal, "recovery_wal_synced")
                self.recovery.checkpoint(self.file, recovering=True)
            if wal.length(self.journal) != PAGE_SIZE:
                wal.retire(self.journal)
            self.overlay = None
            self.recovery = None
        except OSError as exc:
            raise StorageError(f"cannot finish recovery: {exc}") from exc

    def check(self) -> None:
        if self.file.closed or self.failed:
            raise StorageError("database is closed or unavailable after an I/O failure")
        if self.pid != os.getpid() or self.thread is not threading.current_thread():
            raise StorageError("use this handle in its opening process and thread")

    def read(self, page_id: int) -> bytes:
        self.check()
        if not 0 < page_id < self.count:
            raise Corruption("page outside file bounds")
        if self.overlay is not None and page_id in self.overlay:
            return verify(self.overlay[page_id])
        try:
            physical = page_id if self.legacy else page_id + 2
            return verify(wal.get(self.file, physical * PAGE_SIZE, PAGE_SIZE))
        except OSError as exc:
            raise StorageError(f"cannot read page {page_id}: {exc}") from exc

    def begin(self) -> None:
        self.check()
        if self.legacy:
            raise TransactionError(
                "v1 database is read-only; migrate to a new path first"
            )
        if self.overlay is not None:
            raise TransactionError("transaction already active")
        self.overlay = {}

    def write(self, page_id: int, data: bytes) -> None:
        self.check()
        if self.overlay is None or self.legacy:
            raise TransactionError("page write requires an active transaction")
        if not 0 < page_id < self.count or len(data) != PAGE_SIZE:
            raise ValueError("invalid page write")
        if U32.unpack_from(verify(data), 4)[0] != page_id:
            raise Corruption("staged page has the wrong logical identifier")
        if page_id not in self.overlay and len(self.overlay) >= wal.MAX_FRAMES:
            raise NoSpace("transaction exceeds dirty page limit")
        self.overlay[page_id] = data

    def append(self, data: bytes) -> None:
        if self.count == 0xFFFFFFFF:
            raise NoSpace("page identifier space exhausted")
        page = self.count
        self.count += 1
        try:
            self.write(page, data)
        except BaseException:
            self.count = page
            raise

    def rollback(self) -> None:
        self.check()
        self.overlay = None
        self.count = self.stamp.count

    def commit(self) -> None:
        self.check()
        assert self.journal is not None and self.overlay is not None
        if not self.overlay:
            self.overlay = None
            return
        tx = wal.Redo(self.stamp, self.stamp.next(self.count), self.overlay)
        outcome = "uncommitted"
        try:
            offset, marker = tx.prepare(self.journal, self.identity, self.owner)
            wal.boundary("before_marker")
            outcome = "unknown"
            wal.put(self.journal, offset, marker)
            wal.boundary("marker_written")
            os.fsync(self.journal.fileno())
            outcome = "committed"
            wal.boundary("commit_synced")
            tx.checkpoint(self.file)
            wal.retire(self.journal)
        except BaseException as exc:
            self.failed = True
            if isinstance(exc, Exception):
                raise CommitError(outcome, exc) from exc
            raise
        self.stamp = tx.target
        self.overlay = None

    def close(self) -> None:
        if self.file.closed:
            return
        if self.pid != os.getpid() or self.thread is not threading.current_thread():
            raise StorageError("close this handle in its opening process and thread")
        try:
            try:
                if self.journal is not None:
                    self.journal.close()
            finally:
                self.file.close()
        except OSError as exc:
            raise StorageError(f"cannot close database: {exc}") from exc
