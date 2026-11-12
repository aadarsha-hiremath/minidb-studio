"""Single-column, page-resident B+ tree with persistent lower-bound fences."""

import struct
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from .binary import BODY_SIZE, U8, U16, U32, Reader, seal
from .errors import Corruption, NoSpace, ValidationError
from .pager import Pager
from .schema import TYPES, Value, validate_name

META = struct.Struct(">4sIIIBB")
NODE = struct.Struct(">4sIIIH")
RID = struct.Struct(">IH")
LEAF, BRANCH, INDEX, FREE = b"ILEF", b"IBRN", b"IDXM", b"FREE"
KEY_BYTES = {"int": 8, "text": 240, "bool": 1}
Entry = tuple[bytes, int, int]


def key_bytes(kind: str, value: Value, *, stored: bool = True) -> bytes:
    if kind == "int" and type(value) is int:
        data = (value + (1 << 63)).to_bytes(8, "big")
    elif kind == "bool" and type(value) is bool:
        data = bytes([value])
    elif kind == "text" and isinstance(value, str):
        data = value.encode("utf-8")
    else:
        raise ValidationError(f"invalid key for {kind} index")
    if stored and len(data) > KEY_BYTES[kind]:
        raise NoSpace("indexed text exceeds 240 UTF-8 bytes")
    return data


def retired(page_id: int) -> bytes:
    return seal(FREE + U32.pack(page_id))


@dataclass
class Definition:
    page: int
    table: int
    root: int
    column: int
    kind: str
    name: str

    def pack(self) -> bytes:
        name = self.name.encode("ascii")
        return seal(
            META.pack(
                INDEX,
                self.page,
                self.table,
                self.root,
                self.column,
                TYPES.index(self.kind) + 1,
            )
            + U8.pack(len(name))
            + name
        )

    @classmethod
    def read(cls, body: bytes, page: int) -> "Definition":
        tag, ident, table, root, col, kind = META.unpack_from(body)
        if tag != INDEX or page != ident or not 1 <= kind <= len(TYPES):
            raise Corruption("invalid index definition")
        r = Reader(body[META.size :])
        name = r.string(U8)
        try:
            validate_name(name)
        except ValidationError as exc:
            raise Corruption("invalid index name") from exc
        if any(r.data[r.pos :]):
            raise Corruption("invalid index definition padding")
        return cls(page, table, root, col, TYPES[kind - 1], name)


@dataclass
class Node:
    page: int
    entries: list[Entry] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    next_leaf: int = 0

    def pack(self, owner: int) -> bytes:
        link = self.children[0] if self.children else self.next_leaf
        body = bytearray(
            NODE.pack(
                BRANCH if self.children else LEAF,
                self.page,
                owner,
                link,
                len(self.entries),
            )
        )
        for i, (key, page, slot) in enumerate(self.entries):
            body.extend(U16.pack(len(key)) + key + RID.pack(page, slot))
            if self.children:
                body.extend(U32.pack(self.children[i + 1]))
        return seal(body)


