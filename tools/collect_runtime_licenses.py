"""Collect actual installed runtime license texts into the portable application.

This does not certify compliance. Verify all shipped DLLs/Qt plugins and
their license requirements before PUBLIC redistribution.
"""

import hashlib
import json
import shutil
import sys
from importlib.metadata import distribution
from pathlib import Path

PACKAGES = ("PySide6_Essentials", "shiboken6", "pyinstaller")
LICENSE_PARTS = ("license", "copying", "notice", "copyright")


def collect(folder):
    folder = Path(folder).resolve()
    if not (folder / "MiniDB-Studio.exe").exists():
        raise FileNotFoundError("Portable application executable is missing")
    dest = folder / "licenses"
    dest.mkdir(exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / "LICENSE", dest / "MiniDB-MIT.txt")
    # The 6.12.0 wheels have a commercial LicenseRef in dist-info; include
    # verbatim free-software license alternatives from Qt 6.12.0's source.
    license_root = root / "third_party_licenses"
    for name in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt"):
        src = license_root / name
        if not src.is_file() or "GENERAL PUBLIC LICENSE" not in src.read_text(encoding="utf-8"):
            raise FileNotFoundError(f"Required Qt LGPL/GPL license text absent: {src}")
        shutil.copy2(src, dest / name)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError("Python runtime LICENSE.txt not found")
    shutil.copy2(python_license, dest / "Python-LICENSE.txt")

    packages = []
    for name in PACKAGES:
        dist = distribution(name)
        copied = []
        for path in dist.files or ():
            rel = Path(str(path))
            if not any(part in rel.name.lower() for part in LICENSE_PARTS):
                if not any(part.lower() in LICENSE_PARTS for part in rel.parts):
                    continue
            absolute = Path(dist.locate_file(path))
            if not absolute.is_file():
                continue
            target = dest / dist.metadata["Name"] / rel.name
            # Separate identically named files from different subdirectories.
            if target.exists():
                target = dest / dist.metadata["Name"] / "_".join(rel.parts[-3:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(absolute, target)
            copied.append(str(target.relative_to(folder)).replace("\\", "/"))
        if not copied:
            raise RuntimeError(
                f"No installed copyright/license text located for {name}; "
                "public binary distribution is blocked"
            )
        packages.append({"name": dist.metadata["Name"], "version": dist.version, "notices": copied})

    binary_files = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".dll", ".pyd", ".exe"):
            binary_files.append({
                "path": path.relative_to(folder).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    (dest / "BINARY-INVENTORY.json").write_text(
        json.dumps({"python": sys.version, "packages": packages, "binaries": binary_files}, indent=2),
        encoding="utf-8",
    )
    (dest / "READ-ME-FIRST.txt").write_text(
        "MiniDB application source: MIT (MiniDB-MIT.txt).\n"
        "Bundled Python, Qt/PySide6/Shiboken and PyInstaller have separate licenses.\n"
        "See the included original notices and BINARY-INVENTORY.json.\n"
        "The full LGPLv3 and GPLv3 license texts from Qt 6.12.0 are included here.\\n"
        "This open-source distribution uses the LGPLv3 option where permitted.\\n"
        "For component source and LGPL user rights, consult:\\n"
        "https://doc.qt.io/qt-6/licensing.html\n"
        "https://www.qt.io/development/open-source-lgpl-obligations\n"
        "https://github.com/qtproject/pyside-pyside-setup\n"
        "https://www.gnu.org/licenses/lgpl-3.0.html\n"
        "https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt\n"
        "The Qt libraries are packaged as distinct DLLs; replacement may require matching ABI.\n"
        "Distribution requires human review of all included Qt modules, plugins and notices.\n",
        encoding="utf-8",
    )
    print(json.dumps({"notices": packages, "binary_count": len(binary_files)}))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tools/collect_runtime_licenses.py dist/MiniDB-Studio")
    collect(sys.argv[1])
