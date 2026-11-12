"""Native database workspace. Widgets consume detached replies on the GUI thread."""

from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFont, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from minidb import DatabaseError, __version__
from minidb.sql import Delete, DropIndex, Update, parse

from . import style
from .dialogs import Preferences
from .service import write_new
from .settings import Settings
from .widgets import Editor, Grid
from .worker import SessionThread, Worker


def label(text, role=""):
    w = QLabel(text)
    w.setTextFormat(Qt.TextFormat.PlainText)
    w.setObjectName(role)
    return w


def box(horizontal=False, name="", margin=8):
    w = QWidget()
    w.setObjectName(name)
    layout = (QHBoxLayout if horizontal else QVBoxLayout)(w)
    layout.setContentsMargins(margin, margin, margin, margin)
    layout.setSpacing(6)
    return w, layout


def button(text, callback, tip="", primary=False):
    b = QPushButton(text)
    b.setAccessibleName(text)
    b.setToolTip(tip or text)
    if primary:
        b.setObjectName("primary")
    b.clicked.connect(lambda: callback())
    return b


class Window(QMainWindow):
    request = Signal(str, object)

    def __init__(self, prefs=None):
        super().__init__()
        self.settings = prefs or Settings()
        self.snap = {"path": "", "state": "disconnected", "tables": {}, "indexes": []}
        self.busy = False
        self.closing = False
        self.exit_confirmed = False
        self.stopped = False
        self.current_table = ""
        self.has_more = False
        self.notice_text = ""
        self.scope = ""
        self.setWindowTitle("MiniDB Studio")
        self.resize(*self.settings.values["window"])
        self.setMinimumSize(720, 460)
        self.worker = Worker()
        self.thread = SessionThread(self.worker, self)
        self.worker.moveToThread(self.thread)
        self.request.connect(self.thread.enqueue)
        self.worker.completed.connect(self.finished)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread_stopped)
        self.thread.start()
        self.build()
        self.apply_settings()
        self.populate()
        self.controls()
        QApplication.styleHints().colorSchemeChanged.connect(self.apply_settings)

    def build(self):
        central, layout = box(name="canvas", margin=0)
        layout.setSpacing(0)
        self.setCentralWidget(central)
        header = QFrame()
        header.setObjectName("bar")
        bar = QHBoxLayout(header)
        bar.setContentsMargins(14, 6, 12, 6)
        self.logo = QLabel()
        bar.addWidget(self.logo)
        bar.addWidget(label("MiniDB", "brand"))
        bar.addWidget(label("Studio", "muted"))
        bar.addSpacing(12)
        self.filename = label("No database", "muted")
        bar.addWidget(self.filename, 1)
        for title, cb in [
            ("Create", lambda: self.pick(True)),
            ("Open", self.pick),
            ("Settings", self.preferences),
        ]:
            bar.addWidget(button(title, cb))
        layout.addWidget(header)
        self.panels = QSplitter()
        self.panels.setChildrenCollapsible(False)
        layout.addWidget(self.panels, 1)
        self.rail, rail = box(name="rail")
        self.rail.setMinimumWidth(130)
        rail.addWidget(label("DATABASE EXPLORER", "section"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter explorer")
        self.filter.setAccessibleName("Filter database explorer")
        self.filter.textChanged.connect(self.filter_tree)
        rail.addWidget(self.filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(13)
        self.tree.setAccessibleName("Database explorer")
        self.tree.itemActivated.connect(self.navigate)
        self.tree.itemClicked.connect(self.navigate)
        rail.addWidget(self.tree, 1)
        self.panels.addWidget(self.rail)
        self.stack = QStackedWidget()
        self.panels.addWidget(self.stack)
        welcome, hero = box(margin=32)
        hero.addStretch()
        self.hero_logo = QLabel()
        hero.addWidget(self.hero_logo)
        hero.addSpacing(8)
        hero.addWidget(label("Open a database", "title"))
        detail = label(
            "Create a MiniDB .mdb file or open an existing one on this computer.",
            "muted",
        )
        detail.setWordWrap(True)
        hero.addWidget(detail)
        actions, ac = box(True, margin=0)
        ac.addWidget(button("Create database", lambda: self.pick(True), primary=True))
        ac.addWidget(button("Open file…", self.pick))
        ac.addStretch()
        hero.addSpacing(6)
        hero.addWidget(actions)
        hero.addStretch()
        self.stack.addWidget(welcome)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        self.stack.addWidget(self.tabs)
        self.build_sql()
        data, d = box()
        top, t = box(True, margin=0)
        self.table_title = label("Select a table", "section")
        t.addWidget(self.table_title, 1)
        t.addWidget(button("Refresh", self.refresh))
        self.next = button("Next 128", lambda: self.browse(True))
        t.addWidget(self.next)
        d.addWidget(top)
        self.data = Grid("Table data", self.settings)
        d.addWidget(self.data)
        self.page_info = label("Choose a table in the explorer.", "muted")
        d.addWidget(self.page_info)
        self.tabs.addTab(data, "Table data")
        self.schema = Grid("Table schema", self.settings)
        self.tabs.addTab(self.schema, "Schema")
        idx, ix = box()
        row, r = box(True, margin=0)
        r.addWidget(label("PERSISTENT INDEXES", "section"), 1)
        self.create_idx = button("Create index", self.index_dialog)
        self.drop_idx = button("Drop index", self.drop_index)
        r.addWidget(self.create_idx)
        r.addWidget(self.drop_idx)
        ix.addWidget(row)
        self.index_grid = Grid("Index definitions", self.settings)
        ix.addWidget(self.index_grid)
        self.tabs.addTab(idx, "Indexes")
        self.plan = QTextBrowser()
        self.plan.setAccessibleName("Query plan")
        self.tabs.addTab(self.plan, "Query plan")
        health, h = box(margin=16)
        h.addWidget(label("Database health", "title"))
        self.health = QTextBrowser()
        self.health.setAccessibleName("Database health details")
        h.addWidget(self.health, 1)
        row, r = box(True, margin=0)
        self.verify = button("Check integrity", lambda: self.submit("check"))
        self.migrate_btn = button("Migrate to v3…", self.migrate)
        r.addWidget(self.verify)
        r.addWidget(self.migrate_btn)
        r.addStretch()
        h.addWidget(row)
        self.tabs.addTab(health, "Health")
        self.side, si = box(name="rail")
        si.addWidget(label("INSPECTOR", "section"))
        self.inspect_text = QTextBrowser()
        self.inspect_text.setAccessibleName("Selected item details")
        si.addWidget(self.inspect_text)
        self.panels.addWidget(self.side)
        self.panels.setStretchFactor(1, 1)
        self.panels.setSizes(self.settings.values["panels"])
        self.panels.splitterMoved.connect(self.remember_splits)
        self.sql_split.splitterMoved.connect(self.remember_splits)
        self.side.hide()
        for g in self.grids():
            g.inspected.connect(self.inspect)
        self.tabs.currentChanged.connect(self.tab_changed)
        self.notice = label("", "error")
        self.notice.setWordWrap(True)
        self.notice.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.notice.setContentsMargins(10, 4, 10, 4)
        self.notice.hide()
        layout.addWidget(self.notice)
        status = QStatusBar()
        self.setStatusBar(status)
        self.connection = label("Disconnected", "accent")
        self.transaction = label("", "muted")
        self.last = label("", "muted")
        status.addWidget(self.connection)
        status.addWidget(self.transaction)
        status.addPermanentWidget(self.last, 1)
        self.menus()

    def grids(self):
        return (self.results, self.data, self.schema, self.index_grid)

    def build_sql(self):
        page, p = box(margin=0)
        self.sql_split = QSplitter(Qt.Orientation.Vertical)
        p.addWidget(self.sql_split)
        pane, e = box()
        bar, b = box(True, "tools", 4)
        self.run_btn = button(
            "Run", self.run_sql, "Run the editor as one statement (Ctrl+Enter)", True
        )
        self.sel_btn = button("Run selection", lambda: self.run_sql(True))
        self.explain = button("Explain", lambda: self.run_sql(explain=True))
        for item in (
            self.run_btn,
            self.sel_btn,
            self.explain,
            button("Clear", self.clear_editor),
        ):
            b.addWidget(item)
        b.addStretch()
        e.addWidget(bar)
        self.editor = Editor()
        self.editor.selectionChanged.connect(self.controls)
        e.addWidget(self.editor, 1)
        tx, t = box(True, margin=0)
        self.tx_buttons = {}
        for op in ("Begin", "Commit", "Rollback"):
            self.tx_buttons[op] = button(op, lambda op=op: self.transaction_action(op))
            t.addWidget(self.tx_buttons[op])
        t.addStretch()
        e.addWidget(tx)
        self.sql_split.addWidget(pane)
        pane, r = box()
        bar, b = box(True, margin=0)
        b.addWidget(label("QUERY RESULTS", "section"), 1)
        self.export_btn = button(
            "Export…",
            self.export_dialog,
            "Export all rows from the last successful result",
        )
        b.addWidget(self.export_btn)
        r.addWidget(bar)
        self.results = Grid("Query results", self.settings)
        r.addWidget(self.results, 1)
        self.result_info = label("Run a statement to see its result.", "muted")
        r.addWidget(self.result_info)
        self.sql_split.addWidget(pane)
        self.sql_split.setSizes(self.settings.values["sql_split"])
        self.tabs.addTab(page, "SQL editor")

    def menus(self):
        def action(menu, title, cb, key=None):
            a = QAction(title, self)
            if key:
                a.setShortcut(QKeySequence(key))
            a.triggered.connect(lambda: cb())
            menu.addAction(a)
            return a

        file = self.menuBar().addMenu("&File")
        action(file, "Create database…", lambda: self.pick(True), "Ctrl+N")
        action(file, "Open database…", self.pick, "Ctrl+O")
        self.recents = file.addMenu("Open recent")
        action(file, "Close database", self.disconnect)
        action(file, "Save SQL as…", self.save_sql, "Ctrl+S")
        action(file, "Exit", self.close)
        edit = self.menuBar().addMenu("&Workspace")
        action(edit, "Run SQL", self.run_sql, "Ctrl+Return")
        action(edit, "Find…", self.find, "Ctrl+F")
        action(edit, "Refresh", self.refresh, "F5")
        action(edit, "Toggle explorer", self.toggle_rail)
        action(edit, "Toggle inspector", self.toggle_inspector)
        action(edit, "Settings…", self.preferences)
        help_menu = self.menuBar().addMenu("&Help")
        action(
            help_menu,
            "About MiniDB Studio",
            lambda: QMessageBox.about(
                self,
                "About MiniDB Studio",
                f"MiniDB Studio {__version__}\nNative local database workspace\nMIT license · PySide6 / Qt Widgets",
            ),
        )

    def apply_settings(self, *_):
        pref = self.settings.values
        accent, _, literal = style.apply(self, pref)
        self.logo.setPixmap(style.mark(30, accent))
        self.hero_logo.setPixmap(style.mark(62, accent))
        self.setWindowIcon(QIcon(style.mark(48, accent)))
        font = QFont(pref["editor_font"]) if pref["editor_font"] else style.mono()
        font.setPointSize(pref["font_size"] + 1)
        self.editor.setFont(font)
        self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
            if pref["wrap"]
            else QPlainTextEdit.LineWrapMode.NoWrap
        )
        self.editor.position_gutter()
        self.editor.highlighter.accent = accent
        self.editor.highlighter.literal = literal
        self.editor.highlighter.rehighlight()
        for grid in self.grids():
            grid.dataset.metadata_font.setPointSize(pref["font_size"])
            grid.verticalHeader().setDefaultSectionSize(
                max(style.HEIGHT[pref["density"]], grid.fontMetrics().height() + 4)
            )
        self.rail.setVisible(pref["sidebar"])

    def preferences(self):
        dlg = Preferences(self.settings.values, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.reset_requested:
            self.settings.reset()
            self.resize(*self.settings.values["window"])
            self.panels.setSizes(self.settings.values["panels"])
            self.sql_split.setSizes(self.settings.values["sql_split"])
            for g in self.grids():
                g.replace(g.scope, g.dataset.columns, g.dataset.rows)
            self.populate()
        else:
            self.settings.values.update(dlg.values())
        self.apply_settings()
        self.persist()

    def remember_splits(self, *_):
        pref = self.settings.values
        if (
            self.stack.currentIndex() == 1
            and self.tabs.currentIndex() == 0
            and all(self.sql_split.sizes())
        ):
            pref["sql_split"] = self.sql_split.sizes()
        sizes = self.panels.sizes()
        if self.side.isVisible() and self.rail.isVisible():
            pref["panels"] = sizes
        elif self.rail.isVisible() and sizes[0]:
            pref["panels"][0] = sizes[0]

    def persist(self):
        self.settings.values["window"] = [self.width(), self.height()]
        self.remember_splits()
        try:
            self.settings.save()
        except OSError as exc:
            self.error(f"Could not save settings: {exc}")

    def toggle_rail(self):
        self.settings.values["sidebar"] = self.rail.isHidden()
        self.rail.setVisible(self.settings.values["sidebar"])

    def toggle_inspector(self):
        self.settings.values["inspector"] = not self.settings.values["inspector"]
        self.side.setVisible(
            self.settings.values["inspector"] and bool(self.inspect_text.toPlainText())
        )

    def inspect(self, text):
        self.inspect_text.setPlainText(text)
        self.side.setVisible(bool(text) and self.settings.values["inspector"])
        if self.side.isVisible() and self.panels.sizes()[2] < 80:
            self.panels.setSizes(self.settings.values["panels"])

    def tab_changed(self, *_):
        self.inspect("")
        self.controls()

    def error(self, text):
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def controls(self, *_):
        if not hasattr(self, "notice"):
            return
        state = self.snap["state"]
        live = bool(self.snap["path"]) and state != "reopen required" and not self.busy
        can_run = live and state != "rollback-only"
        self.run_btn.setEnabled(live)
        self.sel_btn.setEnabled(live and self.editor.textCursor().hasSelection())
        self.explain.setEnabled(can_run)
        for op, b in self.tx_buttons.items():
            b.setEnabled(
                live
                and self.snap.get("format") == 3
                and state
                in {
                    "Begin": ("idle",),
                    "Commit": ("active",),
                    "Rollback": ("active", "rollback-only"),
                }[op]
            )
        self.verify.setEnabled(live and state == "idle")
        self.migrate_btn.setVisible(self.snap.get("format") == 1)
        self.create_idx.setEnabled(
            can_run and self.snap.get("format") == 3 and bool(self.snap["tables"])
        )
        self.drop_idx.setEnabled(can_run and bool(self.snap["indexes"]))
        self.next.setEnabled(can_run and self.has_more)
        self.export_btn.setEnabled(not self.busy and bool(self.results.dataset.columns))
        self.connection.setText(
            "Connected"
            if live or self.snap["path"] and state != "reopen required"
            else ("Recovery required" if self.snap["path"] else "Disconnected")
        )
        self.transaction.setText(
            "   Transaction · " + state if self.snap["path"] else ""
        )
        self.filename.setText(Path(self.snap["path"]).name or "No database")
        self.filename.setToolTip(self.snap["path"])
        if self.snap["path"]:
            self.filename.setText(
                f"{Path(self.snap['path']).name}   ·   {self.connection.text()}"
            )

    def submit(self, name, *args):
        if self.exit_confirmed and name != "close":
            return False
        if self.busy or self.stopped or self.thread.isFinished():
            return False
        self.busy = True
        self.error("")
        self.last.setText(
            {
                "open": "Opening and validating database…",
                "check": "Checking all pages and indexes…",
                "sql": "Executing statement…",
                "migrate": "Migrating and verifying…",
                "close": "Closing database…",
                "export": "Exporting result…",
                "browse": "Reading table…",
                "refresh": "Refreshing catalog…",
            }[name]
        )
        self.controls()
        self.request.emit(name, args)
        return True

    def confirm(self, title, text):
        return (
            QMessageBox.question(
                self,
                title,
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            == QMessageBox.StandardButton.Yes
        )

    def discard_ok(self):
        return self.snap["state"] not in ("active", "rollback-only") or self.confirm(
            "Active transaction",
            "Discard all changes in the active transaction? Cancel to return and commit or roll back.",
        )

    def pick(self, create=False):
        if self.busy:
            return
        picker = QFileDialog.getSaveFileName if create else QFileDialog.getOpenFileName
        path, _ = picker(
            self,
            "Create database" if create else "Open database",
            "",
            "MiniDB databases (*.mdb);;All files (*)",
        )
        if path:
            self.open_path(path, create)

    def open_path(self, path, create=False):
        if not self.busy and self.discard_ok():
            self.submit("open", path, create, True)

    def disconnect(self):
        if self.snap["path"] and not self.busy and self.discard_ok():
            self.submit("close", True)

    def migrate(self):
        if self.busy:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Migrate to a new v3 file · original is preserved",
            "",
            "MiniDB (*.mdb)",
        )
        if path:
            self.submit("migrate", path)

    def transaction_action(self, op):
        if op == "Rollback" and not self.confirm(
            "Roll back transaction", "Discard all changes since BEGIN?"
        ):
            return
        self.submit("sql", op.upper())

    def run_sql(self, selected=False, explain=False):
        if (
            self.busy
            or not self.snap["path"]
            or self.snap["state"] == "reopen required"
        ):
            return
        if self.tabs.currentIndex() != 0:
            self.tabs.setCurrentIndex(0)
        source = (
            self.editor.textCursor().selectedText().replace("\u2029", "\n")
            if selected
            else self.editor.toPlainText()
        )
        if not source.strip():
            return
        if explain:
            source = "EXPLAIN " + source
        try:
            ast, _ = parse(source)
        except DatabaseError:
            ast = None  # Let the engine apply its rollback-only semantics.
        if (
            isinstance(ast, (Delete, DropIndex, Update))
            or getattr(ast, "command", "") == "ROLLBACK"
        ) and not self.confirm(
            "Confirm statement",
            "Execute this statement on the active database?\n\n" + source[:300],
        ):
            return
        self.submit("sql", source)

    def save_sql(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save SQL to a new file", "", "SQL (*.sql)"
        )
        if not path:
            return False
        try:
            write_new(path, lambda f: f.write(self.editor.toPlainText()))
        except OSError as exc:
            self.error(str(exc))
            return False
        self.editor.document().setModified(False)
        return True

    def clear_editor(self):
        if not self.editor.toPlainText() or self.confirm(
            "Clear editor", "Clear the SQL text?"
        ):
            self.editor.clear()

    def export_dialog(self):
        path, kind = QFileDialog.getSaveFileName(
            self,
            "Export complete SQL result to a new file",
            "",
            "CSV (*.csv);;JSON (*.json)",
        )
        if path:
            self.submit(
                "export",
                path,
                self.results.dataset.columns,
                self.results.dataset.rows,
                kind.split()[0],
            )

    def index_dialog(self):
        tables = self.snap["tables"]
        table, ok = QInputDialog.getItem(
            self, "Create index", "Table", list(tables), editable=False
        )
        if not ok:
            return
        col, ok = QInputDialog.getItem(
            self,
            "Create index",
            "Column",
            [c for c, _ in tables[table]],
            editable=False,
        )
        if not ok:
            return
        name, ok = QInputDialog.getText(
            self, "Create index", "Index name", text=f"idx_{table}_{col}"
        )
        if ok:
            from minidb.schema import validate_name

            try:
                validate_name(name)
            except DatabaseError as exc:
                self.error(str(exc))
                return
            self.submit("sql", f"CREATE INDEX {name} ON {table}({col})")

    def drop_index(self):
        names = [i[0] for i in self.snap["indexes"]]
        name, ok = QInputDialog.getItem(
            self, "Drop index", "Index", names, editable=False
        )
        if ok and self.confirm("Drop index", f"Drop index {name}?"):
            self.submit("sql", f"DROP INDEX {name}")

    def browse(self, advance=False):
        if self.current_table:
            self.submit("browse", self.current_table, advance)

    def refresh(self):
        if not self.snap["path"] or self.busy:
            return
        if self.tabs.currentIndex() == 1 and self.current_table:
            self.browse()
        else:
            self.submit("refresh")

    def find(self):
        if self.stack.currentIndex() == 0:
            self.rail.show()
            self.filter.setFocus()
            return
        text, ok = QInputDialog.getText(self, "Find", "Search active view")
        if not ok or not text:
            return
        view = [
            self.editor,
            self.data,
            self.schema,
            self.index_grid,
            self.plan,
            self.health,
        ][self.tabs.currentIndex()]
        if isinstance(view, Grid):
            for row, items in enumerate(view.dataset.rows):
                for col, value in enumerate(items.values()):
                    if text.casefold() in str(value).casefold():
                        view.setCurrentIndex(view.dataset.index(row, col))
                        return
        else:
            if view.find(text):
                return
            cursor = view.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            view.setTextCursor(cursor)
            if view.find(text):
                return
        self.last.setText("No match in this view")

    def filter_tree(self, text):
        def visit(item):
            children = [visit(item.child(i)) for i in range(item.childCount())]
            visible = text.casefold() in item.text(0).casefold() or any(children)
            item.setHidden(not visible)
            if text and any(children):
                item.setExpanded(True)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def populate(self):
        expanded = set()

        def collect(node):
            if node.isExpanded():
                expanded.add(node.data(0, Qt.ItemDataRole.UserRole))
            for i in range(node.childCount()):
                collect(node.child(i))

        for i in range(self.tree.topLevelItemCount()):
            collect(self.tree.topLevelItem(i))
        self.tree.clear()

        def add(parent, title, kind, *args, expand=False):
            item = QTreeWidgetItem(parent, [title])
            key = (kind, *args)
            item.setData(0, Qt.ItemDataRole.UserRole, key)
            item.setExpanded(expand or key in expanded)
            item.setToolTip(0, title)
            return item

        path = self.snap["path"]
        if path:
            root = add(self.tree, Path(path).name, "root", expand=True)
            add(root, "SQL workspace", "sql")
            group = add(root, "Tables", "tables", expand=True)
            for table, cols in self.snap["tables"].items():
                n = add(group, table, "table", table)
                for c, k in cols:
                    add(n, f"{c}   ·   {k}", "column", table, c, k)
            idx = add(root, "Indexes", "indexes", expand=True)
            for name, table, col, root_page in self.snap["indexes"]:
                add(idx, name, "index", name, table, col, root_page)
            add(root, "Database health", "health")
        recent = self.settings.values["recent"]
        self.recents.clear()
        self.recents.setEnabled(bool(recent))
        group = add(self.tree, "Recent databases", "recent", expand=not path)
        for p in recent:
            item = add(group, Path(p).name, "file", p)
            item.setToolTip(0, p)
            self.recents.addAction(Path(p).name, lambda p=p: self.open_path(p))
        self.filter_tree(self.filter.text())

    def navigate(self, item, *_):
        if self.busy:
            return
        kind, *args = item.data(0, Qt.ItemDataRole.UserRole)
        if kind == "file":
            if args[0] != self.snap["path"]:
                self.open_path(args[0])
        elif kind in ("sql", "indexes", "index", "health"):
            self.tabs.setCurrentIndex(
                {"sql": 0, "indexes": 3, "index": 3, "health": 5}[kind]
            )
            if kind == "index":
                self.inspect(
                    f"{args[0]}\n\nTable   {args[1]}\nColumn   {args[2]}\nRoot page   {args[3]}"
                )
        elif kind in ("table", "column"):
            if self.current_table != args[0]:
                self.current_table = args[0]
                self.clear_data()
            self.schema_view()
            self.tabs.setCurrentIndex(1 if kind == "table" else 2)
            if kind == "table":
                self.browse()
            else:
                self.inspect(f"{args[1]}\n\nType   {args[2]}\nTable   {args[0]}")

    def schema_view(self):
        cols = self.snap["tables"].get(self.current_table, [])
        self.schema.replace(
            "schema", ["Column", "Type"], [{"Column": n, "Type": k} for n, k in cols]
        )

    def clear_data(self):
        self.has_more = False
        self.data.replace("", [], [])
        self.table_title.setText(self.current_table or "Select a table")
        self.page_info.setText(
            "Refresh to read this table."
            if self.current_table
            else "Choose a table in the explorer."
        )
        self.inspect("")

    @Slot(object)
    def finished(self, reply):
        self.busy = False
        old = self.snap
        self.snap = reply["snapshot"]
        name = reply["name"]
        payload = reply["data"]
        path = self.snap["path"]
        connection_changed = (
            old["path"] != path
            or name in ("open", "close", "check", "migrate")
            and not reply["error"]
        )
        if connection_changed:
            self.current_table = ""
            self.results.replace("", [], [])
            self.result_info.setText("Run a statement to see its result.")
            self.plan.setPlainText(
                "Run EXPLAIN on a SELECT statement to inspect its access path."
            )
            self.clear_data()
            if path:
                self.settings.values["recent"] = [
                    path,
                    *[p for p in self.settings.values["recent"] if p != path],
                ][:8]
                if name in ("open", "migrate"):
                    self.tabs.setCurrentIndex(5 if self.snap.get("format") == 1 else 0)
            self.persist()
        self.stack.setCurrentIndex(int(bool(path)))
        if self.current_table not in self.snap["tables"]:
            self.current_table = ""
        message = ""
        if reply["error"]:
            self.error(reply["error"])
            message = "Operation failed"
            if name == "sql":
                self.results.replace("", [], [])
                self.result_info.setText("Statement failed. See the error below.")
            if self.snap["state"] in ("rollback-only", "reopen required"):
                self.clear_data()
        elif name == "sql":
            message = payload["message"]
            self.results.replace("results", payload["columns"], payload["rows"])
            self.result_info.setText(f"{message}   ·   {reply['ms']:.1f} ms")
            if payload["changed"]:
                self.clear_data()
            if payload["plan"]:
                plan = payload["plan"]
                self.plan.setHtml(
                    f"<h2>{escape(plan)}</h2><p>"
                    + (
                        "The engine visits matching B+ tree entries and retrieves their heap records."
                        if "Index" in plan
                        else "The engine scans rows in physical record order."
                    )
                    + "</p><h3>Inspected statement</h3><pre>"
                    + escape(payload["plan_sql"])
                    + "</pre>"
                )
                self.tabs.setCurrentIndex(4)
        elif name == "browse":
            self.data.replace(
                "table/" + payload["table"], payload["columns"], payload["rows"]
            )
            self.has_more = payload["more"]
            n = len(payload["rows"])
            self.table_title.setText(payload["table"])
            self.page_info.setText(
                f"Rows {payload['offset'] + 1}–{payload['offset'] + n}"
                if n
                else (
                    "End of table"
                    if payload["offset"]
                    else "This table has no records."
                )
            )
            message = "Table loaded"
        else:
            message = payload or ("Closed database" if name == "close" else "Complete")
        self.last.setText(f"{message}   ·   {reply['ms']:.1f} ms")
        if path:
            health = [
                ("Database", Path(path).name),
                ("Format", self.snap.get("format")),
                ("Current transaction", self.snap["state"]),
                ("Checkpoint generation", self.snap.get("checkpoint")),
                ("Database bytes", self.snap.get("database_bytes")),
                ("WAL bytes", self.snap.get("wal_bytes")),
                ("Committed WAL replay on last open", self.snap.get("recovered")),
                ("Last integrity check", self.snap.get("integrity")),
            ]
            self.health.setHtml(
                '<table cellspacing="8" cellpadding="3">'
                + "".join(
                    f"<tr><td>{escape(k)}</td><td><b>{escape(str(v)) if v is not None else 'Unavailable'}</b></td></tr>"
                    for k, v in health
                )
                + "</table>"
            )
            if self.snap.get("format") == 1:
                self.health.append(
                    "Legacy v1 opens read-only. Migrate to a new v3 destination to edit."
                )
        if (
            connection_changed
            or old["tables"] != self.snap["tables"]
            or old["indexes"] != self.snap["indexes"]
            or name == "refresh"
        ):
            self.populate()
            self.schema_view()
            keys = ["Name", "Table", "Column", "Root page"]
            self.index_grid.replace(
                "indexes", keys, [dict(zip(keys, i)) for i in self.snap["indexes"]]
            )
        self.controls()
        if self.closing:
            if self.exit_confirmed and name == "close" and not reply["error"]:
                self.thread.stop()
            elif self.exit_confirmed and name == "close":
                self.closing = False
                self.exit_confirmed = False
                self.editor.setReadOnly(False)
            else:
                self.closing = False
                QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if self.stopped:
            event.accept()
            return
        event.ignore()
        if self.closing:
            return
        if self.busy:
            self.closing = True
            self.last.setText("Waiting for current operation before closing…")
            return
        if self.editor.document().isModified():
            choice = QMessageBox.question(
                self,
                "Unsaved SQL",
                "Save the SQL editor text before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if (
                choice == QMessageBox.StandardButton.Cancel
                or choice == QMessageBox.StandardButton.Save
                and not self.save_sql()
            ):
                return
        if not self.discard_ok():
            return
        self.persist()
        self.closing = True
        self.exit_confirmed = True
        self.editor.setReadOnly(True)
        self.submit("close", True)

    @Slot()
    def thread_stopped(self):
        self.stopped = True
        self.close()
