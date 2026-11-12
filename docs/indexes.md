# Disk-backed B+ trees

An index definition names a single table column and stores a root page ID. Each leaf holds sorted triples of encoded value, record page, and slot. This composite ordering keeps duplicates distinct. Internal nodes hold child page IDs and routing fences in the same order. A record belongs to the right child at an equal fence. Lookup reads one node per level. A range starts at the leaf containing its lower bound, then follows next-leaf links until reaching the upper bound.

For equality, the executor searches between two composite keys that bracket every legal RID for that value. Strict or inclusive ranges use the same synthetic boundaries. A matched RID leads directly to its table page. Heap reads still validate and decode the row. The on-disk layouts, key encodings and limits are specified in [the file format](format.md).

## Insert, split and delete

Insertion descends from the root, retaining only the path. When a leaf exceeds capacity, the upper half moves to a new right sibling. The old leaf points to that sibling, which inherits the old successor. A copy of the right leaf's first key becomes the parent's routing fence. An overflowing internal node promotes its median fence and divides its children between two nodes. Splits can propagate to the root, where a new root makes the tree one level taller.

Deletion removes exactly one composite leaf entry. Internal fences remain valid lower bounds even when the record that supplied a fence disappears. Sparse and empty leaves stay linked at the original depth. There is no redistribution, merge, or root shrink. Dropping an index retires its definition and node pages. A later creation appends new pages. Retired pages are never reused in this phase.

The same maintenance runs for SQL, the Python record API, and the original CLI. Index creation checks every existing key before the first write. Inserts and updates validate their encoded row and keys before modifying the heap. Updates remove and insert entries only for changed indexed values. Across tables, index names must be unique. A column can have at most one index.

## Validation and failures

Opening an indexed file performs an integrity check before exposing any queries. It verifies every page checksum and walks each tree from its published root. The walk checks ownership, page and child IDs, ordering, routing intervals, cycles, equal leaf depth, leaf-chain order, and unreachable nodes. Each leaf entry must identify a live row in the correct table and match its indexed value. Strict composite ordering precludes repeated identical entries. Matching the resulting entry count to the live row count detects omitted rows. Validation uses sets of page IDs, the descent stack, and individual decoded pages. Reopening does not rebuild or retain the keys in memory.

Run `minidb check school.mdb` to recover and validate the entire database under its exclusive lock. For a healthy v3 database, a DROP INDEX and CREATE INDEX within `db.transaction()` replace the index atomically. The original survives rollback. Back up the closed, checkpointed file pair before working on a copy.

Heap rows, catalog metadata, index nodes, and root changes use the same overlay. An interrupted commit either leaves the old index and heap or recovers the complete new pair. If consistency cannot be established, open fails and retains the evidence. [Transaction recovery](transactions.md).
