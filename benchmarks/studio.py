"""Measure the real Qt workspace and optionally capture it with disposable sample data."""

import argparse
import itertools
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter


def seed(path, count):
    from minidb import Database

    with Database.create(path) as db, db.transaction():
        db.execute(
            "CREATE TABLE observations (station TEXT, region TEXT, elevation INT, active BOOL)"
        )
        stations = [
            "Alder Ridge",
            "Meridian Point",
            "Juniper Basin",
            "North Channel",
            "Cedar Pass",
            "Harbor Light",
            "Granite Shelf",
            "Willow Creek",
        ]
        regions = ["Coastal", "Highlands", "River valley", "Plateau"]
        for n in range(count):
            db.insert(
                "observations",
                {
                    "station": stations[n % 8],
                    "region": regions[n % 4],
                    "elevation": 30 + n % 377,
                    "active": n % 7 != 0,
                },
            )
        db.execute("CREATE INDEX by_elevation ON observations(elevation)")
        db.execute(
            "CREATE TABLE survey_notes ("
            + ",".join(f"field{i} TEXT" for i in range(12))
            + ")"
        )
        for n in range(24):
            db.insert(
                "survey_notes",
                {f"field{i}": "観測 · café " * (2 + n % 5) for i in range(12)},
            )
        db.execute("CREATE TABLE pending_samples (sample INT)")


def memory_mib():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (n, ctypes.c_size_t)
                for n in ("peak", "working", "qp", "q", "qnp", "qn", "page", "peakpage")
            ]

        info = Counters()
        info.cb = ctypes.sizeof(info)
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        )
        if get_memory(wintypes.HANDLE(-1), ctypes.byref(info), info.cb):
            return round(info.working / 1048576, 2)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=3000)
    parser.add_argument("--capture", type=Path)
    parser.add_argument(
        "--hold",
        action="store_true",
        help="Keep the inspected Windows workspace visible",
    )
    parser.add_argument("--startup-child", action="store_true")
    parser.add_argument("--recover-child", type=Path)
    args = parser.parse_args()
    if args.recover_child:
        from minidb import Database, wal

        def stop(stage):
            if stage == "commit_synced":
                os._exit(86)

        wal.boundary = stop
        with Database.open(args.recover_child) as db:
            db.execute("INSERT INTO pending_samples VALUES(42)")
        return
    from PySide6 import __version__ as qt_version
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from minidb_studio.settings import Settings
    from minidb_studio.window import Window

    app = QApplication([])
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        before = perf_counter()
        win = Window(Settings(root / "preferences.json"))
        win.show()
        app.processEvents()
        metrics = {"empty_window_ms": round((perf_counter() - before) * 1000, 2)}

        def shutdown():
            win.editor.document().setModified(False)
            win.close()
            loop = QEventLoop()
            win.thread.finished.connect(loop.quit)
            if not win.stopped:
                loop.exec()
            app.processEvents()

        if args.startup_child:
            print("ready", flush=True)
            shutdown()
            return
        from_child = perf_counter()
        with subprocess.Popen(
            [sys.executable, __file__, "--startup-child"],
            stdout=subprocess.PIPE,
            text=True,
        ) as child:
            assert child.stdout.readline().strip() == "ready"
            metrics["process_start_to_window_ms"] = round(
                (perf_counter() - from_child) * 1000, 2
            )
            child.communicate(timeout=20)
            assert child.returncode == 0

        def capture(name):
            if args.capture:
                args.capture.mkdir(parents=True, exist_ok=True)
                app.processEvents()
                assert win.grab().save(str(args.capture / f"{name}.png"))

        def operate(name, *values):
            loop = QEventLoop()
            win.worker.completed.connect(loop.quit)
            start = perf_counter()
            assert win.submit(name, *values)
            loop.exec()
            win.worker.completed.disconnect(loop.quit)
            app.processEvents()
            assert not win.notice.text(), win.notice.text()
            return round((perf_counter() - start) * 1000, 2)

        capture("first-launch")
        path = root / "fieldwork.mdb"
        seed(path, args.rows)
        metrics["open_ms"] = operate("open", str(path))
        metrics["reopen_ms"] = operate("open", str(path))
        win.current_table = "observations"
        win.tabs.setCurrentIndex(1)
        metrics["first_128_ms"] = operate("browse", "observations")
        win.data.selectRow(2)
        idx = win.data.dataset.index(2, 0)
        win.data.setCurrentIndex(idx)
        capture("table-grid")
        scroll = win.data.verticalScrollBar()
        start = perf_counter()
        for i in range(100):
            scroll.setValue(round(scroll.maximum() * i / 99))
            app.processEvents()
        metrics["100_scroll_steps_ms"] = round((perf_counter() - start) * 1000, 2)
        win.tabs.setCurrentIndex(0)
        query = "SELECT station, region, elevation, active\nFROM observations\nWHERE elevation >= 240\nORDER BY elevation DESC\nLIMIT 24;"
        win.editor.setPlainText(query)
        metrics["select_ms"] = operate("sql", query)
        win.results.setColumnWidth(0, 178)
        win.results.setColumnWidth(1, 142)
        capture("sql-workspace")
        grid = win.results.dataset
        metrics["csv_24_ms"] = operate(
            "export", str(root / "survey.csv"), grid.columns, grid.rows, "CSV"
        )
        metrics["plan_ms"] = operate("sql", "EXPLAIN " + query)
        capture("query-plan")
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(perf_counter()))
        t0 = perf_counter()
        timer.start()
        metrics["build_index_ms"] = operate(
            "sql", "CREATE INDEX by_station ON observations(station)"
        )
        timer.stop()
        points = [t0, *ticks, perf_counter()]
        metrics["index_heartbeat_max_ms"] = round(
            max(b - a for a, b in itertools.pairwise(points)) * 1000, 2
        )
        metrics["index_heartbeat_ticks"] = len(ticks)
        metrics["check_ms"] = operate("check")
        win.tabs.setCurrentIndex(5)
        capture("health")
        start = perf_counter()
        win.settings.values["theme"] = "Light"
        win.apply_settings()
        app.processEvents()
        metrics["theme_ms"] = round((perf_counter() - start) * 1000, 2)
        win.tabs.setCurrentIndex(3)
        capture("light-indexes")
        start = perf_counter()
        for width, height in [(900, 560), (720, 480), (1200, 760)]:
            win.resize(width, height)
            app.processEvents()
        metrics["three_resizes_ms"] = round((perf_counter() - start) * 1000, 2)
        operate("close", True)
        child = subprocess.run(
            [sys.executable, __file__, "--recover-child", str(path)], check=False
        )
        assert child.returncode == 86
        metrics["recovery_open_ms"] = operate("open", str(path))
        assert win.snap["recovered"]
        operate("sql", "SELECT * FROM pending_samples")
        assert win.results.dataset.rows == [{"sample": 42}]
        metrics.update(
            rows=args.rows,
            working_set_mib=memory_mib(),
            python=sys.version.split()[0],
            qt=qt_version,
            platform=platform.platform(),
            backend=app.platformName(),
        )
        print(json.dumps(metrics, indent=2), flush=True)
        if args.hold:
            win.settings.values["theme"] = "Dark"
            win.apply_settings()
            win.tabs.setCurrentIndex(0)
            operate("sql", query)
            loop = QEventLoop()
            win.thread.finished.connect(loop.quit)
            loop.exec()
        else:
            shutdown()


if __name__ == "__main__":
    main()
