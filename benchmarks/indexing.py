"""Measure paired SQL workloads on an ordinary table and a table with one int index."""

import argparse
import json
import platform
import random
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

from minidb import Database


def measure(folder: Path, size: int, probes: int, edits: int) -> dict:
    rng = random.Random(2026)
    keys = list(range(size))
    rng.shuffle(keys)
    targets = [rng.randrange(size) for _ in range(probes)]
    times = {}
    oracle = {}
    for indexed in (False, True):
        label = "index" if indexed else "heap"
        path = folder / (label + ".mdb")
        ids = []
        with Database.create(path) as db:
            db.execute("CREATE TABLE items(k int, label text)")
            for k in keys:
                ids.append(db.insert("items", {"k": k, "label": f"item-{k:06d}"}))
            if indexed:
                start = perf_counter()
                db.create_index("by_key", "items", "k")
                times["build_ms"] = (perf_counter() - start) * 1000
        times[label + "_bytes"] = path.stat().st_size
        start = perf_counter()
        with Database.open(path) as db:
            times[label + "_open_ms"] = (perf_counter() - start) * 1000
            for operation, where in [("eq", "k=?"), ("range", "k>=? AND k<?")]:
                statement = "SELECT k FROM items WHERE " + where
                args = [(k,) if operation == "eq" else (k, k + 5) for k in targets]
                start = perf_counter()
                answers = [db.execute(statement, params).rows for params in args]
                times[label + "_" + operation + "_ms"] = (perf_counter() - start) * 1000
                if indexed and answers != oracle[operation]:
                    raise AssertionError("indexed and heap results differ")
                oracle[operation] = answers
            start = perf_counter()
            for i in range(edits):
                db.insert("items", {"k": size + i, "label": "added"})
            times[label + "_insert_ms"] = (perf_counter() - start) * 1000
            start = perf_counter()
            for i, rid in enumerate(ids[:edits]):
                db.update("items", rid, {"k": -i - 1, "label": f"item-{keys[i]:06d}"})
            times[label + "_update_ms"] = (perf_counter() - start) * 1000
    return times


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[250, 1000, 3000])
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--probes", type=int, default=24)
    parser.add_argument("--edits", type=int, default=50)
    args = parser.parse_args()
    if min(
        *args.sizes, args.probes, args.iterations, args.edits
    ) < 1 or args.edits > min(args.sizes):
        parser.error("use positive counts and edits <= every size")
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpu": platform.processor(),
                **vars(args),
            }
        )
    )
    for size in args.sizes:
        runs = []
        for _ in range(args.iterations):
            with tempfile.TemporaryDirectory() as temp:
                runs.append(measure(Path(temp), size, args.probes, args.edits))
        print(
            json.dumps(
                {
                    "rows": size,
                    "median": {
                        k: round(statistics.median(r[k] for r in runs), 3)
                        for k in runs[0]
                    },
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
