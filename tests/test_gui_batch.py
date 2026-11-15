"""gui_batch: slicing, RAM cap, skip cache (pure); real private FreeCAD GUI instances when opted in
(FCMCP_GUI_TESTS=1 - they open windows and take ~30 s)."""

import os
import textwrap

import pytest

from freecad_mcp import gui_batch as GB


def test_items_and_slices():
    its = GB.normalise_items([{"id": "a", "args": 1}, 5, {"args": {"x": 1}}])
    assert [i["id"] for i in its] == ["a", "1", "2"] and its[1]["args"] == 5
    with pytest.raises(ValueError):
        GB.normalise_items([{"id": "a", "args": 1}, {"id": "a", "args": 2}])
    sl = GB.split_slices(GB.normalise_items(list(range(7))), 3)
    assert [len(s) for s in sl] == [3, 2, 2] and sl[0][1]["args"] == 3
    assert len(GB.split_slices(GB.normalise_items([1]), 4)) == 1


def test_instance_cap_by_free_ram():
    gb = GB.GB
    assert GB.instance_cap(3, free=int(10 * gb)) == 3          # (10 - 2) // 1.5 = 5
    assert GB.instance_cap(3, free=int(5 * gb)) == 2
    assert GB.instance_cap(3, free=int(1 * gb)) == 1           # always at least one
    assert GB.instance_cap(8, per_instance_gb=1.0, free=int(6 * gb)) == 4


def test_skip_cache(tmp_path):
    out = tmp_path / "a.pdf"
    out.write_text("x")
    c = GB.SkipCache("t", root=tmp_path)
    it = {"id": "a", "fingerprint": "f1", "outputs": [str(out)]}
    assert not c.unchanged(it)
    c.record(it)
    c.save()
    c2 = GB.SkipCache("t", root=tmp_path)
    assert c2.unchanged(it)
    assert not c2.unchanged(dict(it, fingerprint="f2"))
    assert not c2.unchanged(dict(it, outputs=[str(tmp_path / "missing.pdf")]))
    assert not c2.unchanged({"id": "a"})                       # no fingerprint: always run


@pytest.mark.skipif(os.environ.get("FCMCP_GUI_TESTS") != "1", reason="launches FreeCAD GUI windows (FCMCP_GUI_TESTS=1)")
def test_two_private_gui_instances(tmp_path, monkeypatch):
    monkeypatch.setenv("FREECAD_MCP_CACHE", str(tmp_path / "cache"))
    script = tmp_path / "job.py"
    script.write_text(textwrap.dedent('''
        import os, time, FreeCAD as App, FreeCADGui as Gui, Part
        def make(args):
            if args.get("sleep"):
                time.sleep(args["sleep"])
            d = App.newDocument("B")
            f = d.addObject("Part::Feature", "Box")
            f.Shape = Part.makeBox(args["L"], 10, 5)
            d.recompute()
            d.saveAs(args["out"])
            return {"volume": round(f.Shape.Volume, 3), "gui": Gui.getMainWindow() is not None, "pid": os.getpid(),
                    "home": App.getUserAppDataDir()}
    '''), encoding="utf-8")
    items = [{"id": "p%d" % i, "args": {"L": 10 * (i + 1), "out": str(tmp_path / ("p%d.FCStd" % i))},
              "fingerprint": "v1-%d" % i, "outputs": [str(tmp_path / ("p%d.FCStd" % i))]} for i in range(2)]
    items.append({"id": "slow", "args": {"L": 1, "sleep": 60, "out": str(tmp_path / "slow.FCStd")}})
    expected = GB.instance_cap(2, 0.75)                 # the RAM cap may allow only one on a busy machine
    r = GB.run_batch([str(script)], "make", items, instances=2, item_timeout=25, name="t", per_instance_gb=0.75)
    by = {x["id"]: x for x in r["items"]}
    assert r["instances"] in (expected, max(1, expected - 1)), r
    assert by["p0"]["status"] == "ok" and by["p0"]["result"]["volume"] == 500 and by["p0"]["result"]["gui"]
    assert by["p1"]["status"] == "ok" and by["p1"]["result"]["volume"] == 1000
    if r["instances"] == 2:
        assert by["p0"]["result"]["pid"] != by["p1"]["result"]["pid"]      # two processes
    assert "FreeCAD" not in by["p0"]["result"]["home"] or "home" in by["p0"]["result"]["home"]   # private profile
    assert by["slow"]["status"] == "timeout"
    r2 = GB.run_batch([str(script)], "make", items[:2], instances=2, item_timeout=25, name="t")
    assert r2["counts"] == {"skipped": 2} and r2["instances"] == 0


def test_user_cfg_lookup_prefers_the_newest_versioned_profile(tmp_path, monkeypatch):
    root = tmp_path / "FreeCAD"
    for d in ("v1-0", "v1-1"):
        (root / d).mkdir(parents=True)
        (root / d / "user.cfg").write_text(d)
    (root / "user.cfg").write_text("flat")
    monkeypatch.setattr(GB.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert GB.user_cfg_path().read_text() == "v1-1"
    for d in ("v1-0", "v1-1"):
        (root / d / "user.cfg").unlink()
    assert GB.user_cfg_path().read_text() == "flat"
