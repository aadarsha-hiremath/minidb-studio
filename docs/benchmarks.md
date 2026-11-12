# Phase 2 benchmark

Windows 11 build 26300, Intel Core i7-14650HX, CPython 3.15.2. Values are medians of three iterations per size. Each iteration creates two fresh temporary files with identical rows in seeded shuffled insertion order. One file has an integer index. Every timed query runs through the SQL parser, binder, planner and executor on an already open handle. The script checks that both access paths return the same rows.

| Rows | 24 equality queries, heap / index (ms) | 24 ranges, heap / index (ms) | Index build (ms) | File size, heap / index (bytes) |
| ---: | ---: | ---: | ---: | ---: |
| 250 | 8.814 / 3.231 | 9.467 / 6.532 | 22.123 | 16,384 / 32,768 |
| 1,000 | 36.296 / 3.407 | 36.525 / 6.406 | 121.647 | 36,864 / 77,824 |
| 3,000 | 227.404 / 7.344 | 269.153 / 13.922 | 753.472 | 86,016 / 167,936 |

| Rows | 50 inserts, heap / index (ms) | 50 key-changing updates by RID, heap / index (ms) | Validated reopen, heap / index (ms) |
| ---: | ---: | ---: | ---: |
| 250 | 4.616 / 11.598 | 3.714 / 16.890 | 5.007 / 11.543 |
| 1,000 | 11.935 / 18.152 | 3.846 / 17.450 | 5.901 / 33.783 |
| 3,000 | 64.412 / 77.613 | 7.955 / 33.789 | 7.808 / 188.954 |

Each equality probe matches one unique key. Each bounded range spans up to five integer keys. File sizes are measured after index creation and close, before the extra writes. Reopen includes lock acquisition, catalog loading and integrity validation. Index validation checks a heap RID for each leaf entry. Build, insert and update timings end before close and sync. Caches remain enabled and timing varies with system load.

On the 250-row workload, the time saved on 24 equality probes was about 5.6 ms while building the index alone took 22.1 ms. At 3,000 rows, 24 equality probes saved about 220 ms on an open handle. Reopening the indexed file cost about 181 ms extra. One-process-per-query CLI measurements include that cost. Broad ranges can also incur one heap lookup per match, and the heuristic planner does not estimate selectivity.

Reproduce from an installed checkout:

```powershell
.\.venv\Scripts\python.exe benchmarks/indexing.py --sizes 250 1000 3000 --iterations 3 --probes 24 --edits 50
```