class Tree:
    def __init__(self, pager: Pager, definition: Definition):
        self.pager = pager
        self.info = definition
        # One common fanout bound guarantees even worst-case internal keys fit.
        self.capacity = (BODY_SIZE - NODE.size) // (12 + KEY_BYTES[definition.kind])

    def node(self, page: int) -> Node:
        body = self.pager.read(page)
        tag, ident, owner, link, count = NODE.unpack_from(body)
        if (
            tag not in (BRANCH, LEAF)
            or page != ident
            or owner != self.info.page
            or count > self.capacity
            or link >= self.pager.count
        ):
            raise Corruption("invalid index node header or link")
        internal = tag == BRANCH
        if internal and (link == 0 or count == 0):
            raise Corruption("empty internal index node")
        node = Node(
            page, children=[link] if internal else [], next_leaf=0 if internal else link
        )
        r = Reader(body[NODE.size :])
        kind = self.info.kind
        for _ in range(count):
            key = r.take(r.number(U16))
            if kind == "text":
                if len(key) > KEY_BYTES[kind]:
                    raise Corruption("oversized text index key")
                try:
                    key.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise Corruption("invalid UTF-8 index key") from exc
            elif len(key) != KEY_BYTES[kind] or (kind == "bool" and key[0] > 1):
                raise Corruption("invalid fixed-width index key")
            rid_page, slot = RID.unpack(r.take(RID.size))
            entry = (key, rid_page, slot)
            if not 0 < rid_page < self.pager.count or (
                node.entries and entry <= node.entries[-1]
            ):
                raise Corruption("invalid index key order or RID")
            node.entries.append(entry)
            if internal:
                child = r.number(U32)
                if not 0 < child < self.pager.count:
                    raise Corruption("invalid index child")
                node.children.append(child)
        if any(r.data[r.pos :]):
            raise Corruption("invalid index node padding")
        return node

    def _leaf(self, entry: Entry | None) -> tuple[Node, list[tuple[Node, int]]]:
        node = self.node(self.info.root)
        path: list[tuple[Node, int]] = []
        while node.children:
            if len(path) >= 32:
                raise Corruption("index exceeds maximum routing depth")
            i = bisect_right(node.entries, entry) if entry is not None else 0
            path.append((node, i))
            node = self.node(node.children[i])
        return node, path

    def between(self, low: Entry | None, high: Entry | None) -> Iterator[Entry]:
        """Half-open composite bounds. Start with a root descent, then follow siblings."""
        node, _ = self._leaf(low)
        i = bisect_left(node.entries, low) if low is not None else 0
        for _ in range(self.pager.count):
            for entry in node.entries[i:]:
                if high is not None and entry >= high:
                    return
                yield entry
            if not node.next_leaf:
                return
            node = self.node(node.next_leaf)
            if node.children:
                raise Corruption("leaf link points to internal node")
            i = 0
        raise Corruption("cycle in leaf chain")

    def _save(self, node: Node) -> None:
        self.pager.write(node.page, node.pack(self.info.page))

    def insert(self, entry: Entry) -> None:
        node, path = self._leaf(entry)
        i = bisect_left(node.entries, entry)
        if i < len(node.entries) and node.entries[i] == entry:
            raise Corruption("duplicate index entry")
        node.entries.insert(i, entry)
        while len(node.entries) > self.capacity:
            mid = len(node.entries) // 2
            fence = node.entries[mid]
            right = Node(self.pager.count)
            if node.children:
                right.entries, right.children = (
                    node.entries[mid + 1 :],
                    node.children[mid + 1 :],
                )
                node.children = node.children[: mid + 1]
            else:
                right.entries, right.next_leaf = node.entries[mid:], node.next_leaf
                node.next_leaf = right.page
            node.entries = node.entries[:mid]
            self.pager.append(right.pack(self.info.page))
            self._save(node)
            if not path:
                root = Node(self.pager.count, [fence], [node.page, right.page])
                self.pager.append(root.pack(self.info.page))
                self.info.root = root.page
                self.pager.write(self.info.page, self.info.pack())
                return
            node, i = path.pop()
            node.entries.insert(i, fence)
            node.children.insert(i + 1, right.page)
        self._save(node)

    def remove(self, entry: Entry) -> None:
        node, _ = self._leaf(entry)
        i = bisect_left(node.entries, entry)
        if i == len(node.entries) or node.entries[i] != entry:
            raise Corruption("missing index entry during deletion")
        node.entries.pop(i)
        self._save(node)

    def validate(self, check_entry: Callable[[Entry], None]) -> tuple[set[int], int]:
        """Walk branches in order, checking fences, depth, links and each live entry."""
        seen: set[int] = set()
        depth = None
        expected_leaf = None
        entries = 0

        def visit(page: int, low: Entry | None, high: Entry | None, level: int) -> None:
            nonlocal depth, expected_leaf, entries
            if page in seen or level > 32:
                raise Corruption("shared, cyclic or excessively deep index node")
            seen.add(page)
            node = self.node(page)
            if node.entries and (
                (low is not None and node.entries[0] < low)
                or (high is not None and node.entries[-1] >= high)
            ):
                raise Corruption("index keys outside routing fences")
            if node.children:
                for i, child in enumerate(node.children):
                    visit(
                        child,
                        low if i == 0 else node.entries[i - 1],
                        high if i == len(node.entries) else node.entries[i],
                        level + 1,
                    )
            else:
                if depth is not None and (depth != level or expected_leaf != page):
                    raise Corruption("index leaf depth or next link mismatch")
                depth, expected_leaf = level, node.next_leaf
                for entry in node.entries:
                    check_entry(entry)
                entries += len(node.entries)

        visit(self.info.root, None, None, 0)
        if expected_leaf != 0:
            raise Corruption("last index leaf has a successor")
        return seen, entries
