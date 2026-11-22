"""Fail-fast checks against an installed MiniDB package on both runner platforms."""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import sysconfig
import unittest
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent
CORE_MODULES = {
    "test_cli",
    "test_database",
    "test_format",
    "test_indexes",
    "test_legacy_compat",
    "test_recovery",
    "test_sql",
    "test_transactions",
}


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


def run_suite(kind):
    root = ROOT if kind == "core" else ROOT / "studio"
    suite = unittest.defaultTestLoader.discover(str(root))
    ids = set(test_ids(suite))
    expected = CORE_MODULES if kind == "core" else {"test_studio"}
    modules = {name.split(".", 1)[0] for name in ids}
    required = (
        {
            "test_recovery.RecoveryTests.test_process_termination_matrix",
            "test_recovery.RecoveryTests.test_exclusive_lock_and_external_kill_during_recovery",
            "test_legacy_compat.LegacyMigrationTests.test_migrates_heap_and_indexed_v1",
        }
        if kind == "core"
        else set()
    )
    if not expected <= modules or not required <= ids:
        raise SystemExit(
            f"Incomplete discovery: missing modules {expected - modules}, missing tests {required - ids}"
        )
    minimum = 77 if kind == "core" else 13
    if len(ids) < minimum:
        raise SystemExit(f"Expected at least {minimum} tests, discovered {len(ids)}")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.skipped:
        raise SystemExit(
            f"{kind} validation failed, skipped {len(result.skipped)} tests"
        )


def installed():
    import minidb

    assert Path(minidb.__file__).is_relative_to(Path(sys.prefix)), minidb.__file__
    dist = metadata.distribution("minidb-engine")
    assert dist.version == minidb.__version__ == "0.4.0"
    assert dist.metadata["Requires-Python"] == ">=3.11"
    assert dist.metadata["License-Expression"] == "MIT"
    assert dist.requires == ['PySide6-Essentials<7,>=6.8; extra == "gui"']
    assert dist.read_text("licenses/LICENSE").startswith("MIT License")
    assert {(e.group, e.name, e.value) for e in dist.entry_points} == {
        ("console_scripts", "minidb", "minidb.cli:main"),
        ("gui_scripts", "minidb-gui", "minidb_studio:main"),
    }


def core_smoke():
    installed()
    assert importlib.util.find_spec("PySide6") is None
    from minidb import Database

    cli = Path(sysconfig.get_path("scripts")) / (
        "minidb.exe" if os.name == "nt" else "minidb"
    )

    def command(*args):
        return subprocess.check_output([str(cli), *args], text=True, timeout=20).strip()

    assert command("--version") == "MiniDB 0.4.0"
    with TemporaryDirectory() as folder:
        path = str(Path(folder) / "smoke.mdb")
        command("create", path)
        command("sql", path, "CREATE TABLE records(n INT)")
        with Database.open(path) as db:
            db.execute("INSERT INTO records VALUES (?)", (7,))
        assert json.loads(command("sql", path, "SELECT * FROM records")) == {
            "rows": [{"n": 7}],
            "rowcount": 1,
        }
        assert command("check", path) == "ok"


def studio_smoke():
    installed()
    for package in ("PySide6-Essentials", "shiboken6"):
        print(package, metadata.version(package), flush=True)
    subprocess.run(
        [sys.executable, str(ROOT / "studio" / "launcher.py")], timeout=45, check=True
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "check", choices=("core", "studio", "core-smoke", "studio-smoke")
    )
    check = parser.parse_args().check
    if check == "core-smoke":
        core_smoke()
    elif check == "studio-smoke":
        studio_smoke()
    else:
        installed()
        run_suite(check)
