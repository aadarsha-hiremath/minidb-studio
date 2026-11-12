"""One command per open/close cycle. JSON is used only for terminal output."""

import argparse
import json
import sys
from collections.abc import Sequence

from . import (
    Column,
    Database,
    DatabaseError,
    RecordID,
    Result,
    Schema,
    TransactionError,
    ValidationError,
    __version__,
)
from .schema import Value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="minidb", description="Inspect and edit MiniDB files."
    )
    root.add_argument("--version", action="version", version=f"MiniDB {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create", "create a new database file"),
        ("tables", "open a database and list tables"),
        ("create-table", "define a typed table"),
        ("schema", "show columns and types"),
        ("insert", "insert a row and print its page:slot"),
        ("get", "retrieve a live row"),
        ("update", "replace all values of a live row"),
        ("delete", "delete a live row"),
        ("scan", "stream live rows in page and slot order"),
    ):
        cmd = commands.add_parser(name, help=help_text, description=help_text)
        cmd.add_argument("file", help="database file path")
        if name in ("create", "tables"):
            continue
        cmd.add_argument("table")
        if name == "create-table":
            cmd.add_argument(
                "columns", metavar="NAME:TYPE", nargs="+", help="int, text, or bool"
            )
        if name in ("get", "update", "delete"):
            cmd.add_argument("rid", metavar="PAGE:SLOT")
        if name in ("insert", "update"):
            cmd.add_argument("values", metavar="NAME=VALUE", nargs="+")
    sql = commands.add_parser("sql", help="execute one typed SQL statement")
    sql.add_argument("file", help="database file path")
    sql.add_argument("statement", help="one SQL statement")
    check = commands.add_parser(
        "check", help="validate every page, index, and live index reference"
    )
    check.add_argument("file", help="database file path")
    session = commands.add_parser(
        "session", help="run one SQL statement per input line on one handle"
    )
    session.add_argument("file")
    migrate = commands.add_parser(
        "migrate", help="validate and copy a v1 file to a new transactional database"
    )
    migrate.add_argument("file", help="original v1 file")
    migrate.add_argument("destination", help="new database path")
    status = commands.add_parser(
        "status", help="recover, validate and show transaction status"
    )
    status.add_argument("file")
    return root


def _rid(text: str) -> RecordID:
    try:
        page, slot = text.split(":")
        return RecordID(int(page), int(slot))
    except ValueError as exc:
        raise ValidationError("expected PAGE:SLOT with decimal integers") from exc


def _values(schema: Schema, fields: list[str]) -> dict[str, Value]:
    kinds = {c.name: c.kind for c in schema.columns}
    row: dict[str, Value] = {}
    for field in fields:
        key, separator, value = field.partition("=")
        if not separator or key not in kinds or key in row:
            raise ValidationError(f"invalid or duplicate field {key!r}")
        if kinds[key] == "int":
            try:
                row[key] = int(value)
            except ValueError as exc:
                raise ValidationError(f"{key} requires an integer") from exc
        elif kinds[key] == "bool":
            if value not in ("true", "false"):
                raise ValidationError(f"{key} requires true or false")
            row[key] = value == "true"
        else:
            row[key] = value
    return row


def _result(query: Result) -> str:
    if query.plan is not None:
        return query.plan
    output = {"rows": query.rows, "rowcount": query.rowcount}
    if query.rid is not None:
        output["rid"] = f"{query.rid.page}:{query.rid.slot}"
    return json.dumps(output)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "create":
            with Database.create(args.file):
                pass
            return 0
        if args.command == "migrate":
            Database.migrate(args.file, args.destination)
            return 0
        with Database.open(args.file) as db:
            if args.command == "check":
                result = "ok"
            elif args.command == "sql":
                query = db.execute(args.statement)
                if db.transaction_state != "idle":
                    raise TransactionError("use session for an explicit transaction")
                result = _result(query)
            elif args.command == "session":
                for line in sys.stdin:
                    if line.strip():
                        print(_result(db.execute(line)), flush=True)
                if db.transaction_state != "idle":
                    raise TransactionError(
                        "session ended before COMMIT; transaction discarded"
                    )
            elif args.command == "status":
                print(
                    json.dumps(
                        {
                            "state": db.transaction_state,
                            "format": 1 if db._pager.legacy else 3,
                            "checkpoint": None
                            if db._pager.legacy
                            else db._pager.stamp.seq,
                        }
                    )
                )
            elif args.command == "tables":
                print(json.dumps(db.tables()))
            elif args.command == "create-table":
                columns = []
                for item in args.columns:
                    name, _, kind = item.partition(":")
                    columns.append(Column(name, kind))
                db.create_table(Schema(args.table, tuple(columns)))
            elif args.command == "schema":
                print(
                    json.dumps({c.name: c.kind for c in db.schema(args.table).columns})
                )
            elif args.command == "insert":
                rid = db.insert(args.table, _values(db.schema(args.table), args.values))
                result = f"{rid.page}:{rid.slot}"
            elif args.command == "get":
                print(json.dumps(db.get(args.table, _rid(args.rid))))
            elif args.command == "update":
                db.update(
                    args.table,
                    _rid(args.rid),
                    _values(db.schema(args.table), args.values),
                )
            elif args.command == "delete":
                db.delete(args.table, _rid(args.rid))
            elif args.command == "scan":
                for rid, row in db.scan(args.table):
                    print(json.dumps({"rid": f"{rid.page}:{rid.slot}", "row": row}))
        if args.command in ("insert", "sql", "check"):
            print(result)
        return 0
    except DatabaseError as exc:
        print(f"minidb: {exc}", file=sys.stderr)
        return 1
