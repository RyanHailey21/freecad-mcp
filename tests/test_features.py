"""fcmcp_features (feature recognition for drawings) on fixture solids, headless freecadcmd."""

import json
import textwrap
from pathlib import Path

import pytest

from freecad_mcp.headless import detect_freecadcmd, run_headless

FC = detect_freecadcmd()
WORKERS = Path(__file__).parents[1] / "src" / "freecad_mcp" / "fcworkers"

FIXTURES = textwrap.dedent(r'''
    import json, math, sys
    sys.path.insert(0, %(workers)r)
    import FreeCAD as App, Part
    import fcmcp_features as F
    V = App.Vector
    def plate():
        return Part.makeBox(40, 30, 10)                       # Z = thickness 10, holes along Z
    def cyl(r, h, x, y, z=0.0, d=V(0, 0, 1)):
        return Part.makeCylinder(r, h, V(x, y, z), d)
    def cone(r1, r2, h, x, y, z, d=V(0, 0, 1)):
        return Part.makeCone(r1, r2, h, V(x, y, z), d)
    def table(s):
        holes, side, t = F.hole_table(s)
        return {"holes": [F._jsonable(h) for h in holes], "side": [F._jsonable(h) for h in side], "t": t}
    out = {}
    out["thru"] = table(plate().cut(cyl(2, 10, 10, 10)))
    out["blind_near"] = table(plate().cut(cyl(2, 4, 10, 10, 6)))          # from the top (+Z) face
    out["blind_far"] = table(plate().cut(cyl(2, 4, 10, 10, 0)))           # from the bottom face
    out["cbore_near"] = table(plate().cut(cyl(1.65, 10, 20, 15)).cut(cyl(3, 3, 20, 15, 7)))
    out["cbore_far"] = table(plate().cut(cyl(1.65, 10, 20, 15)).cut(cyl(3, 3, 20, 15, 0)))
    out["csk_near"] = table(plate().cut(cyl(1.65, 10, 20, 15)).cut(cone(1.65, 3.3, 1.65, 20, 15, 10 - 1.65)))
    out["csk_far"] = table(plate().cut(cyl(1.65, 10, 20, 15)).cut(cone(3.3, 1.65, 1.65, 20, 15, 0)))
    out["each_side"] = table(plate().cut(cyl(1.25, 3, 10, 10, 0)).cut(cyl(1.25, 3, 10, 10, 7)))
    out["side_blind"] = table(plate().cut(cyl(1.5, 8, 0, 20, 5, V(1, 0, 0))))        # opens on the x = 0 face
    out["side_pair"] = table(plate().cut(cyl(1.5, 8, 0, 20, 5, V(1, 0, 0))).cut(cyl(1.5, 8, 32, 20, 5, V(1, 0, 0))))
    stepped = Part.makeBox(40, 30, 30).cut(Part.makeBox(10, 10, 10, V(5, 17, 0)))   # pocket open to the bottom
    out["coaxial_split"] = table(stepped.cut(cyl(1.25, 6, 10, 22, 10)).cut(cyl(1.25, 6, 10, 22, 24)))
    disc = Part.makeCylinder(15, 6)
    dcut = Part.makeBox(10, 20, 6, V(2.5, -10, 0))                             # flat 2.5 from the axis
    out["dbore"] = table(disc.cut(cyl(3, 6, 0, 0).cut(dcut)))
    sliver = Part.makeBox(10, 20, 6, V(2.95, -10, 0))                          # 0.05 chord: seam artefact
    out["dbore_sliver"] = table(disc.cut(cyl(3, 6, 0, 0).cut(sliver)))
    hexp = Part.makePolygon([V(5.7735 * math.cos(math.radians(60 * i)), 5.7735 * math.sin(math.radians(60 * i)), 0)
                             for i in range(7)])
    hexprism = Part.Face(hexp).extrude(V(0, 0, 8))                             # AF 10
    out["hex_hole"] = F.features(Part.makeBox(30, 30, 8, V(-15, -15, 0)).cut(hexprism))
    out["hex_boss"] = F.features(hexprism)
    shaft = Part.makeCylinder(5, 100, V(0, 0, 0), V(1, 0, 0))
    for gx in (20, 80):
        shaft = shaft.cut(Part.makeCylinder(5, 0.94, V(gx, 0, 0), V(1, 0, 0)).cut(Part.makeCylinder(3.5, 0.94, V(gx, 0, 0), V(1, 0, 0))))
    shaft = shaft.cut(Part.makeBox(30, 10, 10, V(70, -5, 4.5)))                 # drive flat 0.5 deep, parallel to axis
    out["shaft"] = F.features(shaft)
    box = Part.makeBox(60, 20, 8)
    out["orient"] = [round(x, 3) for x in (F.oriented(Part.makeBox(8, 60, 20).cut(cyl(2, 60, 4, 0, 10, V(0, 1, 0)))).BoundBox.ZLength,)]
    out["sym"] = [F.symmetric(box), F.symmetric(box.cut(cyl(2, 8, 10, 10)))]
    out["describe"] = F.describe(plate().cut(cyl(2, 10, 10, 10)))["holes"][0]["thru"]
    print("FEATURES_JSON" + json.dumps(out))
''')


