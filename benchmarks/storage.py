"""Run against the installed project: python benchmarks/storage.py --rows 1500."""

import argparse
import json
import os
import platform
import random
import statistics
import tempfile
import time
from pathlib import Path

from minidb import Column, Database, Schema


def run(path: Path, count: int) -> dict[str, float | int]:
    start = time.perf_counter()
    with Database.create(path):
        pass
    create = time.perf_counter() - start
    schema = Schema("items", (Column("n", "int"), Column("label", "text")))
    rows = [{"n": i, "label": f"item-{i:06d}"} for i in range(count)]
    with Database.open(path) as db:
        db.create_table(schema)
    db = Database.open(path)
    start = time.perf_counter()
    with db:
        ids = [db.insert("items", row) for row in rows]
    insert = time.perf_counter() - start
    start = time.perf_counter()
    with Database.open(path) as db:
        reopen = time.perf_counter() - start
        order = list(range(count))
        random.Random(2026).shuffle(order)
        start = time.perf_counter()
        fetched = [db.get("items", ids[i]) for i in order]
        lookup = time.perf_counter() - start
        start = time.perf_counter()
        scanned = list(db.scan("items"))
        scan = time.perf_counter() - start
    if fetched != [rows[i] for i in order] or scanned != list(zip(ids, rows)):
        raise RuntimeError("benchmark verification failed")
    return {
        "create_s": create,
        "insert_s": insert,
        "lookup_s": lookup,
        "scan_s": scan,
        "reopen_s": reopen,
        "file_bytes": path.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1500)
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    if min(args.rows, args.iterations) < 1:
        parser.error("rows and iterations must be positive")
    with tempfile.TemporaryDirectory() as folder:
        runs = [
            run(Path(folder) / f"{i}.mdb", args.rows) for i in range(args.iterations)
        ]
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "processor": platform.processor(),
                "logical_cpus": os.cpu_count(),
                "rows": args.rows,
                "iterations": args.iterations,
                "runs": runs,
                "median": {k: statistics.median(r[k] for r in runs) for k in runs[0]},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
