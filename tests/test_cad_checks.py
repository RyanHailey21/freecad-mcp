"""Interference / hole checks: pure-python logic always, headless FreeCAD integration when available."""

import json
import os
import math
import textwrap
import threading
import time

import pytest

from freecad_mcp import interference as ic
from freecad_mcp.headless import detect_freecadcmd
from freecad_mcp.jobs import JobManager, run_or_queue
from freecad_mcp.parallel import JsonCache, split_balanced

IDENT = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]


def rot_z(deg, t=(0, 0, 0)):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [c, -s, 0, t[0], s, c, 0, t[1], 0, 0, 1, t[2]]


def test_relkey_is_invariant_under_common_motion():
    a, b = rot_z(0, (10, 0, 0)), rot_z(30, (20, 5, 0))
    k1 = ic.relkey(a, b)
    # move both bodies by the same rigid motion -> same relative placement
    m = rot_z(75, (3, -4, 7))

    def mul(p, q):
        out = []
        for i in range(3):
            for j in range(4):
                v = sum(p[4 * i + k] * q[4 * k + j] for k in range(3)) + (p[4 * i + 3] if j == 3 else 0)
                out.append(v)
        return out

    assert ic.relkey(mul(m, a), mul(m, b)) == k1
    assert ic.relkey(a, rot_z(31, (20, 5, 0))) != k1
    assert ic.relkey(IDENT, IDENT) == "1.000,0.000,0.000,0.000,0.000,1.000,0.000,0.000,0.000,0.000,1.000,0.000"


def test_bbox_hit_with_gap():
    a, b = [0, 0, 0, 1, 1, 1], [1.5, 0, 0, 2, 1, 1]
    assert not ic.bbox_hit(a, b)
    assert ic.bbox_hit(a, b, gap=0.6)


def _manifest():
    def item(name, group, fp, x):
        return [name, group, fp, rot_z(0, (x, 0, 0)), [x, 0, 0, x + 10, 10, 10]]
    return {"shapes": {}, "poses": [
        {"label": "p0", "items": [item("Base", "G", "fa", 0), item("Arm", "G", "fb", 5), item("Pin", "H", "fc", 8),
                                  item("Far", "H", "fd", 100)]},
        {"label": "p1", "items": [item("Base", "G", "fa", 0), item("Arm", "G", "fb", 5), item("Pin", "H", "fc", 50),
                                  item("Far", "H", "fd", 100)]},
    ]}


def test_select_pairs_filters_and_dedupes():
    m = _manifest()
    pairs, stats = ic.select_pairs(m)
    names = {(p[0], p[1], p[2]) for p in pairs}
    assert (0, "Base", "Arm") in names or (0, "Arm", "Base") in names
    assert not any("Far" in (a, b) for _pi, a, b, *_ in pairs)          # bbox prefilter
    keys = {p[3] for p in pairs if {"Base", "Arm"} == {p[1], p[2]}}
    assert len(keys) == 1                                                # same relative pose in both poses
    pairs, _ = ic.select_pairs(m, skip_same_group=True)
    assert all({"Base", "Arm"} != {p[1], p[2]} for p in pairs)
    pairs, _ = ic.select_pairs(m, only="Pin")
    assert pairs and all("Pin" in (p[1], p[2]) for p in pairs)
    pairs, _ = ic.select_pairs(m, ignore=[["Base", "Arm"]])
    assert all({"Base", "Arm"} != {p[1], p[2]} for p in pairs)
    assert stats["pairs"] == 12


def test_select_pairs_canonical_swap_keeps_every_pair():
    # fingerprints in descending order force a swap on every pair; each pair must stay distinct
    items = [[f"P{i}", f"G{i}", f"f{9 - i}", rot_z(0, (i, 0, 0)), [0, 0, 0, 10, 10, 10]] for i in range(5)]
    pairs, _ = ic.select_pairs({"shapes": {}, "poses": [{"label": "p", "items": items}]})
    got = {frozenset((a, b)) for _pi, a, b, _k, _t in pairs}
    assert got == {frozenset((f"P{i}", f"P{j}")) for i in range(5) for j in range(i + 1, 5)}
    for _pi, a, b, key, task in pairs:
        fa, fb = key.split("|")[1:3]
        assert {fa, fb} == {"f%d" % (9 - int(a[1])), "f%d" % (9 - int(b[1]))}


def test_split_balanced_spreads_cost():
    bins = split_balanced(list(range(10)), lambda x: x, 3)
    loads = sorted(sum(b) for b in bins)
    assert len(bins) == 3 and loads[-1] - loads[0] <= 9
    assert split_balanced([1], lambda x: x, 8) == [[1]]


