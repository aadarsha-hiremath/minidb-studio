import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minidb import Database, NoSpace, ValidationError, sql


class ParserTests(unittest.TestCase):
    def test_token_offsets_and_literals(self):
        tokens = sql.tokenize("  sElEcT\t'it''s ?' != -42;")
        self.assertEqual(
            [(t.kind, t.pos) for t in tokens],
            [
                ("word", 2),
                ("str", 9),
                ("op", 19),
                ("int", 22),
                ("symbol", 25),
                ("end", 26),
            ],
        )

    def test_ast_and_parameter_positions(self):
        ast, n = sql.parse(
            "explain Select a,b from T where a>=? AND b!='it''s ?' order BY a desc limit ?;"
        )
        self.assertEqual(n, 2)
        self.assertEqual(
            ast,
            sql.Explain(
                sql.Select(
                    "T",
                    ("a", "b"),
                    (
                        sql.Predicate("a", ">=", sql.Parameter(0)),
                        sql.Predicate("b", "!=", "it's ?"),
                    ),
                    "a",
                    True,
                    sql.Parameter(1),
                )
            ),
        )

    def test_malformed_and_unsupported(self):
        for query in (
            "",
            "SELECT x FROM t WHERE k ! 1",
            "SELECT * FROM t WHERE s='bad",
            "SELECT count(*) FROM t",
            "SELECT x FROM t GROUP BY x",
            "SELECT * FROM t JOIN y",
            "SELECT * FROM t WHERE x=1 OR x=2",
            "SELECT * FROM t; SELECT * FROM t",
            "DELETE t",
            "DROP TABLE t",
            "CREATE TABLE t(x text,)",
            "INSERT INTO t VALUES ()",
            "UPDATE t SET x=1 AND y=2",
            "SELECT * FROM (SELECT x FROM t)",
            "SELECT * FROM t LIMIT 1.0",
            "SELECT * FROM t WHERE n=9223372036854775808",
        ):
            with (
                self.subTest(query=query),
                self.assertRaisesRegex(sql.SQLError, "character"),
            ):
                sql.parse(query)

    def test_resource_bounds(self):
        with self.assertRaises(sql.SQLError):
            sql.parse(" " * 65537)
        with self.assertRaises(sql.SQLError):
            sql.parse("SELECT * FROM t LIMIT " + "9" * 5000)


class SQLTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "records.mdb"
        self.db = Database.create(self.path)
        self.addCleanup(lambda: self.db.close())
        self.q = self.db.execute
        self.q("CREATE TABLE T (n INTEGER, s TEXT, b BOOLEAN)")

    def test_binding_and_projection(self):
        value = "O'Brien; this is text, including ? and = signs"
        result = self.q("INSERT INTO T(s,b,n) VALUES(?, TRUE,-7)", (value,))
        self.assertEqual(self.db.get("T", result.rid), {"n": -7, "s": value, "b": True})
        self.assertEqual(
            self.q("SELECT s,n FROM T WHERE s=?", (value,)).rows,
            [{"s": value, "n": -7}],
        )
        self.assertEqual(self.q("SELECT * FROM T WHERE s='O''Brien'").rowcount, 0)

    def test_types_names_and_empty_tables(self):
        for query, params in [
            ("SELECT * FROM T WHERE n=?", (True,)),
            ("SELECT * FROM T WHERE s=?", (42,)),
            ("SELECT * FROM T WHERE b=?", (0,)),
            ("SELECT * FROM T WHERE n=?", (2**63,)),
            ("SELECT * FROM T WHERE s=?", ("\ud800",)),
            ("SELECT bad FROM T", ()),
            ("SELECT n,n FROM T", ()),
            ("SELECT * FROM T ORDER BY missing", ()),
            ("SELECT * FROM T LIMIT -1", ()),
            ("SELECT * FROM T LIMIT true", ()),
            ("UPDATE T SET n='x'", ()),
            ("UPDATE T SET n=1,n=2", ()),
            ("INSERT INTO T(n,s) VALUES(1,'s')", ()),
            ("INSERT INTO T(n) VALUES(1,2)", ()),
            ("SELECT * FROM T WHERE n=?", ()),
            ("SELECT * FROM T", (1,)),
            ("SELECT * FROM T WHERE n=?", "1"),
            ("EXPLAIN SELECT * FROM T LIMIT ?", (False,)),
        ]:
            with (
                self.subTest(query=query, params=params),
                self.assertRaises(ValidationError),
            ):
                self.q(query, params)

    def test_sql_dml_and_order_ties(self):
        for n in (2, 1, 2):
            self.q(
                "INSERT INTO T VALUES(?,?,false)",
                (n, str(self.db._table("T").live_count)),
            )
        self.q("CREATE INDEX by_n ON T(n)")
        self.assertEqual(
            self.q("SELECT s FROM T ORDER BY n DESC LIMIT ?", (2,)).rows,
            [{"s": "0"}, {"s": "2"}],
        )
        self.assertEqual(self.q("UPDATE T SET n=5,b=true WHERE n>=2").rowcount, 2)
        self.assertEqual(
            self.q("SELECT s FROM T WHERE n=5 AND b=true").rows,
            [{"s": "0"}, {"s": "2"}],
        )
        self.assertEqual(self.q("SELECT * FROM T LIMIT 0").rows, [])
        self.assertEqual(self.q("DELETE FROM T WHERE n!=5").rowcount, 1)
        self.assertEqual(self.q("DELETE FROM T").rowcount, 2)

    def test_explain_matches_access_path(self):
        self.q("CREATE INDEX by_n ON T(n)")
        self.q("CREATE INDEX by_s ON T(s)")
        self.q("INSERT INTO T VALUES(-1,'hi',true)")
        tail = " FROM T WHERE s >= 'a' AND n=-1"
        self.assertEqual(
            self.q("EXPLAIN SELECT *" + tail).plan, "Index Scan using by_n on T"
        )
        with patch.object(
            self.db, "scan", side_effect=AssertionError("unexpected heap scan")
        ):
            self.assertEqual(self.q("SELECT n" + tail).rows, [{"n": -1}])
            self.assertEqual(self.q("SELECT n FROM T WHERE s < 'z'").rows, [{"n": -1}])
        self.assertIn(
            "Sequential Scan", self.q("EXPLAIN SELECT * FROM T WHERE n!=1").plan
        )
        self.assertIn(
            "Index Range Scan", self.q("EXPLAIN SELECT * FROM T WHERE s>'x'").plan
        )

    def test_multirow_space_failure_rolls_back_completed_prefix(self):
        # Place the two targets on different pages, then free space only on page one.
        a = self.q("INSERT INTO T VALUES(1,'',true)").rid
        filler = self.q("INSERT INTO T VALUES(0,?,false)", ("a" * 4000,)).rid
        b = self.q("INSERT INTO T VALUES(1,?,true)", ("b" * 100,)).rid
        self.q("INSERT INTO T VALUES(0,?,false)", ("a" * 3700,))
        self.assertNotEqual(a.page, b.page)
        self.db.delete("T", filler)
        self.q("CREATE INDEX by_n ON T(n)")
        with self.assertRaises(NoSpace):
            self.q("UPDATE T SET s=?,n=2 WHERE n=1", ("u" * 1000,))
        self.db.close()
        with Database.open(self.path) as db:
            self.assertEqual(db.execute("SELECT n FROM T WHERE n=2").rows, [])
            self.assertEqual(db.get("T", a)["n"], 1)
            self.assertEqual(db.get("T", b)["n"], 1)

    def test_cli_processes_reopen_index(self):
        self.db.close()

        def cli(statement):
            return subprocess.run(
                [sys.executable, "-m", "minidb", "sql", str(self.path), statement],
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            ).stdout.strip()

        cli("INSERT INTO T VALUES(101,'Ada',true)")
        cli("CREATE INDEX students_id ON T(n)")
        self.assertIn(
            "Index Scan using students_id", cli("EXPLAIN SELECT * FROM T WHERE n=101")
        )
        self.assertEqual(
            json.loads(cli("SELECT s FROM T WHERE n=101"))["rows"], [{"s": "Ada"}]
        )
