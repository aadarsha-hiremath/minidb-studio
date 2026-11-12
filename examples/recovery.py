"""Run an abrupt-exit recovery demonstration on two disposable databases."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from minidb import Database, wal


def child(path: Path, stage: str) -> None:
    def stop(label: str) -> None:
        if label == stage:
            os._exit(86)

    wal.boundary = stop
    with Database.open(path) as db, db.transaction():
        db.execute("UPDATE accounts SET balance=90 WHERE name='Ada'")
        db.execute("UPDATE accounts SET balance=110 WHERE name='Lin'")


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        for stage, expected in [
            ("frame_written", [100, 100]),
            ("page_installed", [90, 110]),
        ]:
            path = Path(folder) / (stage + ".mdb")
            with Database.create(path) as db, db.transaction():
                db.execute("CREATE TABLE accounts(name text,balance int)")
                db.execute("CREATE INDEX by_balance ON accounts(balance)")
                db.execute("INSERT INTO accounts VALUES('Ada',100)")
                db.execute("INSERT INTO accounts VALUES('Lin',100)")
            p = subprocess.run(
                [sys.executable, __file__, str(path), stage], check=False, timeout=30
            )
            if p.returncode != 86:
                raise RuntimeError("writer did not exit at the requested boundary")
            journal = Path(str(path) + ".wal")
            before = journal.stat().st_size
            with Database.open(path) as db:
                balances = [
                    r["balance"]
                    for r in db.execute(
                        "SELECT balance FROM accounts WHERE balance>=0 ORDER BY name"
                    ).rows
                ]
                if balances != expected:
                    raise RuntimeError("unexpected recovered balances")
            print(
                f"{stage}: balances {balances}, WAL {before} -> {journal.stat().st_size} bytes"
            )


if __name__ == "__main__":
    if len(sys.argv) == 3:
        child(Path(sys.argv[1]), sys.argv[2])
    else:
        main()
