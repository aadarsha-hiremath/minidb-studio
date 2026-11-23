# MiniDB 0.4.0 — Source Release

MiniDB 0.4.0 is an educational Python database engine with an optional native desktop application, MiniDB Studio.

## Features

- Typed tables with persistent binary storage
- Single-table SQL queries and data manipulation
- Persistent B+ tree indexes
- Transactions with commit and rollback
- Binary write-ahead logging and recovery
- Database integrity verification
- Database format migration
- Native Qt-based Studio interface
- SQL editor and query-plan inspection
- CSV and JSON result exports
- Persistent Studio preferences

## Installation

Python 3.11 or later is required.

Install the engine from the [public source repository](https://github.com/aadarsha-hiremath/minidb-studio) checkout. The CLI is available through the installed command or as a Python module.

    python -m pip install .
    minidb --help
    python -m minidb --help

Install the GUI extra from the same checkout, then launch Studio.

    python -m pip install ".[gui]"
    minidb-gui

On Linux with Python 3.11, the GUI extra excludes PySide6 Essentials 6.12.0 because an upstream binding bug can abort the interpreter during Qt object cleanup. [Upstream Qt issue](https://qt-project.atlassian.net/browse/PYSIDE-3509) · [Installation and tests](../README.md).

## Distribution

This repository distributes source code only.

No standalone Windows executable, installer, or prebuilt
native application is included in this release.

MiniDB's own source code is [MIT licensed](../LICENSE). Installing the GUI extra obtains PySide6 Essentials and Shiboken separately. The upstream community distribution uses LGPLv3/GPLv3 terms, and Qt also offers commercial licensing. The source archives include reference LGPLv3 and GPLv3 texts. [Qt licensing](https://doc.qt.io/qt-6/licensing.html) · [Qt for Python licensing](https://doc.qt.io/qtforpython-6/commercial/index.html).

## Limitations

MiniDB is intended for educational and experimental use.
It supports single-owner local database access and a
limited SQL feature set.

SQL is limited to single-table operations. Joins, aggregation, MVCC, and simultaneous readers are outside this version.

WAL durability depends on local filesystem and device sync behavior. Automated tests exercised process termination and injected I/O failures on Windows and Ubuntu. Physical power-loss behavior remains unverified. [Transaction and recovery guarantees](transactions.md).

## Verification

All five post-merge CI jobs passed, including the packaging and quality job. Studio ran offscreen on all four platform and Python combinations. The matrix passed 308 core and 56 Studio test executions with zero failures or skips. [Post-merge CI run](https://github.com/aadarsha-hiremath/minidb-studio/actions/runs/45136817567).

| Runner | Core | Studio (offscreen) | PySide6 Essentials and Shiboken |
| --- | ---: | ---: | --- |
| Windows, Python 3.11 | 77 passed | 14 passed | 6.12.0 |
| Windows, Python 3.14 | 77 passed | 14 passed | 6.12.0 |
| Ubuntu, Python 3.11 | 77 passed | 14 passed | 6.11.3 |
| Ubuntu, Python 3.14 | 77 passed | 14 passed | 6.12.0 |

Separate local Windows validation also included Python 3.15.

The earlier intermittent Ubuntu Python 3.14 Studio migration timeout did not recur in the successful PR and post-merge CI runs. Its original cause remains undetermined. [PR CI run](https://github.com/aadarsha-hiremath/minidb-studio/actions/runs/45131949504).

The owner passed all five manual native Windows GUI checks, covering Create and Open dialogs, cancellation, migration and export file pickers, and restart. Native Linux desktop dialogs and macOS remain unverified.
