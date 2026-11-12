"""Exceptions exposed by the storage API."""


class DatabaseError(Exception):
    """Base class for expected MiniDB errors."""


class ValidationError(DatabaseError):
    """Invalid user-supplied name, schema, value, or identifier."""


class NotFound(DatabaseError):
    """The table or live record does not exist."""


class NoSpace(DatabaseError):
    """The record or schema exceeds its available physical space."""


class Corruption(DatabaseError):
    """Stored bytes violate the format invariants."""


class UnsupportedVersion(DatabaseError):
    """The file belongs to a different format version."""


class StorageError(DatabaseError):
    """Filesystem, locking, or lifecycle failure."""


class Busy(StorageError):
    """Another handle holds the database lock."""


class TransactionError(DatabaseError):
    """Invalid transaction state or use of an unmigrated file."""


class CommitError(StorageError):
    """Commit failed. Reopen to recover before deciding whether to retry."""

    def __init__(self, outcome: str, cause: BaseException):
        self.outcome = outcome
        super().__init__(f"commit outcome {outcome}; close and reopen: {cause}")
