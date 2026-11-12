# Transaction measurements

Windows 11 build 26300, Intel Core i7-14650HX, CPython 3.15.2. Each cell is the median of three independent fresh databases at that row count. The workload uses integer keys and zero-padded text labels. OS caches are enabled. All normal commit syncs run.

| Rows | Phase 2 inserts (ms) | Phase 3 implicit inserts (ms) | One explicit batch (ms) | Batch commit alone (ms) | Checkpoint within commit (ms) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 16 | 2.830 | 65.641 | 6.416 | 4.265 | 2.093 |
| 64 | 6.240 | 266.918 | 10.380 | 5.278 | 2.764 |
| 256 | 28.347 | 1,154.937 | 31.218 | 4.855 | 2.403 |

The first two columns time individual record inserts from the first call through database close. The Phase 2 baseline uses the actual merge revision `8d0fb05` with one sync on dirty close. Phase 3 completes six syncs per dirty transaction, including WAL retirement. The batch includes begin, insertion and commit. Its commit column includes logging and checkpoint. Checkpoint timing starts after durable marker sync and ends after completed-stamp sync, before WAL retirement.

| Rows | All-row UPDATE (ms) | Indexed key UPDATE (ms) | Indexed DELETE (ms) | Rollback (ms) | Clean open (ms) | Recovery open (ms) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 | 5.115 | 7.864 | 6.247 | 0.747 | 16.560 | 18.388 |
| 64 | 9.310 | 26.426 | 17.457 | 3.113 | 22.348 | 18.199 |
| 256 | 41.159 | 162.415 | 87.879 | 15.443 | 35.288 | 33.751 |

The first UPDATE changes unindexed text. After index creation, the second UPDATE changes every integer key to the same value. DELETE removes every matching record through that index. Rollback timing begins after staging a complete deletion and includes catalog and index reconstruction. Clean open includes full integrity validation. For recovery, a separate child inserts the requested row count in one indexed transaction and exits immediately after marker sync. Recovery time includes WAL inspection, prospective validation, redo, readback and retirement; child startup and post-recovery result checks are excluded. The reopened datasets have different page occupancy, so individual open times are not directly comparable.

| Rows | Main before batch checkpoint | Main after checkpoint | WAL at marker sync | WAL after checkpoint |
| ---: | ---: | ---: | ---: | ---: |
| 16 | 16,384 B | 20,480 B | 8,344 B | 4,096 B |
| 64 | 16,384 B | 20,480 B | 8,344 B | 4,096 B |
| 256 | 16,384 B | 24,576 B | 12,444 B | 4,096 B |

The schema already exists when these sizes are collected. Sixteen and 64 rows each dirty one heap page; 256 dirty two. Repeated edits to the same page add no further WAL frames within that transaction. Closing each row's transaction separately repeats the synchronization cost.

From an installed checkout:

```powershell
.\.venv\Scripts\python.exe benchmarks/transactions.py --sizes 16 64 256 --iterations 3
```

To reproduce the historical comparison, add `--phase2-source PATH` pointing to a separate checkout of the pinned Phase 2 merge. The script runs that writer in a separate Python process against temporary files.
