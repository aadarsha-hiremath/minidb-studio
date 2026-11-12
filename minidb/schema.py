"""Immutable table schemas and typed row serialization."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .binary import I64, U8, U16, Reader
from .errors import Corruption, NoSpace, ValidationError

TYPES = ("int", "text", "bool")
Value = int | str | bool
NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,62}")


def validate_name(name: str) -> None:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValidationError("expected a 1–63 character ASCII identifier")


@dataclass(frozen=True)
class Column:
    name: str
    kind: str

    def __post_init__(self) -> None:
        validate_name(self.name)
        if self.kind not in TYPES:
            raise ValidationError(f"unknown column type {self.kind!r}")


@dataclass(frozen=True)
class Schema:
    name: str
    columns: tuple[Column, ...]

    def __post_init__(self) -> None:
        validate_name(self.name)
        if not isinstance(self.columns, tuple) or not 1 <= len(self.columns) <= 63:
            raise ValidationError("a schema requires a tuple of 1–63 columns")
        if not all(isinstance(c, Column) for c in self.columns):
            raise ValidationError("expected Column objects")
        if len({c.name for c in self.columns}) != len(self.columns):
            raise ValidationError("duplicate column name")

    def encode(self, row: Mapping[str, Value], limit: int) -> bytes:
        if not isinstance(row, Mapping) or row.keys() != {c.name for c in self.columns}:
            raise ValidationError("supply exactly the declared columns")
        data = bytearray()
        for column in self.columns:
            value = row[column.name]
            if (
                column.kind == "int"
                and type(value) is int
                and -(2**63) <= value < 2**63
            ):
                data.extend(I64.pack(value))
            elif column.kind == "bool" and type(value) is bool:
                data.append(int(value))
            elif column.kind == "text" and isinstance(value, str):
                if len(value) > limit:
                    raise NoSpace("record exceeds single-page limit")
                try:
                    field = value.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValidationError("text must encode as UTF-8") from exc
                data.extend(U16.pack(len(field)))
                data.extend(field)
            else:
                raise ValidationError(f"{column.name} requires {column.kind}")
            if len(data) > limit:
                raise NoSpace("record exceeds single-page limit")
        return bytes(data)

    def decode(self, record: bytes) -> dict[str, Value]:
        reader = Reader(record)
        row: dict[str, Value] = {}
        for column in self.columns:
            if column.kind == "text":
                row[column.name] = reader.string(U16)
            elif column.kind == "int":
                row[column.name] = reader.number(I64)
            else:
                value = reader.number(U8)
                if value > 1:
                    raise Corruption("invalid boolean")
                row[column.name] = bool(value)
        if reader.pos != len(record):
            raise Corruption("trailing bytes in record")
        return row
