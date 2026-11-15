"""Rasterise exported drawing PDFs to PNG for quick visual review (PyMuPDF).

PyMuPDF is used in-process when installed (``pip install freecad-mcp[drawings]``);
otherwise any Python on PATH (or ``FREECAD_MCP_PDF_PYTHON``) that can import it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_SNIPPET = (
    "import sys\n"
    "try:\n import pymupdf as m\nexcept ImportError:\n import fitz as m\n"
    "d = m.open(sys.argv[1]); dpi = int(sys.argv[2]); out = []\n"
    "for i, pg in enumerate(d):\n"
    "    p = sys.argv[3] if len(d) == 1 else sys.argv[3].replace('.png', '_p%d.png' % (i + 1))\n"
    "    pg.get_pixmap(dpi=dpi).save(p); out.append(p)\n"
    "print('\\n'.join(out))\n"
)


def _inprocess(pdf: str, dpi: int, png: str) -> list[str] | None:
    try:
        import pymupdf as m  # type: ignore
    except ImportError:
        try:
            import fitz as m  # type: ignore
        except ImportError:
            return None
    doc = m.open(pdf)
    out = []
    for i, page in enumerate(doc):
        p = png if len(doc) == 1 else png.replace(".png", f"_p{i + 1}.png")
        page.get_pixmap(dpi=dpi).save(p)
        out.append(p)
    return out


def rasterize(pdf: str, dpi: int = 110, png: str | None = None) -> list[str]:
    png = png or str(Path(pdf).with_suffix(".png"))
    got = _inprocess(pdf, dpi, png)
    if got is not None:
        return got
    # The py launcher follows VIRTUAL_ENV; drop it so it picks the user's default Python.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    for exe in (os.environ.get("FREECAD_MCP_PDF_PYTHON"), shutil.which("python"), shutil.which("py"),
                shutil.which("python3")):
        if not exe:
            continue
        try:
            # -E (not -I): keep the user site-packages, where PyMuPDF is often installed; run from a
            # neutral directory so no local module shadows the import.
            proc = subprocess.run([exe, "-E", "-c", _SNIPPET, pdf, str(dpi), png], capture_output=True, text=True,
                                  timeout=120, cwd=tempfile.gettempdir(), env=env)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return [ln for ln in proc.stdout.splitlines() if ln.strip()]
    raise RuntimeError("PyMuPDF not available: pip install pymupdf (or freecad-mcp[drawings])")
