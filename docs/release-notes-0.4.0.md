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

MiniDB's own source code is MIT licensed.
The optional GUI uses separately licensed PySide6/Qt
dependencies.

## Limitations

MiniDB is intended for educational and experimental use.
It supports single-owner local database access and a
limited SQL feature set.

SQL joins, aggregation, MVCC, and simultaneous readers
are not supported in this version.

See README.md for architecture, examples, and testing.
