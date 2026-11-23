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

Install the engine:

    python -m pip install .

Install the optional desktop Studio:

    python -m pip install ".[gui]"

Launch Studio using:

    minidb-gui

## Distribution

This repository distributes source code only.

No standalone Windows executable, installer, or prebuilt
native application is included in this release.

MiniDB's own source code is [MIT licensed](../LICENSE). Installing the GUI extra obtains PySide6 Essentials and Shiboken separately. The upstream community distribution uses LGPLv3/GPLv3 terms, and Qt also offers commercial licensing. The source archives include reference LGPLv3 and GPLv3 texts. [Qt licensing](https://doc.qt.io/qt-6/licensing.html) · [Qt for Python licensing](https://doc.qt.io/qtforpython-6/index.html).

## Limitations

MiniDB is intended for educational and experimental use.
It supports single-owner local database access and a
limited SQL feature set.

SQL is limited to single-table operations. Joins, aggregation, MVCC, and simultaneous readers are outside this version.

WAL durability depends on local filesystem and device sync behavior. Windows process termination and injected I/O errors were tested. Physical power-loss behavior remains unverified. [Transaction and recovery guarantees](transactions.md).

Local Windows checks used Python 3.11, 3.14 and 3.15. Hosted Ubuntu core checks passed. Studio verification on Ubuntu is pending. On Linux with Python 3.11 the GUI extra excludes PySide6 Essentials 6.12.0 because its bindings can abort the interpreter during Qt object cleanup. [Upstream Qt issue](https://qt-project.atlassian.net/browse/PYSIDE-3509) · [Installation and tests](../README.md) · [Public CI results](https://github.com/aadarsha-hiremath/minidb-studio/actions).
