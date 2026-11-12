"""Public table API over the binary catalog, typed codec, and slotted pages."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Concatenate, ParamSpec, Self, TypeVar

from . import btree, catalog
from . import sql as language
from .binary import U32
from .errors import (
    Corruption,
    NoSpace,
    NotFound,
    StorageError,
    TransactionError,
    ValidationError,
)
from .pager import Pager
from .pages import MAX_RECORD, SLOT, DataPage
from .schema import Schema, Value, validate_name

if TYPE_CHECKING:
    from .executor import Result

P = ParamSpec("P")
R = TypeVar("R")


def mutation(
    method: Callable[Concatenate[Database, P], R],
) -> Callable[Concatenate[Database, P], R]:
    @wraps(method)
    def atomic(db: Database, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with db._statement():
            return method(db, *args, **kwargs)

    return atomic


@dataclass(frozen=True, order=True)
class RecordID:
    """Physical page and zero-based slot in a particular database file."""

    page: int
    slot: int

    def __post_init__(self) -> None:
        if (
            type(self.page) is not int
            or not 0 < self.page < 0xFFFFFFFF
            or type(self.slot) is not int
            or not 0 <= self.slot < 65536
        ):
            raise ValidationError("invalid physical record identifier")


@dataclass
class _Table:
    schema: Schema
    page_id: int
    pages: list[int] = field(default_factory=list)
    indexes: list[btree.Tree] = field(default_factory=list)
    live_count: int = 0


class Database:
    """A thread-confined database handle. Use create/open as context managers."""

    def __init__(self, pager: Pager):
        self._pager = pager
        self._tables: dict[str, _Table] = {}
        self._indexes: dict[str, btree.Tree] = {}
        self._revision = 0
        self._rollback_only = False
        self._managed = False
        self._statement_depth = 0
        try:
            self._load()
            self.replayed_on_open = pager.recovery is not None
            pager.finish_open()
        except BaseException:
            pager.close()
            raise

    @classmethod
    def create(cls, path: str | Path) -> Self:
        return cls(Pager(path, create=True))

    @classmethod
    def open(cls, path: str | Path) -> Self:
        return cls(Pager(path, create=False))

    @classmethod
    def migrate(cls, source: str | Path, destination: str | Path) -> None:
        """Validate the locked v1 source and bootstrap an exclusive new v3 pair."""
        with cls.open(source) as old:
            if not old._pager.legacy:
                raise TransactionError("migration requires a v1 source")
            with cls(Pager(destination, create=True, source=old._pager)):
                pass

    @property
    def transaction_state(self) -> str:
        self._pager.check()
        if self._rollback_only:
            return "rollback-only"
        return "active" if self._pager.overlay is not None else "idle"

    def begin(self) -> None:
        self._pager.begin()

    def commit(self) -> None:
        if self._statement_depth or self._managed or self.transaction_state != "active":
            raise TransactionError(
                "commit requires an active, committable unmanaged transaction"
            )
        self._pager.commit()

    def rollback(self) -> None:
        if self._statement_depth or self._managed or self.transaction_state == "idle":
            raise TransactionError("rollback requires an active unmanaged transaction")
        dirty = bool(self._pager.overlay)
        self._pager.rollback()
        self._rollback_only = False
        if dirty:
            self._tables.clear()
            self._indexes.clear()
            self._revision += 1
            try:
                self._load()
            except BaseException:
                self._pager.failed = True
                raise

    @contextmanager
    def transaction(self) -> Iterator[Self]:
        self.begin()
        self._managed = True
        try:
            yield self
            if self._rollback_only:
                raise TransactionError("transaction is rollback-only")
        except BaseException:
            self._managed = False
            self.rollback()
            raise
        else:
            self._managed = False
            self.commit()

    @contextmanager
    def _statement(self) -> Iterator[None]:
        self._guard()
        implicit = self._pager.overlay is None
        if implicit:
            self.begin()
        try:
            self._statement_depth += 1
            try:
                yield
                self._guard()
            finally:
                self._statement_depth -= 1
        except BaseException:
            self._rollback_only = True
            if implicit:
                self.rollback()
            raise
        if implicit:
            self.commit()

    def _guard(self) -> None:
        self._pager.check()
        if self._rollback_only:
            raise TransactionError("transaction is rollback-only")

    def _load(self) -> None:
        owners: dict[int, _Table] = {}
        nodes: set[int] = set()
        for page_id in range(1, self._pager.count):
            body = self._pager.read(page_id)
            if body.startswith(catalog.TAG):
                schema = catalog.unpack_schema(body, page_id)
                if schema.name in self._tables:
                    raise Corruption("duplicate table in catalog")
                table = _Table(schema, page_id)
                self._tables[schema.name] = owners[page_id] = table
            elif body.startswith(btree.INDEX):
                info = btree.Definition.read(body, page_id)
                owner = owners.get(info.table)
                if (
                    owner is None
                    or info.column >= len(owner.schema.columns)
                    or owner.schema.columns[info.column].kind != info.kind
                    or info.name in self._indexes
                    or any(t.info.column == info.column for t in owner.indexes)
                ):
                    raise Corruption("inconsistent index definition")
                tree = btree.Tree(self._pager, info)
                owner.indexes.append(tree)
                self._indexes[info.name] = tree
            elif body.startswith((btree.LEAF, btree.BRANCH)):
                nodes.add(page_id)
            elif body.startswith(btree.FREE):
                if U32.unpack_from(body, 4)[0] != page_id or any(body[8:]):
                    raise Corruption("malformed retired page")
            else:
                page = DataPage.unpack(body, page_id)
                if page.table_id not in owners:
                    raise Corruption("data page has no preceding schema")
                table = owners[page.table_id]
                for record in page.slots:
                    if record is not None:
                        table.schema.decode(record)
                        table.live_count += 1
                table.pages.append(page_id)
        for table in owners.values():
            for tree in table.indexes:
                nodes.difference_update(self._validate_tree(table, tree))
        if nodes:
            raise Corruption("unreachable index pages")

    def _validate_tree(self, table: _Table, tree: btree.Tree) -> set[int]:
        column = table.schema.columns[tree.info.column]

        def check(entry: btree.Entry) -> None:
            key, page, slot = entry
            try:
                row = self.get(table.schema.name, RecordID(page, slot))
            except NotFound as exc:
                raise Corruption("index references missing record") from exc
            if btree.key_bytes(column.kind, row[column.name], stored=False) != key:
                raise Corruption("index key disagrees with record")

        pages, count = tree.validate(check)
        if count != table.live_count:
            raise Corruption("index does not cover every live record")
        return pages

    @mutation
    def create_index(self, name: str, table_name: str, column_name: str) -> None:
        table = self._table(table_name)
        validate_name(name)
        validate_name(column_name)
        columns = [c.name for c in table.schema.columns]
        if column_name not in columns:
            raise ValidationError(f"unknown column {column_name!r}")
        col = columns.index(column_name)
        if name in self._indexes or any(t.info.column == col for t in table.indexes):
            raise ValidationError("index name or indexed column already in use")
        kind = table.schema.columns[col].kind
        for _, row in self.scan(table_name):
            btree.key_bytes(kind, row[column_name])
        page = self._pager.count
        if page > 0xFFFFFFFF - 2:
            raise NoSpace("cannot allocate index definition and root")
        info = btree.Definition(page, table.page_id, page + 1, col, kind, name)
        tree = btree.Tree(self._pager, info)
        self._pager.append(info.pack())
        self._pager.append(btree.Node(info.root).pack(page))
        for rid, row in self.scan(table_name):
            tree.insert((btree.key_bytes(kind, row[column_name]), rid.page, rid.slot))
        table.indexes.append(tree)
        self._indexes[name] = tree
        self._revision += 1

    @mutation
    def drop_index(self, name: str) -> None:
        self._pager.check()
        validate_name(name)
        if name not in self._indexes:
            raise NotFound(f"unknown index {name!r}")
        tree = self._indexes[name]
        table = next(t for t in self._tables.values() if t.page_id == tree.info.table)
        pages = self._validate_tree(table, tree)
        for page in sorted(pages | {tree.info.page}):
            self._pager.write(page, btree.retired(page))
        table.indexes.remove(tree)
        del self._indexes[name]
        self._revision += 1

    def storage_status(self) -> dict[str, int | None]:
        """Inspect the locked descriptors without opening another database handle."""
        pager = self._pager
        pager.check()
        return {
            "format": 1 if pager.legacy else 3,
            "checkpoint": None if pager.legacy else pager.stamp.seq,
            "database_bytes": os.fstat(pager.file.fileno()).st_size,
            "wal_bytes": os.fstat(pager.journal.fileno()).st_size
            if pager.journal
            else None,
        }

    def indexes(self, table_name: str) -> tuple[tuple[str, str, int], ...]:
        table = self._table(table_name)
        return tuple(
            (
                tree.info.name,
                table.schema.columns[tree.info.column].name,
                tree.info.root,
            )
            for tree in table.indexes
        )

    def execute(
        self,
        sql: str,
        params: tuple[Value, ...] | list[Value] = (),
        *,
        max_result_rows: int | None = None,
    ) -> Result:
        from .executor import execute

        self._pager.check()
        try:
            ast, count = language.parse(sql)
        except BaseException:
            if self._pager.overlay is not None:
                self._rollback_only = True
            raise
        if isinstance(ast, language.Control):
            from .executor import Result

            if not isinstance(params, (tuple, list)) or params:
                raise TransactionError("transaction commands take no parameters")
            {"BEGIN": self.begin, "COMMIT": self.commit, "ROLLBACK": self.rollback}[
                ast.command
            ]()
            return Result()
        self._guard()
        if isinstance(ast, (language.Select, language.Explain)):
            try:
                return execute(
                    self, ast, count, params, max_result_rows=max_result_rows
                )
            except BaseException:
                if self._pager.overlay is not None:
                    self._rollback_only = True
                raise
        with self._statement():
            return execute(self, ast, count, params)

    @staticmethod
    def _keys(table: _Table, record: bytes) -> list[tuple[btree.Tree, bytes]]:
        if not table.indexes:
            return []
        row = table.schema.decode(record)
        return [
            (
                tree,
                btree.key_bytes(
                    tree.info.kind, row[table.schema.columns[tree.info.column].name]
                ),
            )
            for tree in table.indexes
        ]

    def __enter__(self) -> Self:
        self._pager.check()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._managed or self._statement_depth:
            raise TransactionError(
                "cannot close during a statement or managed transaction"
            )
        self._pager.close()

    def tables(self) -> tuple[str, ...]:
        self._guard()
        return tuple(self._tables)

    def _table(self, name: str) -> _Table:
        self._guard()
        validate_name(name)
        try:
            return self._tables[name]
        except KeyError as exc:
            raise NotFound(f"unknown table {name!r}") from exc

    def schema(self, name: str) -> Schema:
        return self._table(name).schema

    @mutation
    def create_table(self, schema: Schema) -> None:
        self._pager.check()
        if not isinstance(schema, Schema):
            raise ValidationError("expected a Schema")
        if schema.name in self._tables:
            raise ValidationError(f"table {schema.name!r} already exists")
        page_id = self._pager.count
        self._pager.append(catalog.pack_schema(page_id, schema))
        self._tables[schema.name] = _Table(schema, page_id)
        self._revision += 1

    def _read_page(self, table: _Table, page_id: int) -> DataPage:
        page = DataPage.unpack(self._pager.read(page_id), page_id)
        if page.table_id != table.page_id:
            raise Corruption("unexpected table identifier")
        return page

    @mutation
    def insert(self, name: str, row: Mapping[str, Value]) -> RecordID:
        table = self._table(name)
        record = table.schema.encode(row, MAX_RECORD)
        keys = self._keys(table, record)
        for page_id in table.pages:
            page = self._read_page(table, page_id)
            if page.free >= len(record) + SLOT.size:
                slot = len(page.slots)
                page.slots.append(record)
                break
        else:
            page_id = self._pager.count
            slot = 0
            page = DataPage(page_id, table.page_id, [record])
        if page_id == self._pager.count:
            self._pager.append(page.pack())
            table.pages.append(page_id)
        else:
            self._pager.write(page_id, page.pack())
        for tree, key in keys:
            tree.insert((key, page_id, slot))
        table.live_count += 1
        self._revision += 1
        return RecordID(page_id, slot)

    def _locate(self, table: _Table, rid: RecordID) -> DataPage:
        if not isinstance(rid, RecordID):
            raise ValidationError("expected a RecordID")
        if not 0 < rid.page < self._pager.count:
            raise NotFound("no such record page")
        body = self._pager.read(rid.page)
        if body.startswith(
            (catalog.TAG, btree.INDEX, btree.LEAF, btree.BRANCH, btree.FREE)
        ):
            raise NotFound("record identifier refers to a non-data page")
        page = DataPage.unpack(body, rid.page)
        if page.table_id != table.page_id:
            raise NotFound("record page belongs to a different table")
        return page

    def get(self, name: str, rid: RecordID) -> dict[str, Value]:
        table = self._table(name)
        return table.schema.decode(self._locate(table, rid).get(rid.slot))

    @mutation
    def update(self, name: str, rid: RecordID, row: Mapping[str, Value]) -> None:
        table = self._table(name)
        page = self._locate(table, rid)
        record = table.schema.encode(row, MAX_RECORD)
        old_keys = self._keys(table, page.get(rid.slot))
        new_keys = self._keys(table, record)
        page.replace(rid.slot, record)
        self._pager.write(page.page_id, page.pack())
        for (tree, old), (_, new) in zip(old_keys, new_keys):
            if old != new:
                tree.remove((old, rid.page, rid.slot))
                tree.insert((new, rid.page, rid.slot))
        self._revision += 1

    @mutation
    def delete(self, name: str, rid: RecordID) -> None:
        table = self._table(name)
        page = self._locate(table, rid)
        keys = self._keys(table, page.get(rid.slot))
        page.replace(rid.slot, None)
        self._pager.write(page.page_id, page.pack())
        for tree, key in keys:
            tree.remove((key, rid.page, rid.slot))
        table.live_count -= 1
        self._revision += 1

    def scan(self, name: str) -> Iterator[tuple[RecordID, dict[str, Value]]]:
        """Yield live rows in physical order. A mutation invalidates an active scan."""
        table = self._table(name)
        revision = self._revision
        for page_id in table.pages:
            page = self._read_page(table, page_id)
            for slot, record in enumerate(page.slots):
                if record is not None:
                    yield RecordID(page_id, slot), table.schema.decode(record)
                    self._guard()
                    if self._revision != revision:
                        raise StorageError("database changed during scan")