def test_json_cache_merges_concurrent_writers(tmp_path):
    p = tmp_path / "c.json"
    a, b = JsonCache(p), JsonCache(p)
    a.put("x", 1)
    b.put("y", 2)
    a.save()
    b.save()
    assert json.loads(p.read_text()) == {"x": 1, "y": 2}
    assert JsonCache(p).get("x") == 1


def test_job_manager_progress_result_and_failure():
    jm = JobManager()

    def ok(job):
        for i in range(3):
            job.progress(i + 1, 3, "step")
        return {"answer": 42}

    j = jm.start("ok", ok)
    assert jm.wait(j.id, 5).state == "done"
    s = j.summary()
    assert s["result"] == {"answer": 42} and s["progress"] == "3/3"
    bad = jm.start("bad", lambda job: 1 / 0)
    assert jm.wait(bad.id, 5).state == "failed" and "ZeroDivisionError" in bad.summary()["error"]

    def slow(job):
        while not job.cancel.is_set():
            time.sleep(0.01)
        raise RuntimeError("stopped")

    c = jm.start("slow", slow)
    c.cancel.set()
    assert jm.wait(c.id, 5).state == "cancelled"
    assert run_or_queue("x", lambda job: 7, background=False) == 7


# ---------------------------------------------------------------- headless integration
FC = detect_freecadcmd()
needs_fc = pytest.mark.skipif(FC is None, reason="freecadcmd not available")

PROVIDER = textwrap.dedent('''
    import FreeCAD as App, Part
    V = App.Vector
    _S = {}
    def poses():
        return [0.0, 15.0, 40.0]
    def shapes(pose, gap=0.0):
        if not _S:
            _S["base"] = Part.makeBox(20, 20, 10)
            _S["slider"] = Part.makeBox(10, 10, 10)
        x = 0.0 if pose is None else float(pose)
        return [
            {"name": "Base", "shape": _S["base"], "placement": App.Placement(), "group": "ground"},
            {"name": "Slider", "shape": _S["slider"], "group": "moving",
             "placement": App.Placement(V(x, 5, 9.0 + gap), App.Rotation())},
        ]
''')


@pytest.fixture()
def cache_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FREECAD_MCP_CACHE", str(tmp_path / "cache"))
    return tmp_path


@needs_fc
def test_interference_provider_poses_cache_and_clearance(cache_env):
    prov = cache_env / "prov.py"
    prov.write_text(PROVIDER)
    # a file written milliseconds before the run can carry an mtime at/after prepare()'s start on Windows
    # (file-system vs time.time() clocks), which the "edited during evaluation" guard rightly rejects
    past = time.time() - 5
    os.utime(prov, (past, past))
    src = {"python": str(prov), "poses_function": "poses"}
    r = ic.interference_check(src, workers=2, command=FC)
    assert r["status"] == "clash", r
    ov = r["overlaps"][0]
    # slider dips 1 mm into the base: 10x10x1 at x=0 and x=15 (overlap 5x10x1), clear at x=40
    assert {ov["a"], ov["b"]} == {"Base", "Slider"}
    assert ov["volume_mm3"] == pytest.approx(100, abs=0.5) and ov["poses_hit"] == 2
    assert r["overlaps_per_pose"] == {"0.0": 1, "15.0": 1}
    assert r["summary"]["computed"] == 2  # x=40 pruned by the bbox prefilter
    again = ic.interference_check(src, workers=2, command=FC)
    assert again["summary"]["computed"] == 0 and again["summary"]["cache_hits"] == 2
    assert "reused" in str(again["timing_s"]["source_build"])  # provider unchanged -> no FreeCAD launch
    # gap 0.5 above the base: no overlap, but closer than the 1 mm clearance
    src_gap = dict(src, args={"gap": 1.5})
    r = ic.interference_check(src_gap, clearance=1.0, workers=1, command=FC)
    assert r["status"] == "clear"
    assert r["clearance_violations"][0]["distance_mm"] == pytest.approx(0.5, abs=1e-3)


