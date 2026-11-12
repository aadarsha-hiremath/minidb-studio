import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minidb import Database


class CLITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "demo with spaces.mdb"

    def cli(self, command, *args, ok=True):
        result = subprocess.run(
            [sys.executable, "-m", "minidb", command, str(self.path), *args],
            text=True,
            check=False,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0 if ok else 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return (result.stdout if ok else result.stderr).strip()

    def test_separate_process_persistence(self):
        self.cli("create")
        self.cli("create-table", "notes", "id:int", "text:text", "saved:bool")
        self.assertEqual(json.loads(self.cli("tables")), ["notes"])
        self.assertEqual(json.loads(self.cli("schema", "notes"))["text"], "text")
        rid = self.cli("insert", "notes", "text=a=b c", "saved=false", "id=12")
        self.assertEqual(rid, "2:0")
        self.assertEqual(
            json.loads(self.cli("get", "notes", rid)),
            {"text": "a=b c", "saved": False, "id": 12},
        )
        self.cli("update", "notes", rid, "id=13", "text=", "saved=true")
        self.assertEqual(
            json.loads(self.cli("scan", "notes")),
            {"rid": rid, "row": {"id": 13, "text": "", "saved": True}},
        )
        self.cli("delete", "notes", rid)
        self.assertEqual(self.cli("scan", "notes"), "")
        self.assertIn("deleted", self.cli("get", "notes", rid, ok=False))

    def test_validation_messages(self):
        self.cli("create")
        self.cli("create-table", "t", "n:int", "b:bool")
        for fields in [
            ("n=1",),
            ("n=x", "b=true"),
            ("n=0", "b=no"),
            ("n=1", "n=2"),
            ("wrong=1",),
            ("oops",),
        ]:
            self.assertTrue(self.cli("insert", "t", *fields, ok=False))
        self.assertIn("PAGE:SLOT", self.cli("get", "t", "bad", ok=False))

    def test_another_process_cannot_open_locked_file(self):
        with Database.create(self.path):
            self.assertIn("in use", self.cli("tables", ok=False))
        self.assertEqual(self.cli("tables"), "[]")
