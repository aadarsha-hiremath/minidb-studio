# Native Studio measurements

Windows 11 build 26300, Intel Core i7-14650HX, CPython 3.15.2, PySide6 6.12.0, Qt Windows platform plugin, Fusion widget style, 150% display scaling. One instrumented run at each size. Numbers below are observations from this machine; the OS cache was enabled and other verification processes were running.

| Operation | 3,000 observations | 12,000 observations |
| --- | ---: | ---: |
| Fresh child process to first painted empty window | 655.54 ms | 714.50 ms |
| Construct and paint empty workspace after Qt initialization | 259.07 ms | 262.16 ms |
| Open and validate | 158.14 ms | 539.25 ms |
| Reopen and validate | 144.65 ms | 522.69 ms |
| First 128 records through grid paint | 27.45 ms | 28.82 ms |
| Indexed SELECT, sort and display 24 rows | 78.21 ms | 230.50 ms |
| EXPLAIN and show plan | 22.36 ms | 11.94 ms |
| Export all 24 result rows as CSV | 12.34 ms | 14.60 ms |
| 100 scrollbar changes and event processing | 971.19 ms | 889.68 ms |
| Repaint in light theme | 38.13 ms | 27.08 ms |
| Three workspace resizes, including 720 × 480 | 43.24 ms | 34.71 ms |
| Build second index with normal syncs | 473.97 ms | 2,609.89 ms |
| Largest observed event-loop heartbeat gap during build | 58.12 ms | 18.00 ms |
| Explicit integrity check | 296.62 ms | 1,065.09 ms |
| Open, recover committed WAL, validate and display outcome | 308.56 ms | 1,395.90 ms |
| Process working set after recovery query | 99.82 MiB | 97.93 MiB |

Each run seeds a fresh disposable v3 database with an indexed four-column observation table, an empty table, and 24 rows of multilingual text across twelve columns. Seeding is outside the timed operations. Each SQL result is bounded to 2,000 rows; the measured query explicitly limits output to 24. The table view reads 128 rows per request. Timings include worker dispatch, result delivery and GUI event processing. The 10 ms heartbeat fired 26 and 260 times respectively during index creation. Scrolling measures wall time for 100 updates in one loaded window. Memory is a point-in-time Windows process working set, not a peak or a leak test.

Recovery measurement terminates a separate writer immediately after its marker sync. The Studio worker reopens the original file, reports actual replay, and retrieves the committed row. No durability settings are changed. A fresh process for the startup measurement does not imply a cold filesystem cache.

From a GUI-enabled checkout in a desktop session:

```powershell
.\.venv\Scripts\python.exe benchmarks/studio.py --rows 3000
.\.venv\Scripts\python.exe benchmarks/studio.py --rows 12000
```

Use `--capture DIRECTORY` for actual widget screenshots from the disposable workload. [Engine transaction benchmark](transaction-benchmarks.md).
