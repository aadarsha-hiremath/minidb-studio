import csv
import io
import re

from PySide6.QtCore import QAbstractTableModel, QRect, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QSyntaxHighlighter
from PySide6.QtWidgets import QApplication, QMenu, QPlainTextEdit, QTableView, QWidget

from .style import mono

ROLE = Qt.ItemDataRole.DisplayRole
KEYWORDS = {
    "SELECT",
    "FROM",
    "WHERE",
    "AND",
    "ORDER",
    "BY",
    "ASC",
    "DESC",
    "LIMIT",
    "EXPLAIN",
    "INSERT",
    "INTO",
    "VALUES",
    "UPDATE",
    "SET",
    "DELETE",
    "CREATE",
    "TABLE",
    "INDEX",
    "ON",
    "DROP",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "INT",
    "TEXT",
    "BOOL",
    "TRUE",
    "FALSE",
}


class Highlight(QSyntaxHighlighter):
    def highlightBlock(self, text):
        # A quoted literal may cross a line and may contain doubled quotes.
        start = 0
        literal = self.previousBlockState() == 1
        for match in re.finditer(r"'|[A-Za-z_][A-Za-z_0-9]*|-?[0-9]+", text):
            token = match.group()
            if token == "'":
                if literal:
                    self.setFormat(start, match.end() - start, QColor(self.literal))
                    literal = False
                else:
                    start = match.start()
                    literal = True
            elif not literal and token.upper() in KEYWORDS:
                self.setFormat(match.start(), len(token), QColor(self.accent))
        if literal:
            self.setFormat(start, len(text) - start, QColor(self.literal))
        self.setCurrentBlockState(int(literal))


class Gutter(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(self.editor.palette().placeholderText().color())
        block = self.editor.firstVisibleBlock()
        while block.isValid():
            y = round(
                self.editor.blockBoundingGeometry(block)
                .translated(self.editor.contentOffset())
                .top()
            )
            if y > event.rect().bottom():
                break
            if block.isVisible():
                p.drawText(
                    QRect(0, y, self.width() - 7, self.editor.fontMetrics().height()),
                    Qt.AlignmentFlag.AlignRight,
                    str(block.blockNumber() + 1),
                )
            block = block.next()
        p.end()


class Editor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setFont(mono())
        self.setAccessibleName("SQL editor")
        self.setPlaceholderText("Write one MiniDB SQL statement…")
        self.gutter = Gutter(self)
        self.highlighter = Highlight(self.document())
        self.highlighter.accent = "#82ddbd"
        self.highlighter.literal = "#d5ab75"
        self.blockCountChanged.connect(self.position_gutter)
        self.updateRequest.connect(lambda *_: self.gutter.update())
        self.position_gutter()

    def position_gutter(self, *_):
        width = (
            self.fontMetrics().horizontalAdvance("9")
            * max(2, len(str(self.blockCount())))
            + 16
        )
        self.setViewportMargins(width, 0, 0, 0)
        self.gutter.setGeometry(1, 1, width, self.height() - 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_gutter()


class Model(QAbstractTableModel):
    def __init__(self, parent):
        super().__init__(parent)
        self.columns = []
        self.rows = []
        self.metadata_font = mono()

    def replace(self, columns, rows):
        self.beginResetModel()
        self.columns = list(columns)
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=ROLE):
        if role == ROLE:
            return (
                self.columns[section]
                if orientation == Qt.Orientation.Horizontal
                else str(section + 1)
            )
        return None

    def data(self, index, role=ROLE):
        if (
            index.isValid()
            and role == Qt.ItemDataRole.FontRole
            and self.columns[index.column()] in ("Record ID", "Root page", "Type")
        ):
            return self.metadata_font
        if index.isValid() and role in (ROLE, Qt.ItemDataRole.ToolTipRole):
            value = self.rows[index.row()][self.columns[index.column()]]
            return str(value).lower() if type(value) is bool else str(value)
        return None


class Grid(QTableView):
    inspected = Signal(str)

    def __init__(self, label, prefs):
        super().__init__()
        self.prefs = prefs
        self.scope = ""
        self.dataset = Model(self)
        self.setModel(self.dataset)
        self.setAccessibleName(label)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.verticalHeader().setDefaultSectionSize(29)
        self.horizontalHeader().setMinimumSectionSize(32)
        self.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.horizontalHeader().setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.horizontalHeader().customContextMenuRequested.connect(self.column_menu)
        self.horizontalHeader().sectionResized.connect(self.remember)
        self.selectionModel().currentChanged.connect(self.inspect)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.context)
        self.copy_action = QAction("Copy value", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.copy_action.triggered.connect(self.copy_value)
        self.addAction(self.copy_action)

    def replace(self, scope, columns, rows):
        self.scope = ""
        self.dataset.replace(columns, rows)
        saved = self.prefs.values["columns"].get(scope, {})
        for i, c in enumerate(columns):
            width, shown = saved.get(c, [150, True])
            self.setColumnHidden(i, not shown)
            self.setColumnWidth(i, width)
        if columns and all(self.isColumnHidden(i) for i in range(len(columns))):
            self.showColumn(0)
        self.scope = scope

    def remember(self, *_):
        if self.scope:
            self.prefs.values["columns"][self.scope] = {
                c: [
                    max(
                        32,
                        self.columnWidth(i)
                        or self.prefs.values["columns"]
                        .get(self.scope, {})
                        .get(c, [150])[0],
                    ),
                    not self.isColumnHidden(i),
                ]
                for i, c in enumerate(self.dataset.columns)
            }

    def column_menu(self, pos):
        menu = QMenu(self)
        for i, c in enumerate(self.dataset.columns):
            act = menu.addAction(c)
            act.setCheckable(True)
            act.setChecked(not self.isColumnHidden(i))
            act.setEnabled(
                self.isColumnHidden(i)
                or sum(
                    not self.isColumnHidden(j)
                    for j in range(self.dataset.columnCount())
                )
                > 1
            )
            act.triggered.connect(lambda checked, i=i: self.visibility(i, checked))
        menu.exec(self.horizontalHeader().mapToGlobal(pos))

    def visibility(self, i, visible):
        self.setColumnHidden(i, not visible)
        self.remember()

    def inspect(self, index, *_):
        if index.isValid():
            row = self.dataset.rows[index.row()]
            self.inspected.emit("\n\n".join(f"{k}\n{v}" for k, v in row.items()))

    def copy_value(self):
        index = self.currentIndex()
        if index.isValid():
            QApplication.clipboard().setText(self.dataset.data(index))

    def copy_rows(self):
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, delimiter="\t")
        for idx in sorted(self.selectionModel().selectedRows(), key=lambda i: i.row()):
            writer.writerow(
                [
                    self.dataset.rows[idx.row()][c]
                    for i, c in enumerate(self.dataset.columns)
                    if not self.isColumnHidden(i)
                ]
            )
        QApplication.clipboard().setText(buf.getvalue())

    def context(self, pos):
        if self.currentIndex().isValid():
            menu = QMenu(self)
            menu.addAction("Copy value", self.copy_value)
            menu.addAction("Copy selected rows", self.copy_rows)
            menu.exec(self.viewport().mapToGlobal(pos))
