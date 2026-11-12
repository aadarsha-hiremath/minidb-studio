"""Terminate without cleanup at one selected storage boundary in a disposable DB."""

import os
import sys
from pathlib import Path

from minidb import Database, wal


def change(db: Database) -> None:
    db.execute("UPDATE accounts SET balance=90 WHERE name='Ada'")
    db.execute("UPDATE accounts SET balance=110 WHERE name='Lin'")
    db.execute("CREATE TABLE events (k text, n int)")
    db.execute("CREATE INDEX events_k ON events(k)")
    for n in range(170):
        db.insert("events", {"k": f"entry-{n:03}", "n": n})
    db.execute("DROP INDEX balance_idx")
    db.execute("CREATE INDEX by_balance ON accounts(balance)")


def main() -> None:
    path, mode, stop = sys.argv[1:4]

    def crash(label):
        if label == stop:
            if mode == "hold":
                print(f"locked {os.getpid()}", flush=True)
                sys.stdin.buffer.read(1)
            os._exit(86)  # No Python cleanup, rollback, close or finalizers.

    wal.boundary = crash
    original = wal.put

    def write_prefix(stream, offset, data):
        stage = {
            b"COMMIT01": "partial_marker",
            b"INTN": "partial_intent",
            b"DONE": "partial_done",
            b"DATA": "partial_heap",
        }
        label = (
            "partial_frame"
            if len(data) == wal.FRAME_SIZE
            else stage.get(data[:8], stage.get(data[:4]))
        )
        if label == stop:
            original(stream, offset, data[: len(data) // 2])
            os._exit(86)
        original(stream, offset, data)

    wal.put = write_prefix
    with Database.open(Path(path)) as db:
        if mode == "write":
            with db.transaction():
                change(db)
                crash("staged")


if __name__ == "__main__":
    main()