@needs_fc
def test_interference_fcstd_source(cache_env):
    from freecad_mcp.headless import run_headless

    doc = cache_env / "two_boxes.FCStd"
    code = textwrap.dedent(f'''
        import FreeCAD as App, Part
        d = App.newDocument("T")
        a = d.addObject("Part::Box", "BoxA")
        b = d.addObject("Part::Box", "BoxB")
        b.Placement.Base = App.Vector(9, 0, 0)
        stock = d.addObject("Part::Box", "Stock")
        stock.Placement.Base = App.Vector(50, 0, 0)
        drill = d.addObject("Part::Cylinder", "Drill")
        drill.Radius, drill.Placement.Base = 2, App.Vector(55, 5, 0)
        cut = d.addObject("Part::Cut", "CutResult")
        cut.Base, cut.Tool = stock, drill  # consumed inputs must not be reported separately
        d.recompute()
        d.saveAs({str(doc)!r})
    ''')
    assert run_headless(code, 120, FC)["success"]
    r = ic.interference_check({"fcstd": str(doc)}, workers=1, command=FC)
    names = {(o["a"], o["b"]) for o in r["overlaps"]}
    assert names == {("BoxA", "BoxB")}, r
    assert r["overlaps"][0]["volume_mm3"] == pytest.approx(100, abs=0.1)
    assert r["summary"]["pairs_considered"] == 3   # BoxA, BoxB, CutResult (Stock/Drill consumed by the cut)
    assert r["summary"]["shapes"] == 2             # identical boxes share one fingerprint / BREP


HOLE_PART = textwrap.dedent('''
    import FreeCAD as App, Part
    V = App.Vector
    def shapes(pose):
        plate = Part.makeBox(60, 30, 4)
        good = Part.makeCylinder(2, 4, V(15, 15, 0))         # wall 13 mm
        thin = Part.makeCylinder(2, 4, V(35, 2.5, 0))        # wall 0.5 mm -> FAIL
        warn = Part.makeCylinder(2, 4, V(50, 26.8, 0))       # wall 1.2 mm -> FAIL? no: 30-26.8-2 = 1.2 -> WARN
        broken = Part.makeCylinder(2, 4, V(59, 15, 0))       # breaks out through x = 60
        s = plate.cut(good).cut(thin).cut(warn).cut(broken)
        slot = Part.makeBox(10, 4, 4, V(20, 24, 0)).fuse(Part.makeCylinder(2, 4, V(20, 26, 0)))
        s = s.cut(slot)                                       # tangent slot end: intended, ignored
        return [{"name": "Plate", "shape": s}]
''')


@needs_fc
def test_hole_edge_check_statuses_and_cache(cache_env):
    from freecad_mcp.holes import hole_edge_check

    prov = cache_env / "holes_prov.py"
    prov.write_text(HOLE_PART)
    r = hole_edge_check({"python": str(prov)}, workers=1, command=FC)
    st = sorted((row["status"], tuple(row["at"][:2])) for row in r["rows"])
    assert ("BROKEN", (59.0, 15.0)) in st
    assert ("FAIL", (35.0, 2.5)) in st
    assert ("WARN", (50.0, 26.8)) in st
    assert not any(s == "BROKEN" and xy[0] == 20.0 for s, xy in st), st   # slot end ignored
    assert r["status"] == "fail" and r["summary"]["full_holes"] == 3
    again = hole_edge_check({"python": str(prov)}, workers=1, command=FC)
    assert again["summary"]["computed"] == 0 and again["rows"] == r["rows"]
    loose = hole_edge_check({"python": str(prov)}, fail_wall=0.4, workers=1, command=FC)
    assert loose["summary"]["computed"] == 1          # thresholds are part of the cache key
    assert not any(row["status"] == "FAIL" for row in loose["rows"])


@needs_fc
def test_fingerprint_sees_a_moved_hole():
    """Regression: volume/area/counts/bbox are identical when a hole moves; the fingerprint must differ."""
    from pathlib import Path

    from freecad_mcp.headless import run_headless

    workers = Path(ic.__file__).with_name("fcworkers")
    code = textwrap.dedent(f'''
        import sys
        sys.path.insert(0, {str(workers)!r})
        import FreeCAD as App, Part
        import fcmcp_common as C
        V = App.Vector
        plate = Part.makeBox(40, 20, 5)
        a = plate.cut(Part.makeCylinder(2, 5, V(10, 10, 0)))
        b = plate.cut(Part.makeCylinder(2, 5, V(25, 10, 0)))       # same hole, moved 15 mm
        c = plate.cut(Part.makeCylinder(2, 5, V(10, 10, 0)))       # rebuilt identical part
        same = abs(a.Volume - b.Volume) < 1e-6 and abs(a.Area - b.Area) < 1e-6 and \
            len(a.Faces) == len(b.Faces) and a.BoundBox.isInside(b.BoundBox.Center)
        print("RESULT", same, C.fingerprint(a) != C.fingerprint(b), C.fingerprint(a) == C.fingerprint(c),
              C.FINGERPRINT_VERSION)
    ''')
    res = run_headless(code, 120, FC)
    line = [ln for ln in res.get("output", "").splitlines() if ln.startswith("RESULT")]
    assert line, res
    assert line[0].split()[1:] == ["True", "True", "True", "g2"]
