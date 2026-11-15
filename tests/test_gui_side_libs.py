"""GUI-side libraries: compile, script builder safety, pure helpers; assembly helpers headless."""

import json
import math
import struct
import textwrap
import zlib
from pathlib import Path

import pytest

from freecad_mcp import gui_lib
from freecad_mcp.headless import detect_freecadcmd, run_headless

GUISIDE = Path(__file__).parents[1] / "src" / "freecad_mcp" / "guiside"
FC = detect_freecadcmd()
needs_fc = pytest.mark.skipif(FC is None, reason="freecadcmd not available")


@pytest.mark.parametrize("name", ["techdraw_lib", "render_lib", "assembly_lib"])
def test_libraries_compile_into_scripts(name):
    lib = (GUISIDE / f"{name}.py").read_text(encoding="utf-8")
    code = gui_lib.build_script(lib, "some_fn", {"doc": "D"})
    compile(code, name, "exec")


def test_params_are_data_not_code():
    nasty = "x\"); import os; os.system('boom') #"
    code = gui_lib.build_script("def f(p):\n    return p['doc']\n", "f", {"doc": nasty})
    ns: dict = {}
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, ns)
    assert gui_lib.parse_reply(buf.getvalue()) == {"ok": True, "result": nasty}
    with pytest.raises(ValueError):
        gui_lib.build_script("", "f(); import os", {})


def test_errors_are_reported_with_traceback():
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(gui_lib.build_script("def f(p):\n    raise KeyError('nope')\n", "f", {}), {})
    rep = gui_lib.parse_reply(buf.getvalue())
    assert not rep["ok"] and "KeyError" in rep["error"] and "Traceback" in rep["traceback"]
    assert gui_lib.parse_reply("garbage")["ok"] is False


class _Conn:
    def __init__(self):
        self.code = None

    def execute_code(self, code, timeout=None):
        import contextlib
        import io

        self.code = code
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, {})
        return {"success": True, "message": buf.getvalue()}


def test_run_gui_roundtrip_and_error():
    conn = _Conn()
    assert gui_lib.run_gui(conn, "def f(p):\n    return p['a'] + 1\n", "f", {"a": 1}) == 2
    with pytest.raises(RuntimeError, match="ZeroDivisionError"):
        gui_lib.run_gui(conn, "def f(p):\n    return 1/0\n", "f", {})


def _load_pure(name, stub_names=("FreeCAD", "FreeCADGui")):
    """Exec a GUI-side lib with FreeCAD stubbed, for its pure helpers."""
    import sys
    import types

    saved = {k: sys.modules.get(k) for k in stub_names}
    fake = types.ModuleType("FreeCAD")
    fake.Vector = lambda *a: tuple(a)
    for k in stub_names:
        sys.modules[k] = fake
    try:
        ns: dict = {}
        exec((GUISIDE / f"{name}.py").read_text(encoding="utf-8"), ns)
        return ns
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_techdraw_tokens_and_scales():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("fcmcp_asme_rules", GUISIDE / "asme_rules.py")
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    sys.modules["fcmcp_asme_rules"] = rules
    try:
        ns = _load_pure("techdraw_lib")
    finally:
        sys.modules.pop("fcmcp_asme_rules", None)
    assert ns["T"]("6X {dia}3.3 THRU {position}") == "6X ⌀3.3 THRU ⌖"
    assert ns["_fcf_text"]({"symbol": "position", "tolerance": "0.1", "diameter": True, "modifier": "mmc",
                            "datums": ["A", "B"]}) == "⌖|⌀0.1Ⓜ|A|B"
    assert ns["_fcf_text"]({"cells": ["flatness", "0.01"]}) == "⏥|0.01"
    assert ns["pick_scale"](100, 50, 205, 150) == 2
    assert ns["scale_txt"](0.5) == "1:2" and ns["scale_txt"](3) == "3:1" and ns["scale_txt"](0.75) == "3:4"
    assert ns["hole_text"]({"r": 1.25, "thru": False, "depth": 6.0}, {"1.25": "M3x0.5-6H"}) == "M3x0.5-6H ↧5.5"
    # Y14.5 4.3.1: metric values without trailing zeros (was "↧3.0")
    assert ns["hole_text"]({"r": 1.65, "thru": True, "depth": 3, "cbore": (6.5, 3.0)}) == \
        "⌀3.3 THRU  ⌴⌀6.5 ↧3"
    assert ns["hole_text"]({"r": 1.25, "thru": True, "depth": 5}, {"1.25": "M3x0.5-6H"}) == "M3x0.5-6H THRU"
    # machined bearing seat / dowel hole: ISO 286 H7 fit; printed parts: no fit
    assert ns["hole_text"]({"r": 7.5, "thru": False, "depth": 5}) == "⌀15 H7 ↧5"
    assert ns["hole_text"]({"r": 1.5, "thru": True, "depth": 5}, None, "printed") == "⌀3 THRU"
    assert ns["hole_text"]({"r": 2.0, "thru": True, "depth": 5, "dflat": 1.2}) == \
        "⌀4 H7 D-BORE, FLAT 1.2 FROM ℄ THRU"


