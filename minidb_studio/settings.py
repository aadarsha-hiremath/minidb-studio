"""Validated per-user preferences written atomically via QSaveFile."""

import json
from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile, QStandardPaths

DEFAULTS = {
    "theme": "Dark",
    "density": "Standard",
    "font_size": 10,
    "editor_font": "",
    "wrap": False,
    "reduced_motion": True,
    "recent": [],
    "panels": [220, 800, 240],
    "sql_split": [310, 330],
    "columns": {},
    "window": [1200, 760],
    "sidebar": True,
    "inspector": True,
}


def valid(key, value):
    if key in ("theme", "density"):
        choices = (
            ("Dark", "Light", "System")
            if key == "theme"
            else ("Comfortable", "Standard", "Compact")
        )
        return value in choices
    if key == "font_size":
        return type(value) is int and 8 <= value <= 20
    if key == "editor_font":
        return isinstance(value, str) and len(value) <= 128
    if key == "recent":
        return (
            isinstance(value, list)
            and len(value) <= 8
            and all(isinstance(p, str) and len(p) <= 32768 for p in value)
        )
    if key in ("panels", "sql_split", "window"):
        return (
            isinstance(value, list)
            and len(value) == len(DEFAULTS[key])
            and all(type(n) is int and 0 <= n <= 32000 for n in value)
            and sum(value) > 0
        )
    if key == "columns":
        return isinstance(value, dict) and all(
            isinstance(k, str)
            and isinstance(v, dict)
            and all(
                isinstance(c, str)
                and isinstance(p, list)
                and len(p) == 2
                and type(p[0]) is int
                and 24 <= p[0] <= 3000
                and type(p[1]) is bool
                for c, p in v.items()
            )
            for k, v in value.items()
        )
    return type(value) is bool


class Settings:
    def __init__(self, path=None):
        self.path = (
            Path(path)
            if path
            else Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppConfigLocation
                )
            )
            / "studio.json"
        )
        self.reset()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return
        if isinstance(raw, dict):
            self.values.update(
                {k: v for k, v in raw.items() if k in DEFAULTS and valid(k, v)}
            )

    def reset(self):
        from copy import deepcopy

        self.values = deepcopy(DEFAULTS)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = QSaveFile(str(self.path))
        data = json.dumps(self.values, ensure_ascii=False).encode()
        if (
            not file.open(QIODevice.OpenModeFlag.WriteOnly)
            or file.write(data) != len(data)
            or not file.commit()
        ):
            raise OSError(file.errorString())
