"""Pure ASME Y14.5 / ISO 286 drawing rules (guiside/asme_rules.py) - no FreeCAD needed."""

import importlib.util
from pathlib import Path

import pytest

GUISIDE = Path(__file__).parents[1] / "src" / "freecad_mcp" / "guiside"


def _load(name):
    spec = importlib.util.spec_from_file_location("fcmcp_" + name, GUISIDE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load("asme_rules")


@pytest.mark.parametrize("v, nd, txt", [
    (38.0, 2, "38"),            # Y14.5 4.3.1(b): whole millimetres without decimal point or zero
    (38.5, 2, "38.5"),          # 4.3.1(c): no trailing zero
    (0.25, 2, "0.25"),          # leading zero kept below 1 mm
    (0.10, 2, "0.1"),
    (12.345, 2, "12.35"),
    (-0.001, 2, "0"),           # no "-0"
    (0.0, 3, "0"),
    (80, 0, "80"),              # regression: integers keep their zeros (was "8")
    (100.0, 1, "100"),
    (120.50, 1, "120.5"),
])
def test_metric_value_format(v, nd, txt):
    assert R.mm(v, nd) == txt


def test_dim_format_strips_trailing_zeros():
    assert R.DIM_FORMAT == "%.2w"


@pytest.mark.parametrize("d, internal, txt, limits", [
    (15, True, "H7", (0.018, 0.0)),        # 10-18 mm: IT7 = 18 um
    (20, True, "H7", (0.021, 0.0)),        # published worked example 20H7 +21 um
    (40, True, "H7", (0.025, 0.0)),        # 40H7 +25 um
    (10, False, "k6", (0.010, 0.001)),
    (12, False, "k6", (0.012, 0.001)),
    (25, False, "k6", (0.015, 0.002)),
])
def test_iso286_fits(d, internal, txt, limits):
    # Y14.5 5.2.1: size + tolerance symbol only, no inline (+a/b)
    assert R.fit_text(d, internal) == txt
    assert R.fit_limits(d, internal) == pytest.approx(limits)


def test_fit_outside_table_is_none():
    assert R.fit_text(2.5, True) is None and R.fit_text(60, False) is None
    assert R.fit_text(3, True) is None          # bands are (lo, hi]: 3 mm is below the first band


def test_bearing_and_dowel_detection():
    assert R.bearing_seat(15.0, True) and R.bearing_seat(37.01, True)      # 6700 / 6805 outer rings
    assert R.bearing_seat(10.0, False) and not R.bearing_seat(10.5, False)
    assert not R.bearing_seat(14.9, True)
    assert R.dowel_hole(3.0) and R.dowel_hole(4.01) and not R.dowel_hole(3.3)


def test_clearance_hole_to_screw():
    assert R.screw_for_clearance(3.4) == 3 and R.screw_for_clearance(4.5) == 4
    assert R.screw_for_clearance(5.5) == 5 and R.screw_for_clearance(7.0) is None


def test_floating_fastener_position_tol():
    # Y14.5 App. B: floating fastener T = H - F at MMC
    assert R.position_tol(3.4, "machined") == ("0.4", True)
    assert R.position_tol(4.5, "machined") == ("0.5", True)
    # not a clearance-hole size: floor value, RFS
    assert R.position_tol(7.0, "machined", floor=0.05) == ("0.05", False)
    assert R.position_tol(7.0, "machined") == ("0.1", False)


def test_fixed_fastener_position_tol():
    # fixed fastener T = (H - F) / 2 (App. B-4, with a projected tolerance zone)
    assert R.position_tol(2.5, "machined", tapped=True, mate_clear=3.3) == ("0.15", True)
    assert R.position_tol(3.3, "machined", tapped=True, mate_clear=4.5) == ("0.25", True)
    assert R.position_tol(2.5, "machined", tapped=True) == ("0.15", True)      # default M3 mate
    assert R.PROJ == "Ⓟ" and R.PROJ_H_DEFAULT == 6.0


def test_projected_heights():
    R.set_proj_heights({"Plate|M3": 4, "M4": 5.5})
    try:
        assert R.proj_height("Plate", "M3") == 4.0
        assert R.proj_height("Other", "M4") == 5.5       # thread-only fallback
        assert R.proj_height("Other", "M3") is None      # caller warns + uses PROJ_H_DEFAULT
    finally:
        R.set_proj_heights({})
    assert R.proj_height("Plate", "M3") is None


def test_datum_reference_frame():
    # a pattern of >= 2 holes as datum B already clocks the part: no datum C
    assert R.drf(4) == ["A", "B"]
    assert R.drf(1) == ["A", "B", "C"]
    assert R.drf(0) == ["A", "B", "C"]


@pytest.mark.parametrize("text, issues", [
    ("4X ⌀4.5 THRU", 0),
    ("⌀3.30 THRU", 1),            # trailing zero
    ("↧.5", 1),                   # missing leading zero
    ("LINEAR ±0.10", 1),
    ("M3x0.5-6H ↧5.5", 0),
    ("SCALE 1:10", 0),
])
def test_lint_text(text, issues):
    assert len(R.lint_text(text)) == issues


def test_rules_module_has_no_freecad_import():
    src = (GUISIDE / "asme_rules.py").read_text(encoding="utf-8")
    assert "import FreeCAD" not in src and "import FreeCADGui" not in src


# ---------------------------------------------------------------- layout geometry helpers (pure parts)
def _layout():
    import sys
    import types

    saved = {k: sys.modules.get(k) for k in ("FreeCAD", "FreeCADGui", "Part")}
    for k in saved:
        sys.modules[k] = types.ModuleType(k)
    try:
        return _load("layout")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_layout_segment_rules():
    L = _layout()
    assert L._seg_x((0, 0), (10, 10), (0, 10), (10, 0))            # proper crossing
    assert not L._seg_x((0, 0), (10, 0), (10, 0), (20, 5))        # touching endpoints do not count
    assert L._conflict((0, 0), (10, 0), (0, 0.5), (10, 0.5))      # parallel closer than 0.8 mm
    assert not L._conflict((0, 0), (10, 0), (0, 2), (10, 2))
    assert not L._conflict((0, 0), (10, 10), (0, 0), (10, -10))   # same anchor: not a conflict
    assert L._seg_box((-5, 5), (15, 5), (0, 0, 10, 10))           # leader through a label box
    assert not L._seg_box((-5, 20), (15, 20), (0, 0, 10, 10))
    assert L._inter((0, 0, 10, 10), (9, 9, 20, 20)) and not L._inter((0, 0, 10, 10), (11, 0, 20, 10))


def test_layout_configure_scales_ansi_b_boxes():
    L = _layout()
    f, t = L.configure(431.8, 279.4)
    assert f == pytest.approx((21.0, 21.0, 410.0, 258.0)) and t[0] == pytest.approx((194.0, 19.0, 412.0, 70.5))
    f, t = L.configure(863.6, 279.4)
    assert f[2] == pytest.approx(820.0) and t[0][0] == pytest.approx(388.0)
    f, t = L.configure(420, 297, frame=(10, 10, 410, 287), keepout=[(230, 10, 410, 60)])
    assert f == (10, 10, 410, 287) and t == [(230, 10, 410, 60)]


def test_leader_rank_penalises_running_over_views():
    L = _layout()
    assert L._len_in_box((0, 5), (20, 5), (5, 0, 15, 10)) == pytest.approx(10)
    assert L._len_in_box((0, 20), (20, 20), (5, 0, 15, 10)) == 0
    assert L._len_in_box((6, 5), (8, 5), (5, 0, 15, 10)) == pytest.approx(2)
    L.VIEW_BOXES[:] = [(0, 0, 100, 50)]
    try:
        near = ((10, 45), (20, 70))          # short hop out of the view
        far = ((90, 5), (20, 70))            # across the whole view
        assert L._rank(near) < L._rank(far)
        assert L._rank(((0, 60), (10, 60))) == pytest.approx(10)   # clear of the view: just its length
    finally:
        L.VIEW_BOXES[:] = []
