"""A catalog page's physical page number doubles as its table identifier."""

import struct

from .binary import BODY_SIZE, U8, Reader, seal
from .errors import Corruption, NoSpace, ValidationError
from .schema import TYPES, Column, Schema

PREFIX = struct.Struct(">4sI")
TAG = b"SCHM"


def pack_schema(page_id: int, schema: Schema) -> bytes:
    def name_field(name: str) -> bytes:
        return U8.pack(len(name)) + name.encode("ascii")

    body = bytearray(PREFIX.pack(TAG, page_id))
    body.extend(name_field(schema.name))
    body.append(len(schema.columns))
    for column in schema.columns:
        body.extend(name_field(column.name))
        body.append(TYPES.index(column.kind) + 1)
    if len(body) > BODY_SIZE:
        raise NoSpace("schema exceeds catalog page capacity")
    return seal(body)


def unpack_schema(body: bytes, page_id: int) -> Schema:
    if PREFIX.unpack_from(body) != (TAG, page_id):
        raise Corruption("invalid catalog page identifier")
    reader = Reader(body[PREFIX.size :])
    name = reader.string(U8)
    columns = []
    try:
        for _ in range(reader.number(U8)):
            column_name = reader.string(U8)
            kind = reader.number(U8)
            if not 1 <= kind <= len(TYPES):
                raise Corruption("unknown type code")
            columns.append(Column(column_name, TYPES[kind - 1]))
        schema = Schema(name, tuple(columns))
    except ValidationError as exc:
        raise Corruption("malformed catalog schema") from exc
    if any(reader.data[reader.pos :]):
        raise Corruption("nonzero catalog padding")
    return schema
