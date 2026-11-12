# MiniDB storage pages

New databases use format **v3**. Physical page 0 is an immutable anchor, pages 1 and 2 hold checkpoint stamps, and logical page `p ≥ 1` resides at physical page `p + 2`. Its RID, tag, owners and child pointers use logical IDs. See [the v3 header and WAL specification](transactions.md#binary-layout) for exact fields. The following v1 header applies only to legacy files. Both versions share the catalog, heap, index, padding and CRC layouts below.

Integers are big-endian. Offsets and sizes are in bytes. Every page occupies exactly 4,096 bytes. In v1, logical page `p` starts at file offset `4096 × p`. All unused bytes must be zero. On each page, bytes 4,092–4,095 store the unsigned CRC-32 of bytes 0–4,091, using Python's `zlib.crc32` convention. The checksum covers padding.

## File header, page 0

| Offset | Size | Value |
| ---: | ---: | --- |
| 0 | 8 | Signature, hex `4d 49 4e 49 44 42 0d 0a` |
| 8 | 2 | Unsigned version, `1` |
| 10 | 2 | Unsigned page size, `4096` |
| 12 | 4 | Unsigned page count including this page |
| 16 | 4,076 | Zero padding |
| 4,092 | 4 | CRC-32 |

File length must equal page count × 4,096. A new file contains exactly one page. The maximum page count is 4,294,967,295. The first schema or data allocation uses the previous count as the new page ID and increments the count in the header.

## Catalog page

Offset 0 holds four ASCII bytes `SCHM`. Offset 4 holds the unsigned four-byte physical page ID, which also identifies the table. Offset 8 begins the schema. A name consists of one unsigned length byte followed by that many ASCII identifier bytes. Table names are unique in the file. Column names are unique within a table. The schema holds, in order:

1. Table name.
2. One unsigned byte giving the column count (1–63).
3. For each column, its name and one type byte (`1` integer, `2` text, `3` boolean).
4. Zero padding to the checksum.

The entire encoded catalog entry including its eight-byte prefix must fit in 4,092 bytes. A table's schema precedes every data page it owns. Multiple tables' pages can be interleaved.

## Phase 2 index extension

The header, page size, CRC trailer, table schemas, row encodings, and physical RIDs retain their v1 layouts. Files containing only Phase 1 page types work in both engines. The Phase 2 writer supported SQL in v1. Its CREATE INDEX appended the first extension pages and updates the ordinary header count. Phase 1 verifies the tag of every non-schema page and rejects an extension page during open, before exposing a writable handle. A file containing retired index pages also requires Phase 2. Phase 3 reads v1 and requires explicit copy migration for all writes. Existing heap pages and RIDs are preserved.

All index integers are unsigned big-endian. CRC and zero padding follow the common page rules.

| Page tag | Offsets in the 4,092-byte body |
| --- | --- |
| `IDXM` | 0 tag, 4 own ID (u32), 8 table schema ID (u32), 12 root ID (u32), 16 zero-based column ordinal (u8), 17 schema type code (u8), 18 name length (u8), 19 index name bytes, then padding. |
| `ILEF` | 0 tag, 4 own ID (u32), 8 definition ID (u32), 12 next leaf ID (u32, zero ends chain), 16 entry count (u16), 18 packed entries, then padding. |
| `IBRN` | Same header, with the leftmost child ID at offset 12. Each routing entry includes its right child ID. |
| `FREE` | 0 tag, 4 own ID (u32), then zero padding. A dropped index leaves these pages behind. |

Each entry has a u16 key-byte length, the key bytes, a u32 RID page and a u16 RID slot. Internal entries add a u32 right child. Integers encode as eight bytes of `value + 2^63`. Booleans encode as one byte, zero or one. Text uses up to 240 strict UTF-8 bytes, including the empty string. Comparisons order the key bytes lexicographically **without** their length prefix, then compare numeric RID page and slot. UTF-8 byte order preserves Unicode scalar order. Length and type are validated when decoding.

Both node types use capacity `floor((4092 − 18) / (12 + maximum_key_bytes))`: 203 integer entries, 16 text entries, or 313 boolean entries. This leaves room for a right child per routing entry even at the maximum text width. A branch stores at least one fence and one more child than fences. Child intervals include their lower fence and exclude their upper fence. Leaf entries represent live records. Internal fences may survive deletion of the original row. All leaves of a tree have equal depth and their next links follow the in-order branch traversal.

## Data page

| Offset | Size | Value |
| ---: | ---: | --- |
| 0 | 4 | ASCII `DATA` |
| 4 | 4 | Unsigned physical page ID |
| 8 | 4 | Unsigned owning schema page ID |
| 12 | 2 | Unsigned slot count, at least 1 |
| 14 | 4 × slot count | Slot directory in slot-number order |
| After directory | Variable | Zero-filled free space |
| Before CRC | Variable | Densely packed live record bytes |
| 4,092 | 4 | CRC-32 |

Slot `s` begins at offset `14 + 4 × s`. Each descriptor holds two unsigned 16-bit integers, the page-relative record start and byte length. A tombstone is `(0, 0)`. Live records have positive length. Start a cursor at 4,092 and visit slots from 0 upward. For each live slot, subtract the record length from the cursor. Its start must equal the resulting cursor, at or beyond the directory end. Tombstones leave the cursor unchanged. This rule rules out overlaps, gaps, and offsets into metadata or the checksum.

Rows concatenate the fields in schema order, with no delimiters. Integer fields occupy eight signed bytes in two's complement. Text fields occupy a two-byte unsigned byte length followed by strict UTF-8. A boolean occupies one byte (`00` or `01`). The fields must consume the exact slot length.

A new slot needs four bytes in addition to the encoded row. A fresh page therefore accepts up to `4092 − 14 − 4 = 4074` row bytes. A one-column text table can store at most 4,072 UTF-8 bytes in a row. Larger inserts fail before allocation. An update keeps its slot and may use the old payload plus free space on that page. Delete discards payload bytes and permanently reserves the slot. Later inserts append descriptors and may reuse the freed payload space. Page IDs and slot IDs are never recycled.

## Inspect a file

On a closed **v1** file with one table and one record, `Format-Hex -Path demo.mdb` in PowerShell shows `MINIDB` at offset `0x0000`, `SCHM` at `0x1000`, and `DATA` at `0x2000`. After its first insert, the file is 12,288 bytes and `2:0` refers to the directory entry at file offset `0x200E`. The format tests contain an independently packed byte fixture for a two-row page.

In the corresponding v3 file, the same schema and data appear at offsets `0x3000` and `0x4000`. The first slot begins at `0x400E` and still has RID `2:0`.
