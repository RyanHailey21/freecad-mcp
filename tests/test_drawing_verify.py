"""techdraw_verify: callout parsing + model comparison (pure), and the headless extraction end to end."""

import textwrap
from pathlib import Path

import pytest

from freecad_mcp import drawing_verify as DV
from freecad_mcp.gui_lib import run_headless_lib
from freecad_mcp.headless import detect_freecadcmd

FC = detect_freecadcmd()
FEATURES = Path(__file__).parents[1] / "src" / "freecad_mcp" / "fcworkers" / "fcmcp_features.py"


def test_parse_callouts():
    c = DV.parse_callout("4X ⌀3.3 THRU  ⌴⌀6 ↧3 FAR SIDE")
    assert c["count"] == 4 and c["dia"] == 3.3 and c["thru"] and c["cbore"] == [6, 3] and c["cb_far"] and not c["far"]
    c = DV.parse_callout("2X M3x0.5-6H ↧4.5 EACH SIDE")
    assert c["thread"] == "M3" and c["depth"] == 4.5 and c["each_side"] and not c["thru"]
    c = DV.parse_callout("⌀5 ↧4 FAR SIDE")
    assert c["count"] == 1 and c["far"] and c["depth"] == 4
    c = DV.parse_callout("6X ⌀3.3 THRU  ⌵⌀6.6 X 90° FAR SIDE")
    assert c["csk"] == [6.6, 90] and c["csk_far"]
    c = DV.parse_callout("4X ⌴⌀6 ↧3")
    assert c["cbore_only"] and c["cbore"] == [6, 3]
    assert DV.parse_callout("ISOMETRIC (REF)") is None
    assert DV.parse_callout("⌀15 k6  Ra 0.8")["dia"] == 15


def _hole(r, thru=True, depth=10.0, **kw):
    return dict(dict(r=r, thru=thru, depth=depth, cbore=None, cb_far=False, csk=None, each_side=False, far=False), **kw)


def _data(holes, balloons, thickness=10.0, notes=()):
    return {"model": {"thickness": thickness, "holes": holes, "side_holes": []},
            "balloons": [{"name": "B%d" % i, "label": lab, "text": txt} for i, (lab, txt) in enumerate(balloons)],
            "notes": [{"label": "Notes", "text": n} for n in notes]}


def test_compare_clean_sheet():
    holes = [_hole(1.65, cbore=(6.0, 3.0))] * 4 + [_hole(2.0, thru=False, depth=4.0, far=True)] * 2
    d = _data(holes, [("Call_g0", "4X ⌀3.3 THRU  ⌴⌀6 ↧3"), ("Call_g1", "2X ⌀4 ↧4 FAR SIDE"),
                      ("FCF_g0", "⌖|⌀0.1|A"), ("Datum_A", "A")])
    r = DV.compare(d)
    assert r["clean"] and r["errors"] == 0 and r["callouts_checked"] == 2, r


def test_compare_finds_review_defects():
    holes = [_hole(1.65, cbore=(6.0, 3.0), cb_far=True)] * 4 + [_hole(2.0, thru=False, depth=4.0, far=True)] * 2 \
        + [_hole(1.65, csk=(6.6, 82.0, False))] * 2 + [_hole(3.0)]
    d = _data(holes, [("Call_g0", "3X ⌀3.3 THRU  ⌴⌀6 ↧3"),         # wrong count, counterbore side wrong
                      ("Call_g1", "2X ⌀4 ↧10"),                    # depth = thickness, FAR SIDE missing
                      ("Call_g2", "2X ⌀3.3 THRU  ⌵⌀6.6 X 90°"),    # countersink angle wrong
                      ("FCF_g0", "⌖|⌀0.1|A|B")])
    r = DV.compare(d)
    issues = "\n".join(f["issue"] for f in r["findings"])
    assert "count 3X on the sheet, 4 in the model" in issues
    assert "counterbore FAR SIDE missing" in issues
    assert "not less than the part thickness" in issues and "FAR SIDE missing" in issues
    assert "countersink angle 90° vs model 82°" in issues
    assert "model hole group 1X ⌀6 THRU has no callout" in issues
    assert "datum A referenced" in issues and "datum B referenced" in issues
    assert not r["clean"]


def test_split_datum_counterbore_and_thread_depth():
    holes = [_hole(1.65, cbore=(6.0, 3.0))] * 4 + [_hole(1.25, thru=False, depth=6.0)] * 2
    d = _data(holes, [("Call_g0", "4X ⌀3.3 THRU"), ("Call_g30", "4X ⌴⌀6 ↧3"),
                      ("Call_g1", "2X M3x0.5-6H ↧7")])           # thread deeper than the drilled hole
    r = DV.compare(d)
    issues = [f["issue"] for f in r["findings"]]
    assert not any("counterbore" in i for i in issues), issues
    assert any("thread depth 7 deeper than the drilled hole 6" in i for i in issues)


@pytest.mark.skipif(FC is None, reason="freecadcmd not available")
def test_extract_and_verify_headless(tmp_path):
    from freecad_mcp.headless import run_headless

    doc = tmp_path / "dwg.FCStd"
    code = textwrap.dedent(f'''
        import FreeCAD as App, Part
        V = App.Vector
        d = App.newDocument("Dwg")
        s = Part.makeBox(40, 30, 10).cut(Part.makeCylinder(2, 4, V(10, 10, 0))).cut(Part.makeCylinder(2, 4, V(30, 10, 0)))
        f = d.addObject("Part::Feature", "Part")
        f.Shape = s
        n = d.addObject("App::FeaturePython", "Dummy")
        d.saveAs({str(doc)!r})
    ''')
    assert run_headless(code, 120, FC)["success"]
    data = run_headless_lib(FC, DV.EXTRACT_LIB, "extract", {"path": str(doc)}, 120,
                            modules={"fcmcp_features": FEATURES.read_text(encoding="utf-8")})
    assert data["model"]["thickness"] == pytest.approx(10)
    hs = data["model"]["holes"]
    assert len(hs) == 2 and all(h["far"] and not h["thru"] for h in hs)     # blind from the bottom face
    data["balloons"] = [{"name": "C", "label": "Call_g0", "text": "2X ⌀4 ↧4"}]
    r = DV.compare(data)
    assert any("FAR SIDE missing" in f["issue"] for f in r["findings"])
    data["balloons"] = [{"name": "C", "label": "Call_g0", "text": "2X ⌀4 ↧4 FAR SIDE"}]
    assert DV.compare(data)["clean"]


def test_external_journals_and_rounded_overrides():
    holes = [_hole(1.27)] + [_hole(4.05)]
    d = _data(holes, [("Call_g21", "⌀12 k6  Ra 0.8 FAR SIDE"),           # journal: not a hole
                      ("Call_g0", "⌀2.5 THRU; M3x0.5-6H ↧8 BOTH ENDS"),   # override text rounds 2.54 -> 2.5
                      ("Call_g1", "Tr8x8(P2) LH-7H THRU")])
    r = DV.compare(d)
    assert r["clean"] and r["external_callouts_skipped"] == 1 and r["callouts_checked"] == 2, r


def test_both_ends_on_a_through_hole_is_not_each_side():
    d = _data([_hole(1.27, depth=30.0)], [("Call_g0", "⌀2.5 THRU; M3x0.5-6H ↧8 BOTH ENDS")], thickness=30.0)
    assert DV.compare(d)["findings"] == []