@pytest.fixture(scope="module")
def res():
    if FC is None:
        pytest.skip("freecadcmd not available")
    r = run_headless(FIXTURES % {"workers": str(WORKERS)}, 180, FC)
    line = [ln for ln in r.get("output", "").splitlines() if ln.startswith("FEATURES_JSON")]
    assert line, r.get("output", "")[-3000:]
    return json.loads(line[0][len("FEATURES_JSON"):])


def one(res, key):
    hs = res[key]["holes"]
    assert len(hs) == 1, hs
    return hs[0]


def test_through_and_blind_sides(res):
    h = one(res, "thru")
    assert h["thru"] and not h["far"] and h["depth"] == pytest.approx(10)
    near, far = one(res, "blind_near"), one(res, "blind_far")
    assert not near["thru"] and not near["far"] and near["depth"] == pytest.approx(4)
    assert not far["thru"] and far["far"]                       # opens on the bottom face only


def test_counterbore_side_probed_in_the_ring(res):
    near, far = one(res, "cbore_near"), one(res, "cbore_far")
    assert near["thru"] and far["thru"]                         # bore + counterbore column open both ends
    assert near["cbore"] == pytest.approx([6, 3]) and far["cbore"] == pytest.approx([6, 3])
    assert near["cb_far"] is False and far["cb_far"] is True


def test_countersink_angle_and_side(res):
    near, far = one(res, "csk_near"), one(res, "csk_far")
    assert near["csk"][0] == pytest.approx(6.6) and near["csk"][1] == pytest.approx(90, abs=0.01)
    assert near["csk"][2] is False and far["csk"][2] is True
    assert near["thru"] and far["thru"]


def test_each_side_blind_holes(res):
    h = one(res, "each_side")
    assert h["each_side"] and not h["thru"] and not h["far"] and h["depth"] == pytest.approx(3)


def test_side_holes_split_by_extent_with_open_dir(res):
    s = res["side_blind"]["side"]
    assert len(s) == 1 and not s[0]["thru"] and s[0]["depth"] == pytest.approx(8)
    assert s[0]["open_dir"] == pytest.approx([-1, 0, 0])        # open toward -X
    pair = sorted(res["side_pair"]["side"], key=lambda h: h["t0"])
    assert len(pair) == 2                                       # coaxial, same radius: two features
    assert {tuple(round(v) for v in h["open_dir"]) for h in pair} == {(-1, 0, 0), (1, 0, 0)}


def test_dbore_threshold(res):
    d = one(res, "dbore")
    assert d["dflat"] == pytest.approx(2.5, abs=0.01) and d["thru"]
    s = one(res, "dbore_sliver")
    assert "dflat" not in s                                     # 0.05 chord is not a D-bore


def test_hex_drives(res):
    hh, hb = res["hex_hole"]["hex"], res["hex_boss"]["hex"]
    assert len(hh) == 1 and hh[0]["hole"] and hh[0]["af"] == pytest.approx(10, abs=0.01)
    assert len(hb) == 1 and not hb[0]["hole"] and hb[0]["af"] == pytest.approx(10, abs=0.01)


def test_shaft_grooves_and_drive_flat(res):
    s = res["shaft"]
    assert s["groove"] and s["grooves"] == 2 and s["flat"] and not s["hex"]


def test_orientation_and_symmetry(res):
    assert res["orient"] == [60.0]                              # the (Y, 60 long) hole axis becomes Z
    assert res["sym"] == [True, True]                           # mirror about a mid-plane still holds with one hole
    assert res["describe"] is True


def test_coaxial_blind_holes_from_different_faces_stay_separate(res):
    """ARC-106: one blind hole opens into a pocket below, the other from the top, material between."""
    hs = sorted(res["coaxial_split"]["holes"], key=lambda h: h["z0"])
    assert len(hs) == 2, hs
    low, top = hs
    assert not low["thru"] and not top["thru"]
    assert low["far"] and not top["far"]                      # opens downward into the pocket / from the top
    assert low["depth"] == pytest.approx(6) and top["depth"] == pytest.approx(6)
