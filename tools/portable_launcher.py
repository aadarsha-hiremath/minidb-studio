"""PyInstaller entry point for the portable MiniDB Studio GUI.

Keep this outside the package to ensure reliable import semantics when frozen.
The internal --smoke-test option is used only for a disposable CI check.
"""

import sys
from pathlib import Path

from minidb_studio import main
from minidb_studio.portable_smoke import run as smoke_test


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        raise SystemExit(smoke_test(Path(sys.argv[2])))
    raise SystemExit(main())
