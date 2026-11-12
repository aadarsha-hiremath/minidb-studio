"""Position-aware lexer and recursive-descent parser for MiniDB's single statements."""

import re
from dataclasses import dataclass

from .errors import ValidationError
from .schema import Column, Schema, Value, validate_name


class SQLError(ValidationError):
    """An invalid statement, binding or SQL expression."""


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int


TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<word>[A-Za-z_][A-Za-z_0-9]*)|(?P<int>-?[0-9]+)"
    r"|(?P<str>'(?:[^']|'')*')|(?P<op>!=|<=|>=|[=<>])|(?P<symbol>[?(),*;])"
)


def tokenize(source: str) -> list[Token]:
    if not isinstance(source, str) or len(source) > 65536:
        raise SQLError("expected SQL text of at most 65536 characters")
    tokens = []
    pos = 0
    while pos < len(source):
        match = TOKEN.match(source, pos)
        if match is None:
            raise SQLError(
                f"unexpected character or unterminated literal at character {pos + 1}"
            )
        kind = match.lastgroup
        assert kind is not None  # Every token alternative has a group name.
        if kind != "space":
            tokens.append(Token(kind, match.group(), pos))
        pos = match.end()
    tokens.append(Token("end", "", pos))
    return tokens


@dataclass(frozen=True)
class Parameter:
    index: int


Operand = Value | Parameter


@dataclass(frozen=True)
class Predicate:
    column: str
    op: str
    value: Operand


@dataclass(frozen=True)
class Select:
    table: str
    columns: tuple[str, ...] | None
    where: tuple[Predicate, ...]
    order: str | None
    descending: bool
    limit: Operand | None


@dataclass(frozen=True)
class Explain:
    query: Select


@dataclass(frozen=True)
class Insert:
    table: str
    columns: tuple[str, ...] | None
    values: tuple[Operand, ...]


@dataclass(frozen=True)
class Update:
    table: str
    assignments: tuple[tuple[str, Operand], ...]
    where: tuple[Predicate, ...]


@dataclass(frozen=True)
class Delete:
    table: str
    where: tuple[Predicate, ...]


@dataclass(frozen=True)
class CreateIndex:
    name: str
    table: str
    column: str


@dataclass(frozen=True)
class DropIndex:
    name: str


@dataclass(frozen=True)
class Control:
    command: str


Statement = (
    Schema | CreateIndex | DropIndex | Select | Explain | Insert | Update | Delete
)


class Parser:
    def __init__(self, source: str):
        self.tokens = tokenize(source)
        self.pos = 0
        self.parameters = 0

    @property
    def token(self) -> Token:
        return self.tokens[self.pos]

    def error(self, expected: str) -> SQLError:
        return SQLError(
            f"expected {expected} at character {self.token.pos + 1}, found {self.token.text!r}"
        )

    def accept(self, text: str) -> bool:
        if self.token.text.upper() != text:
            return False
        self.pos += 1
        return True

    def need(self, text: str) -> None:
        if not self.accept(text):
            raise self.error(text)

    def name(self) -> str:
        if self.token.kind != "word":
            raise self.error("identifier")
        name = self.token.text
        validate_name(name)
        self.pos += 1
        return name

    def value(self) -> Operand:
        t = self.token
        if self.accept("?"):
            self.parameters += 1
            return Parameter(self.parameters - 1)
        if self.accept("TRUE"):
            return True
        if self.accept("FALSE"):
            return False
        if t.kind == "int":
            self.pos += 1
            digits = t.text.lstrip("-").lstrip("0") or "0"
            if len(digits) > 19:
                raise SQLError(f"integer out of range at character {t.pos + 1}")
            n = int(digits) * (-1 if t.text.startswith("-") else 1)
            if not -(2**63) <= n < 2**63:
                raise SQLError(f"integer out of range at character {t.pos + 1}")
            return n
        if t.kind == "str":
            self.pos += 1
            return t.text[1:-1].replace("''", "'")
        raise self.error("integer, quoted text, boolean or ?")

    def names(self) -> tuple[str, ...]:
        names = [self.name()]
        while self.accept(","):
            names.append(self.name())
        return tuple(names)

    def where(self) -> tuple[Predicate, ...]:
        predicates = []
        if self.accept("WHERE"):
            while True:
                name = self.name()
                op = self.token
                if op.kind != "op":
                    raise self.error("comparison operator")
                self.pos += 1
                predicates.append(Predicate(name, op.text, self.value()))
                if not self.accept("AND"):
                    break
        return tuple(predicates)

    def select(self) -> Select:
        columns = None if self.accept("*") else self.names()
        self.need("FROM")
        table = self.name()
        where = self.where()
        order = None
        descending = False
        if self.accept("ORDER"):
            self.need("BY")
            order = self.name()
            descending = self.accept("DESC")
            if not descending:
                self.accept("ASC")
        limit = self.value() if self.accept("LIMIT") else None
        return Select(table, columns, where, order, descending, limit)

    def statement(self) -> Statement | Control:
        for command in ("BEGIN", "COMMIT", "ROLLBACK"):
            if self.accept(command):
                return Control(command)
        if self.accept("SELECT"):
            return self.select()
        if self.accept("EXPLAIN"):
            self.need("SELECT")
            return Explain(self.select())
        if self.accept("INSERT"):
            self.need("INTO")
            table = self.name()
            names = None
            if self.accept("("):
                names = self.names()
                self.need(")")
            self.need("VALUES")
            self.need("(")
            values = [self.value()]
            while self.accept(","):
                values.append(self.value())
            self.need(")")
            return Insert(table, names, tuple(values))
        if self.accept("UPDATE"):
            table = self.name()
            self.need("SET")
            assignments = []
            while True:
                name = self.name()
                self.need("=")
                assignments.append((name, self.value()))
                if not self.accept(","):
                    break
            return Update(table, tuple(assignments), self.where())
        if self.accept("DELETE"):
            self.need("FROM")
            return Delete(self.name(), self.where())
        if self.accept("DROP"):
            self.need("INDEX")
            return DropIndex(self.name())
        if self.accept("CREATE"):
            if self.accept("INDEX"):
                name = self.name()
                self.need("ON")
                table = self.name()
                self.need("(")
                column = self.name()
                self.need(")")
                return CreateIndex(name, table, column)
            self.need("TABLE")
            name = self.name()
            self.need("(")
            columns = []
            while True:
                column = self.name()
                kind = self.name().lower()
                columns.append(
                    Column(
                        column, {"integer": "int", "boolean": "bool"}.get(kind, kind)
                    )
                )
                if not self.accept(","):
                    break
            self.need(")")
            return Schema(name, tuple(columns))
        raise self.error(
            "SELECT, EXPLAIN, INSERT, UPDATE, DELETE, CREATE, DROP, BEGIN, COMMIT or ROLLBACK"
        )


def parse(source: str) -> tuple[Statement | Control, int]:
    parser = Parser(source)
    ast = parser.statement()
    parser.accept(";")
    if parser.token.kind != "end":
        raise parser.error("end of statement")
    return ast, parser.parameters
