"""Single-owner Studio session. All database operations are serialized by the caller."""

import csv
import json
from itertools import islice
from pathlib import Path

from minidb import Database, TransactionError, ValidationError
from minidb.sql import Control, Explain, Select, parse

PAGE_ROWS = 128
RESULT_ROWS = 2000


def write_new(path, writer):
    """Exclusive creation also protects database files and hard-link aliases."""
    with Path(path).open("x", encoding="utf-8", newline="") as stream:
        try:
            writer(stream)
            stream.flush()
        except BaseException:
            stream.close()
            Path(path).unlink(missing_ok=True)
            raise


def export(path, columns, rows, kind):
    def write(stream):
        if kind == "JSON":
            json.dump(rows, stream, ensure_ascii=False, indent=2)
        elif kind == "CSV":
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        else:
            raise ValueError("Choose CSV or JSON")

    write_new(path, write)


class Session:
    def __init__(self):
        self.db = None
        self.path = ""
        self.cursor = None
        self.table = ""
        self.offset = 0
        self.integrity = ""
        self.failed = False

    def state(self):
        if self.failed:
            return "reopen required"
        return self.db.transaction_state if self.db else "disconnected"

    def snapshot(self):
        data = {
            "path": self.path,
            "state": self.state(),
            "tables": {},
            "indexes": [],
            "integrity": self.integrity,
        }
        if self.db and not self.failed:
            data.update(self.db.storage_status(), recovered=self.db.replayed_on_open)
            if data["state"] != "rollback-only":
                for name in self.db.tables():
                    data["tables"][name] = [
                        (c.name, c.kind) for c in self.db.schema(name).columns
                    ]
                    data["indexes"].extend(
                        (idx, name, col, root)
                        for idx, col, root in self.db.indexes(name)
                    )
        return data

    def close(self, discard=False):
        if self.db:
            if self.state() in ("active", "rollback-only") and not discard:
                raise TransactionError("Resolve the active transaction before closing.")
            self.db.close()
        self.__init__()

    def connect(self, path, create=False, discard=False):
        path = str(Path(path).absolute())
        if self.db and Path(path) == Path(self.path):
            if create:
                raise FileExistsError("Choose a new database filename.")
            self.close(discard)
        candidate = Database.create(path) if create else Database.open(path)
        try:
            self.close(discard)
        except BaseException:
            candidate.close()
            raise
        self.db, self.path = candidate, path
        self.integrity = "Passed on open"
        return (
            "Created database"
            if create
            else (
                "Recovered committed WAL and opened database"
                if candidate.replayed_on_open
                else "Opened database"
            )
        )

    def migrate(self, destination):
        if not self.db or self.db.storage_status()["format"] != 1:
            raise ValidationError("Migration requires an open v1 database.")
        source = self.path
        self.close()
        try:
            Database.migrate(source, destination)
            self.connect(destination)
        except Exception:
            self.connect(source)
            raise
        return "Migrated and verified destination"

    def check(self):
        if self.state() != "idle":
            raise TransactionError("Finish the transaction before checking integrity.")
        path = self.path
        self.close()
        self.connect(path)
        self.integrity = "Passed explicit integrity check"
        return self.integrity

    def browse(self, name, advance=False):
        if not self.db:
            raise TransactionError("Open a database first.")
        if not advance or self.table != name or self.cursor is None:
            self.cursor = self.db.scan(name)
            self.offset = 0
            self.table = name
        fields = [col.name for col in self.db.schema(name).columns]
        rows = [
            {"Record ID": f"{rid.page}:{rid.slot}", **row}
            for rid, row in islice(self.cursor, PAGE_ROWS)
        ]
        self.offset += len(rows)
        return {
            "columns": ["Record ID", *fields],
            "rows": rows,
            "offset": self.offset - len(rows),
            "more": len(rows) == PAGE_ROWS,
            "table": name,
        }

    def sql(self, statement):
        if not self.db or self.failed:
            raise TransactionError("Reopen the database before running SQL.")
        result = self.db.execute(statement, max_result_rows=RESULT_ROWS)
        ast, _ = parse(statement)
        changed = not isinstance(ast, (Select, Explain))
        if changed:
            self.cursor = None
        outcome = (
            {
                "COMMIT": "Committed",
                "ROLLBACK": "Rolled back",
                "BEGIN": "Transaction started",
            }.get(ast.command, "")
            if isinstance(ast, Control)
            else ""
        )
        return {
            "columns": result.columns,
            "rows": result.rows,
            "count": result.rowcount,
            "plan": result.plan,
            "plan_sql": statement if result.plan else "",
            "changed": changed,
            "message": outcome
            or (
                f"{result.rowcount} rows returned"
                if isinstance(ast, Select)
                else (
                    "Plan ready"
                    if result.plan
                    else f"Statement complete · {result.rowcount} rows affected"
                )
            ),
        }
