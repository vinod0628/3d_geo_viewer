"""Generate packaging/geoviewer.ico (the .exe icon) from the app's toolbar icon set.

Run once after changing the design; the .ico is kept in the repository so
release builds do not depend on this script:
    .venv\\Scripts\\python.exe packaging\\make_icon.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int):
    """The design lives in the app (the window / taskbar icon uses the same drawing)."""
    sys.path.insert(0, str(HERE.parent))
    from geoviewer.ui.icons import render_app_icon

    return render_app_icon(size)


def main() -> int:
    from PIL import Image
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv)
    tmp = HERE / "_icon_256.png"
    render(256).save(str(tmp))
    big = Image.open(tmp).convert("RGBA")
    out = HERE / "geoviewer.ico"
    big.save(out, sizes=[(s, s) for s in SIZES])
    tmp.unlink()
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
