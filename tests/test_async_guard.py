"""execute_code_async guard: TechDraw/GUI work outside commit() is refused, document writes warned."""

from freecad_mcp.async_guard import check
from freecad_mcp.operations.core import execute_code_async_operation


class Conn:
    def __init__(self):
        self.calls = 0

    def execute_code_async(self, code):
        self.calls += 1
        return {"success": True, "job_id": "j1"}


def test_techdraw_outside_commit_is_refused():
    r = check("import TechDraw\npage = doc.addObject('TechDraw::DrawPage', 'Page')\n")
    assert r["refuse"] and any("TechDraw" in x for x in r["refuse"]) and r["warn"]
    conn = Conn()
    [t] = execute_code_async_operation(conn, "import TechDrawGui\nTechDrawGui.exportPageAsPdf(p, 'x.pdf')")
    assert t.text.startswith("REFUSED") and conn.calls == 0


def test_inside_commit_is_fine():
    code = (
        "fused = a.fuse(b)\n"
        "def apply():\n"
        "    import TechDraw\n"
        "    obj.Shape = fused\n"
        "    doc.recompute()\n"
        "commit(apply)\n"
        "commit(lambda: doc.addObject('TechDraw::DrawPage', 'P'))\n"
    )
    assert check(code) == {"refuse": [], "warn": []}
    conn = Conn()
    [t] = execute_code_async_operation(conn, code)
    assert "job_id: j1" in t.text and "WARNING" not in t.text and conn.calls == 1


def test_document_writes_outside_commit_warn_but_start():
    conn = Conn()
    [t] = execute_code_async_operation(conn, "s = a.cut(b)\ndoc.recompute()\n")
    assert t.text.startswith("WARNING") and ".recompute()" in t.text and conn.calls == 1


def test_allow_gui_overrides_with_a_warning():
    conn = Conn()
    [t] = execute_code_async_operation(conn, "import FreeCADGui\n", allow_gui=True)
    assert t.text.startswith("WARNING") and conn.calls == 1
