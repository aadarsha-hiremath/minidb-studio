"""Exercise actual historical writers, their original suite, and migration in a fresh install."""

import io
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from minidb import Database, RecordID, TransactionError

PHASE1 = "33d7ffa820f52ae613b713689424d00a6dd99669"
PHASE2 = "8d0fb054ee15bff8b2f0867cb232c7a5da22bf03"


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for revision in (PHASE1, PHASE2):
            old = root / revision
            archive = subprocess.check_output(
                ["git", "archive", "--format=zip", revision, "minidb", "tests"],
                cwd=repo,
            )
            with zipfile.ZipFile(io.BytesIO(archive)) as files:
                files.extractall(old)
            legacy = old / "legacy.mdb"
            new = old / "migrated.mdb"

            def run(*args, old=old, ok=True):
                p = subprocess.run(
                    [sys.executable, "-m", "minidb", *map(str, args)],
                    env=env,
                    cwd=old,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if p.returncode != (0 if ok else 1):
                    raise RuntimeError(p.stdout + p.stderr)
                return p.stdout.strip() if ok else p.stderr.strip()

            run("create", legacy)
            run("create-table", legacy, "records", "n:int", "s:text")
            rid = run("insert", legacy, "records", "n=-7", "s=before")
            if revision == PHASE2:
                run("sql", legacy, "CREATE INDEX idx ON records(n)")
                subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                    cwd=old,
                    env=env,
                    check=True,
                    timeout=180,
                )
            original = legacy.read_bytes()
            with Database.open(legacy) as db:
                try:
                    db.execute("DELETE FROM records")
                except TransactionError:
                    pass
                else:
                    raise AssertionError("legacy mutation accepted")
            Database.migrate(legacy, new)
            assert original == legacy.read_bytes()
            assert original[4096:] == new.read_bytes()[12288:]
            with Database.open(new) as db:
                assert db.get("records", RecordID(*map(int, rid.split(":")))) == {
                    "n": -7,
                    "s": "before",
                }
                if revision == PHASE2:
                    assert (
                        "using idx"
                        in db.execute("EXPLAIN SELECT * FROM records WHERE n=-7").plan
                    )
                db.execute("UPDATE records SET s='after' WHERE n=-7")
            with Database.open(new) as db:
                assert db.execute("SELECT s FROM records WHERE n=-7").rows == [
                    {"s": "after"}
                ]
            assert "unsupported format version" in run("tables", new, ok=False)
            assert original == legacy.read_bytes()
            print(
                f"Verified migration from {revision[:7]} and legacy reader rejection."
            )


if __name__ == "__main__":
    main()
