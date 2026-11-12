# MiniDB Studio 0.4.0

Studio is a local Qt Widgets client for MiniDB. The first window is empty. Create or open a MiniDB file, or pick one from File → Open recent. The filename in the header has a full-path tooltip. Core-only installations can continue to use the CLI and the Python API.

## Using the workspace

| Task | Action |
| --- | --- |
| Browse | Select a table in the explorer. Table data reads up to 128 records in physical RID order. Next advances the existing iterator. Refresh starts at the beginning. Selecting a column opens its schema. |
| Inspect | Select a record, schema field or index. The side inspector displays the selection. Drag the dividers or use the Workspace menu to hide either side panel. |
| Write SQL | Enter one complete statement, including multiline SQL. Ctrl+Enter runs the whole editor. Run selection submits only selected text as one statement. The engine rejects extra statements before running any. Ctrl+S saves the draft to a new SQL file. |
| Manage data | Use CREATE TABLE, INSERT, UPDATE or DELETE in the editor. The grid is read-only. Studio asks before UPDATE, DELETE, DROP INDEX and ROLLBACK. The engine supports integer, text, and Boolean fields. |
| Work in a transaction | Begin, Commit, and Rollback operate on the editor's handle. The status bar shows current transaction state. The last-operation text records events such as a successful commit. A failed SQL statement in an active transaction can require rollback. |
| Manage indexes | Open Indexes to inspect the name, table, column and root page. Create index selects a table and column. Drop index asks which index to remove and requests confirmation. F5 refreshes the active view. |
| Explain | With one SELECT in the editor, choose Explain. The Query plan tab shows the engine's access path and the inspected statement. EXPLAIN SELECT also works directly. |
| Copy and export | Ctrl+C copies the current cell. The grid context menu can copy selected rows as tab-separated text. Right-click the header to change column visibility. Export writes the entire successful SQL result, including hidden columns, to a new UTF-8 CSV or JSON file. |
| Check health | Health shows format, transaction, checkpoint, main and WAL byte sizes, actual replay on open, and the last validation. Check integrity closes the idle handle and opens and validates it again. A failure reacquiring the lock is surfaced. |
| Migrate v1 | Opening v1 selects Health. Choose Migrate to v3 and a new destination. The engine validates and copies the source, then Studio opens and verifies the destination. The source remains the backup. |
| Find and navigate | Ctrl+N creates, Ctrl+O opens. Ctrl+F searches the current editor, grid window or text view. The explorer has its own name filter. Use the keyboard to navigate tree items and rows. Escape cancels standard dialogs. |

A SELECT may return up to **2,000 rows** in Studio. A larger result fails with a suggestion to narrow the filter or add LIMIT. A small LIMIT can still require scanning every match to preserve ordering. Parsing and statement failure semantics are shared with the engine. An oversized SELECT in an explicit transaction makes it rollback-only. UPDATE and DELETE use the engine's original target materialization and transaction dirty-page limits. Table browsing uses sequential windows. There is no OFFSET pagination, editable spreadsheet grid, multiple concurrent connections, or arbitrary script runner.

## Ownership, shutdown and recovery

The window sends a named command to a queue and admits one outstanding operation. A QThread runs a persistent Python loop and owns the engine session for its entire lifetime. This matters because the pager checks the identity of the Python thread object. SQL controls, catalog inspection and table reads share this session. The worker sends detached values back by Qt signal. Only the GUI thread touches widgets and table models.

Open either installs a successfully validated candidate or reports failure. Switching away from an active transaction requires explicit confirmation to discard it. A failed commit reports its engine outcome. Studio blocks further SQL on a failed handle. Close and reopen the original database, inspect the recovered state, and decide whether the operation needs to be repeated. Studio does not retry it. The replay indicator comes from successful engine recovery. The permanent 4 KiB WAL also exists on a clean database.

Closing the window during work waits for the current command. The user can cancel closing, save or discard edited SQL, or return to an active transaction. Running commits, index builds and validation finish on their owner thread. Closing a file leaves SQL text in the editor for the next connection. Exports and SQL saves require new filenames to protect pre-existing files. Keep closed, validated database/WAL pairs together for backup. [Storage guarantees and failure model](transactions.md).

## Preferences and diagnostics

Settings are kept in `studio.json` under Qt's per-user AppConfigLocation for MiniDB Studio. QSaveFile atomically replaces the settings file. Invalid individual preferences fall back to defaults. Dark is the initial theme. Light and System are available, along with three row densities, font size, editor family, wrap and reduced motion. Pane sizes, sidebar/inspector preference, window size, column widths/visibility, and up to eight recent paths survive restart. Reset preferences and history from Settings. SQL drafts and result values are excluded. A size-limited `studio.log` under AppLocalDataLocation records operation names and exception classes.

## Screenshots from the Windows application

[Empty workspace](../assets/studio/first-launch.png) · [SQL and results](../assets/studio/sql-workspace.png) · [Table and inspector](../assets/studio/table-grid.png) · [Query plan](../assets/studio/query-plan.png) · [Database health](../assets/studio/health.png) · [Light theme and indexes](../assets/studio/light-indexes.png).

The screenshot harness explicitly creates a temporary field-observation database and runs real queries. The application never creates this dataset on first launch.

## First-time verification

1. Launch Studio from the installed virtual environment. Confirm the empty workspace, then create a fresh `studio-check.mdb`.
2. Run each of the following as a separate statement. Use the buttons or SQL commands for the transaction boundaries.

   ```sql
   CREATE TABLE notes (id INT, title TEXT)
   BEGIN
   INSERT INTO notes VALUES (1, 'First observation')
   COMMIT
   BEGIN
   UPDATE notes SET title='Draft' WHERE id=1
   ROLLBACK
   SELECT * FROM notes
   ```

3. Verify the title is `First observation`. Open the table in the explorer, select its row, and inspect its RID. Create an index on `id`, then Explain `SELECT title FROM notes WHERE id=1`. The plan should name that index.
4. Rerun the SELECT, copy the cell, and export the result to a new CSV or JSON file. Run Check integrity. Close the database before opening it from the CLI.
5. Change theme, density and editor wrap. Drag both splitters, resize a column, and close the whole application. Reopen it and use the recent-file entry. Verify the preferences, then reset them and restart once more.
6. Start another transaction and try closing the file. Cancel and verify that it remains active. Roll back explicitly. Edit the SQL draft and close the application to exercise the save/discard prompt.
