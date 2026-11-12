"""Time real durable commits and recovery. Optionally compare a Phase 2 source checkout."""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from minidb import Database


def inserts(path: Path, count: int) -> float:
    with Database.create(path) as db:
        db.execute("CREATE TABLE t(n int, s text)")
        start = perf_counter()
        for i in range(count):
            db.insert("t", {"n": i, "s": f"item-{i:05}"})
    return (perf_counter() - start) * 1000


def workload(root: Path, n: int) -> dict:
    from minidb import wal

    path = root / "batch.mdb"
    journal = Path(str(path) + ".wal")
    result = {"implicit_ms": inserts(root / "single.mdb", n)}
    with Database.create(path) as db:
        db.execute("CREATE TABLE t(n int, s text)")
        start = perf_counter()
        db.begin()
        for i in range(n):
            db.insert("t", {"n": i, "s": f"item-{i:05}"})
        result["before_bytes"] = path.stat().st_size
        ticks = {}

        def observe(phase):
            if phase in ("commit_synced", "checkpoint_synced"):
                ticks[phase] = perf_counter()
            if phase == "commit_synced":
                result["wal_peak_bytes"] = journal.stat().st_size

        commit_start = perf_counter()
        with patch.object(wal, "boundary", observe):
            db.commit()
        finish = perf_counter()
        result.update(
            batch_ms=(finish - start) * 1000,
            commit_ms=(finish - commit_start) * 1000,
            checkpoint_ms=(ticks["checkpoint_synced"] - ticks["commit_synced"]) * 1000,
            after_bytes=path.stat().st_size,
            wal_clean_bytes=journal.stat().st_size,
        )
        start = perf_counter()
        assert db.execute("UPDATE t SET s='updated'").rowcount == n
        result["update_ms"] = (perf_counter() - start) * 1000
        db.execute("CREATE INDEX by_n ON t(n)")
        start = perf_counter()
        assert db.execute("UPDATE t SET n=-1 WHERE n>=0").rowcount == n
        result["indexed_update_ms"] = (perf_counter() - start) * 1000
        db.begin()
        db.execute("DELETE FROM t WHERE n=-1")
        start = perf_counter()
        db.rollback()
        result["rollback_ms"] = (perf_counter() - start) * 1000
    start = perf_counter()
    with Database.open(path) as db:
        result["open_ms"] = (perf_counter() - start) * 1000
        start = perf_counter()
        assert db.execute("DELETE FROM t WHERE n=-1").rowcount == n
        result["delete_ms"] = (perf_counter() - start) * 1000
    # The child exits immediately after durable commit. Child startup is outside recovery timing.
    cmd = [sys.executable, str(Path(__file__).resolve()), "--crash", str(path), str(n)]
    p = subprocess.run(cmd, check=False, timeout=180)
    if p.returncode != 86:
        raise RuntimeError(f"crash setup returned {p.returncode}")
    start = perf_counter()
    with Database.open(path) as db:
        result["recovery_ms"] = (perf_counter() - start) * 1000
        assert db.execute("SELECT n FROM t WHERE n>=0 ORDER BY n").rows == [
            {"n": i} for i in range(n)
        ]
    return result


def main() -> None:
    if sys.argv[1:2] == ["--crash"]:
        from minidb import wal

        def crash(phase):
            if phase == "commit_synced":
                os._exit(86)

        wal.boundary = crash
        with Database.open(sys.argv[2]) as db, db.transaction():
            for i in range(int(sys.argv[3])):
                db.insert("t", {"n": i, "s": "recovered"})
        raise RuntimeError("expected a process exit")
    if sys.argv[1:2] == ["--legacy"]:
        print(inserts(Path(sys.argv[2]), int(sys.argv[3])))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[16, 64, 256])
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--phase2-source", type=Path)
    args = parser.parse_args()
    if min(*args.sizes, args.iterations) < 1:
        parser.error("use positive sizes and iterations")
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "iterations": args.iterations,
            }
        )
    )
    for count in args.sizes:
        runs = []
        for _ in range(args.iterations):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                sample = workload(root, count)
                if args.phase2_source:
                    if not (args.phase2_source / "minidb" / "database.py").is_file():
                        parser.error("expected a Phase 2 source checkout")
                    env = dict(os.environ, PYTHONPATH=str(args.phase2_source.resolve()))
                    sample["phase2_ms"] = float(
                        subprocess.check_output(
                            [
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "--legacy",
                                str(root / "legacy.mdb"),
                                str(count),
                            ],
                            env=env,
                            cwd=root,
                            text=True,
                        )
                    )
                runs.append(sample)
        print(
            json.dumps(
                {
                    "rows": count,
                    **{
                        k: round(statistics.median(r[k] for r in runs), 3)
                        for k in runs[0]
                    },
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
