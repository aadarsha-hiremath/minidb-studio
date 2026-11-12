from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFontComboBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
)

from .style import mono


class Preferences(QDialog):
    def __init__(self, prefs, parent):
        super().__init__(parent)
        self.setWindowTitle("Studio settings")
        self.setMinimumWidth(390)
        self.reset_requested = False
        form = QFormLayout(self)
        form.addRow(QLabel("Appearance and editor"))

        def combo(key, choices):
            box = QComboBox()
            box.addItems(choices)
            box.setCurrentText(prefs[key])
            return box

        self.theme = combo("theme", ["Dark", "Light", "System"])
        self.density = combo("density", ["Comfortable", "Standard", "Compact"])
        self.size = QSpinBox()
        self.size.setRange(8, 20)
        self.size.setValue(prefs["font_size"])
        self.family = QFontComboBox()
        self.family.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        self.family.setCurrentFont(
            QFont(prefs["editor_font"]) if prefs["editor_font"] else mono()
        )
        self.wrap = QCheckBox("Wrap long SQL lines")
        self.wrap.setChecked(prefs["wrap"])
        self.motion = QCheckBox("Reduce interface motion")
        self.motion.setChecked(prefs["reduced_motion"])
        for label, widget in [
            ("Theme", self.theme),
            ("Table density", self.density),
            ("Text size (pt)", self.size),
            ("SQL typeface", self.family),
        ]:
            form.addRow(label, widget)
        form.addRow(self.wrap)
        form.addRow(self.motion)
        reset = QPushButton("Reset preferences and history")
        reset.clicked.connect(self.reset_all)
        form.addRow(reset)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def reset_all(self):
        self.reset_requested = True
        self.accept()

    def values(self):
        return {
            "theme": self.theme.currentText(),
            "density": self.density.currentText(),
            "font_size": self.size.value(),
            "editor_font": self.family.currentFont().family(),
            "wrap": self.wrap.isChecked(),
            "reduced_motion": self.motion.isChecked(),
        }
