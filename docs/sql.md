# SQL language and execution

Each call parses exactly one statement. Keywords and type names are case-insensitive. Names preserve case and use the Phase 1 identifier rules. Keywords are contextual. Identifiers are unquoted. The lexer accepts whitespace between tokens and reports one-based character positions for syntax errors. A statement can contain at most 65,536 characters.

```text
statement  = create_table | insert | select | update | delete
           | create_index | drop_index | EXPLAIN select | BEGIN | COMMIT | ROLLBACK
create_table = CREATE TABLE name "(" name type { "," name type } ")"
type       = INT | INTEGER | TEXT | BOOL | BOOLEAN
insert     = INSERT INTO name [ "(" names ")" ] VALUES "(" values ")"
select     = SELECT ( "*" | names ) FROM name [ where ]
             [ ORDER BY name [ ASC | DESC ] ] [ LIMIT value ]
update     = UPDATE name SET assignment { "," assignment } [ where ]
delete     = DELETE FROM name [ where ]
where      = WHERE predicate { AND predicate }
predicate  = name ( "=" | "!=" | "<" | "<=" | ">" | ">=" ) value
assignment = name "=" value
create_index = CREATE INDEX name ON name "(" name ")"
drop_index = DROP INDEX name
names      = name { "," name }
values     = value { "," value }
value      = signed_decimal_integer | single_quoted_text | TRUE | FALSE | "?"
```

An optional semicolon ends the statement. Inside a string, two single quotes represent one quote, as in `'O''Brien'`. Backslashes are ordinary characters. Integer literals can have a leading minus sign. Bind parameters by passing a tuple or list of the exact length. Binding fills typed AST operands in source order. An identifier, operator, or SQL clause cannot be supplied as a parameter. The CLI accepts literal SQL. Use the Python API for programmatic values.

```python
from minidb import Database

with Database.open("school.mdb") as db:
    answer = db.execute(
        "SELECT name FROM students WHERE id >= ? AND id < ? ORDER BY id LIMIT ?",
        (100, 200, 5),
    )
    print(answer.rows)
    print(db.execute("EXPLAIN SELECT * FROM students WHERE id = ?", (101,)).plan)
```

`execute` returns a `Result`. SELECT populates `rows` (a list of projected dictionaries) and `rowcount`. UPDATE and DELETE report the number of matching rows. INSERT reports one affected row and its physical `rid`. DDL reports zero. EXPLAIN returns its access strategy in `plan`. The CLI prints JSON containing `rows`, `rowcount`, and the inserted `rid` when present. EXPLAIN prints plain text. Grammar and binding failures raise `SQLError`, a `ValidationError` subclass. Schema, capacity, storage and lookup errors use the existing engine exceptions.

All fields must be supplied once for INSERT. Omitting the column list uses schema order. UPDATE replaces only assigned fields and preserves each row's physical ID. Omitting WHERE matches every row. A predicate compares a column with a value of its declared type. Integers are signed 64-bit. Booleans sort false before true. Text uses Unicode code-point order without normalization, locale rules or case folding. Projection names must be distinct and ORDER BY may reference an unprojected column. The default row order is ascending physical RID. ORDER BY ties retain ascending RID even for DESC. LIMIT accepts a nonnegative signed 64-bit integer after binding, including zero.

SQL text operands, including bound parameters used only for comparisons, are limited to 65,536 characters. Row and stored index key limits apply separately when changing data.

## How the planner chooses

The planner takes the first indexed equality predicate in source order. If none exists, it takes the first indexed inequality with a lower or upper bound. It intersects all usable bounds on the chosen column, descends the B+ tree, then follows leaf links. It reapplies every predicate to the fetched heap rows. A lone `!=`, an unindexed predicate, and a query without WHERE use a sequential scan. EXPLAIN validates the same binding and reports the access path selected by the executor.

```sql
EXPLAIN SELECT name FROM students WHERE id = 101
-- output: Index Scan using students_id on students
EXPLAIN SELECT name FROM students WHERE id >= 100 AND id < 110
-- output: Index Range Scan using students_id on students
EXPLAIN SELECT name FROM students WHERE name = 'Ada'
-- output when only id is indexed: Sequential Scan on students
```

The lines beginning with two dashes above label output. SQL comments are outside the grammar. The planner uses no statistics, join enumeration, index intersection, or sort elimination. The executor materializes matches, restores RID order on an index path, applies any explicit sort, then takes the limit. UPDATE and DELETE freeze the matching rows before modifying the first row so changing an indexed key cannot revisit or skip a target. Large result sets and statements affecting many rows use memory proportional to the matches. All rows of a mutating statement share one transaction. An implicit transaction rolls back on a statement error. Inside an explicit transaction, a failed mutation or SQL query makes the transaction rollback-only.

Joins, aggregates, GROUP BY, OR, NULL, subqueries, arithmetic, savepoints, and multiple statements per call are outside this subset.

Use `minidb session FILE` for one statement per input line on the same connection. EOF with a pending transaction exits with an error and discards staged changes. SQL control commands take no parameters. [Transaction states and commit errors](transactions.md).
