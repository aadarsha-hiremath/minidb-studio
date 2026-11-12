"""Bind typed SQL operands, choose a table access path, and execute the bound plan."""

import operator
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from . import sql
from .btree import Entry, Tree, key_bytes
from .database import Database, RecordID
from .schema import Schema, Value

Row = dict[str, Value]
OPS = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@dataclass
class Result:
    rows: list[Row] = field(default_factory=list)
    rowcount: int = 0
    plan: str | None = None
    rid: RecordID | None = None
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Condition:
    column: str
    op: str
    value: Value


@dataclass(frozen=True)
class Plan:
    table: str
    conditions: tuple[Condition, ...]
    tree: Tree | None = None
    low: Entry | None = None
    high: Entry | None = None
    equality: bool = False

    @property
    def description(self) -> str:
        if self.tree is None:
            return f"Sequential Scan on {self.table}"
        mode = "Index Scan" if self.equality else "Index Range Scan"
        return f"{mode} using {self.tree.info.name} on {self.table}"

    def records(self, db: Database) -> Iterator[tuple[RecordID, Row]]:
        if self.tree is None:
            yield from db.scan(self.table)
        else:
            for _, page, slot in self.tree.between(self.low, self.high):
                rid = RecordID(page, slot)
                yield rid, db.get(self.table, rid)

    def matching_records(self, db: Database) -> Iterator[tuple[RecordID, Row]]:
        return (
            (rid, row)
            for rid, row in self.records(db)
            if all(OPS[c.op](row[c.column], c.value) for c in self.conditions)
        )

    def matches(self, db: Database) -> list[tuple[RecordID, Row]]:
        rows = list(self.matching_records(db))
        if self.tree is not None:
            rows.sort(key=operator.itemgetter(0))
        return rows


def plan_query(db: Database, schema: Schema, conditions: tuple[Condition, ...]) -> Plan:
    indexes = {
        schema.columns[t.info.column].name: t for t in db._table(schema.name).indexes
    }
    eligible = [c for c in conditions if c.op != "!=" and c.column in indexes]
    if not eligible:
        return Plan(schema.name, conditions)
    chosen = next((c for c in eligible if c.op == "="), eligible[0])
    tree = indexes[chosen.column]
    lows, highs = [], []
    for c in eligible:
        if c.column != chosen.column:
            continue
        key = key_bytes(tree.info.kind, c.value, stored=False)
        before, after = (key, 0, 0), (key, 2**32, 0)
        if c.op in ("=", ">="):
            lows.append(before)
        if c.op == ">":
            lows.append(after)
        if c.op in ("=", "<="):
            highs.append(after)
        if c.op == "<":
            highs.append(before)
    return Plan(
        schema.name,
        conditions,
        tree,
        max(lows, default=None),
        min(highs, default=None),
        chosen.op == "=",
    )


def _resolve(value: sql.Operand, params: Sequence[Value]) -> Value:
    return params[value.index] if isinstance(value, sql.Parameter) else value


def _typed(kind: str, value: Value) -> Value:
    valid = (
        kind == "int"
        and type(value) is int
        and -(2**63) <= value < 2**63
        or kind == "bool"
        and type(value) is bool
        or kind == "text"
        and type(value) is str
    )
    if not valid:
        raise sql.SQLError(f"expected a value of type {kind}")
    if isinstance(value, str):
        if len(value) > 65536:
            raise sql.SQLError("SQL text operands may contain at most 65536 characters")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise sql.SQLError("expected valid UTF-8 text") from exc
    return value


def execute(
    db: Database,
    ast: sql.Statement,
    count: int,
    params: Sequence[Value],
    *,
    max_result_rows: int | None = None,
) -> Result:
    if not isinstance(params, (tuple, list)) or len(params) != count:
        raise sql.SQLError(f"expected {count} positional parameters")
    if isinstance(ast, Schema):
        db.create_table(ast)
        return Result()
    if isinstance(ast, sql.CreateIndex):
        db.create_index(ast.name, ast.table, ast.column)
        return Result()
    if isinstance(ast, sql.DropIndex):
        db.drop_index(ast.name)
        return Result()

    explain = isinstance(ast, sql.Explain)
    if isinstance(ast, sql.Explain):
        ast = ast.query
    schema = db.schema(ast.table)
    kinds = {col.name: col.kind for col in schema.columns}

    def columns(names: tuple[str, ...]) -> None:
        if len(set(names)) != len(names) or any(name not in kinds for name in names):
            raise sql.SQLError("unknown or duplicate column in " + ", ".join(names))

    def bind(column: str, operand: sql.Operand) -> Value:
        if column not in kinds:
            raise sql.SQLError(f"unknown column {column!r}")
        return _typed(kinds[column], _resolve(operand, params))

    if isinstance(ast, sql.Insert):
        names = ast.columns or tuple(kinds)
        columns(names)
        if len(names) != len(ast.values):
            raise sql.SQLError("INSERT column and value counts differ")
        row = {name: bind(name, v) for name, v in zip(names, ast.values)}
        return Result(rowcount=1, rid=db.insert(ast.table, row))

    conditions = tuple(
        Condition(c.column, c.op, bind(c.column, c.value)) for c in ast.where
    )
    plan = plan_query(db, schema, conditions)
    if isinstance(ast, sql.Select):
        names = ast.columns or tuple(kinds)
        columns(names)
        if ast.order is not None:
            columns((ast.order,))
        limit: int | None = None
        if ast.limit is not None:
            bound = _resolve(ast.limit, params)
            if type(bound) is not int or not 0 <= bound < 2**63:
                raise sql.SQLError("LIMIT requires a nonnegative integer")
            limit = bound
        if explain:
            return Result(plan=plan.description)
        order = ast.order

        def sort_matches(items: list[tuple[RecordID, Row]]) -> None:
            if plan.tree is not None:
                items.sort(key=operator.itemgetter(0))
            if order is not None:
                items.sort(key=lambda pair: pair[1][order], reverse=ast.descending)

        matches: list[tuple[RecordID, Row]] = []
        if limit != 0:
            if max_result_rows is None:
                matches = list(plan.matching_records(db))
            else:
                if max_result_rows < 1:
                    raise sql.SQLError("result capacity must be positive")
                keep = (
                    min(limit, max_result_rows)
                    if limit is not None
                    else max_result_rows
                )
                for pair in plan.matching_records(db):
                    matches.append(pair)
                    if (limit is None or limit > keep) and len(matches) > keep:
                        raise sql.SQLError(
                            f"result exceeds {keep} rows; narrow WHERE or use LIMIT {keep}"
                        )
                    if limit == keep and len(matches) >= 2 * keep:
                        sort_matches(matches)
                        del matches[keep:]
                    if (
                        limit == keep
                        and order is None
                        and plan.tree is None
                        and len(matches) == keep
                    ):
                        break
            sort_matches(matches)
        rows = [{name: row[name] for name in names} for _, row in matches[:limit]]
        return Result(rows, len(rows), columns=names)

    changes = {}
    if isinstance(ast, sql.Update):
        columns(tuple(name for name, _ in ast.assignments))
        changes = {name: bind(name, val) for name, val in ast.assignments}
    # Freeze targets before any mutation can move entries in the driving index.
    matches = plan.matches(db)
    for rid, row in matches:
        if isinstance(ast, sql.Update):
            db.update(ast.table, rid, row | changes)
        else:
            db.delete(ast.table, rid)
    return Result(rowcount=len(matches))