def test_render_camera_path():
    ns = _load_pure("render_lib")
    cam = {"keys": [{"t": 0, "position": [0, 0, 0], "target": [1, 0, 0]},
                    {"t": 1, "position": [10, 0, 0], "target": [1, 0, 0]}], "ease": False}
    pos, tgt, up = ns["camera_at"](cam, 5, 11)
    assert pos == [5.0, 0.0, 0.0] and up == [0, 0, 1]
    orb = {"orbit": {"center": [0, 0, 0], "radius": 10, "height": 0, "start_deg": 0, "end_deg": 90}}
    pos, _t, _u = ns["camera_at"](orb, 1, 2)
    assert pos[0] == pytest.approx(0, abs=1e-9) and pos[1] == pytest.approx(10)


# ---------------------------------------------------------------- assembly helpers, headless
ASM = textwrap.dedent('''
    import FreeCAD as App, Part
    import JointObject
    V = App.Vector
    d = App.newDocument("AsmT")
    asm = d.addObject("Assembly::AssemblyObject", "Assembly")
    jg = asm.newObject("Assembly::JointGroup", "Joints")
    parts = {}
    for name, base in (("Ground", V(0, 0, 0)), ("Lid", V(3, 4, 15))):
        ap = asm.newObject("App::Part", name)
        ap.Placement.Base = base
        f = d.addObject("Part::Feature", name + "_box")
        f.Shape = Part.makeBox(10, 10, 2)
        ap.addObject(f)
        parts[name] = (ap, f)
    d.recompute()
    g = jg.newObject("App::FeaturePython", "Grounded")
    JointObject.GroundedJoint(g, parts["Ground"][0])
    j = jg.newObject("App::FeaturePython", "Fix")
    JointObject.Joint(j, JointObject.JointTypes.index("Fixed"))
    for k, nm in ((1, "Ground"), (2, "Lid")):
        ap, f = parts[nm]
        setattr(j, "Reference%d" % k, [ap, [f.Name + ".Vertex1", f.Name + ".Vertex1"]])
        setattr(j, "Detach%d" % k, True)
    j.Placement1 = App.Placement(V(0, 0, 10), App.Rotation())
    j.Placement2 = App.Placement()
    d.recompute()                      # (recompute solves the assembly)
    parts["Lid"][0].Placement.Base = V(3, 4, 15)   # stored placement now violates the fixed joint
    d.saveAs(PATH)
''')


@needs_fc
def test_assembly_solve_check_reports_drift_headless(tmp_path):
    path = tmp_path / "asm.FCStd"
    assert run_headless(f"PATH = {str(path)!r}\n" + ASM, 120, FC)["success"]
    lib = (GUISIDE / "assembly_lib.py").read_text(encoding="utf-8")
    res = gui_lib.run_headless_lib(FC, lib, "solve_check", {"path": str(path)}, 120)
    assert res["solved"] is True, res
    worst = {r[0]: r for r in res["worst"]}
    assert worst["Lid"][1] == pytest.approx(math.sqrt(9 + 16 + 25), abs=1e-3)
    assert res["moved_parts"] == 1 and res["restored"] is True
    with pytest.raises(RuntimeError, match="no simulation"):
        gui_lib.run_headless_lib(FC, lib, "simulation_check", {"path": str(path)}, 120)


# ---------------------------------------------------------------- video encode
def _png(path, w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def test_encode_video(tmp_path):
    from freecad_mcp import video

    if not (video.find_ffmpeg() or video.find_blender()):
        pytest.skip("no ffmpeg / blender")
    frames = tmp_path / "frames"
    frames.mkdir()
    for i in range(12):
        _png(frames / f"frame_{i:05d}.png", 65, 33, (i * 20, 100, 200))   # odd size: padded to even
    res = video.encode_video(str(frames), str(tmp_path / "out.mp4"), fps=12)
    assert res["frames"] == 12 and res["duration_s"] == 1.0
    data = (tmp_path / "out.mp4").read_bytes()
    assert b"ftyp" in data[:64]
    with pytest.raises(FileNotFoundError):
        video.encode_video(str(frames), str(tmp_path / "x.mp4"), pattern="nothing_%04d.png")


def test_pdf_rasterize(tmp_path):
    from freecad_mcp import pdf_raster

    pdf = tmp_path / "a.pdf"
    # minimal one-page PDF
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    pdf.write_bytes(out)
    try:
        pngs = pdf_raster.rasterize(str(pdf), 72)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    assert len(pngs) == 1 and Path(pngs[0]).read_bytes()[:4] == b"\x89PNG"
