"""Studio's native palette, density and original stacked-page mark."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication

DARK = (
    "#14191d",
    "#1b2126",
    "#232b31",
    "#394349",
    "#e7ecee",
    "#a5b3b9",
    "#82ddbd",
    "#223e37",
    "#926635",
    "#ffafa3",
)
LIGHT = (
    "#f3f5f4",
    "#ffffff",
    "#e9eeeb",
    "#c4ceca",
    "#182822",
    "#52665e",
    "#146b55",
    "#ceeade",
    "#80551c",
    "#a72d29",
)
HEIGHT = {"Comfortable": 36, "Standard": 29, "Compact": 24}


def mono():
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


def mark(size, color):
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 48, size / 48)
    p.setPen(
        QPen(
            QColor(color),
            2.2,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    p.drawPolygon(
        QPolygonF([QPointF(4, 15), QPointF(24, 5), QPointF(44, 15), QPointF(24, 25)])
    )
    for y in (23, 31):
        p.drawPolyline(QPolygonF([QPointF(4, y), QPointF(24, y + 10), QPointF(44, y)]))
    p.end()
    return px


def apply(window, prefs):
    system = QApplication.styleHints().colorScheme()
    dark = prefs["theme"] == "Dark" or (
        prefs["theme"] == "System" and system == Qt.ColorScheme.Dark
    )
    bg, panel, raised, line, ink, muted, accent, selected, warn, error = (
        DARK if dark else LIGHT
    )
    font = QFont(QApplication.font())
    font.setPointSize(prefs["font_size"])
    window.setFont(font)
    pal = QPalette()
    for role, col in {
        "Window": bg,
        "WindowText": ink,
        "Base": bg,
        "AlternateBase": panel,
        "Text": ink,
        "Button": raised,
        "ButtonText": ink,
        "Highlight": selected,
        "HighlightedText": ink,
        "ToolTipBase": raised,
        "ToolTipText": ink,
        "PlaceholderText": muted,
        "Link": accent,
    }.items():
        pal.setColor(getattr(QPalette.ColorRole, role), QColor(col))
    QApplication.setPalette(pal)
    window.setStyleSheet(f"""
    QWidget {{color:{ink};}} QMainWindow,QWidget#canvas,QPlainTextEdit,QTableView,QTreeWidget,QTextBrowser {{background:{bg};}}
    QFrame#bar,QWidget#rail,QWidget#tools,QStatusBar,QTabBar::tab,QHeaderView::section,QWidget#box {{background:{panel};}}
    QFrame#bar,QWidget#tools,QHeaderView::section {{border:0;border-bottom:1px solid {line};}}
    QLabel#brand {{font-weight:700;font-size:17pt;}} QLabel#title {{font-weight:650;font-size:21pt;}}
    QLabel#section {{font-weight:bold;color:{muted};font-size:9pt;}} QLabel#muted {{color:{muted};}}
    QLabel#accent {{color:{accent};font-weight:bold;}} QLabel#error {{color:{error};}}
    QPushButton,QToolButton,QComboBox,QSpinBox {{background:{raised};border:1px solid {line};border-radius:4px;padding:4px 9px;}}
    QPushButton:hover,QToolButton:hover,QTreeView::item:hover,QTabBar::tab:hover {{background:{selected};}}
    QPushButton#primary {{color:{bg};background:{accent};font-weight:bold;border:1px solid {accent};}}
    QPushButton:disabled,QToolButton:disabled,QTabBar::tab:disabled {{color:{muted};background:{panel};}}
    QWidget:focus {{outline:0;}} QPushButton:focus,QToolButton:focus,QLineEdit:focus,QTreeWidget:focus,QTableView:focus,QPlainTextEdit:focus,QTabBar::tab:focus {{border:1px solid {accent};}}
    QTreeWidget,QTableView,QPlainTextEdit,QTextBrowser {{border:1px solid {line};selection-background-color:{selected};selection-color:{ink};}}
    QTreeView::item {{height:{max(25, QFontMetrics(font).height() + 4)}px;}} QTreeView::item:selected,QTableView::item:selected {{background:{selected};color:{ink};}}
    QHeaderView::section {{color:{muted};padding:5px;text-align:left;}} QTableView {{gridline-color:{line};}}
    QTabWidget::pane {{border:0;}} QTabBar::tab {{padding:8px 10px;color:{muted};border-bottom:2px solid {line};}}
    QTabBar::tab:selected {{color:{accent};border-bottom:2px solid {accent};}}
    QSplitter::handle {{background:{line};}} QLineEdit {{background:{bg};padding:5px;border:1px solid {line};border-radius:4px;}}
    QToolTip {{background:{raised};color:{ink};border:1px solid {line};}}
    """)
    window.setAnimated(not prefs["reduced_motion"])
    return accent, muted, warn
