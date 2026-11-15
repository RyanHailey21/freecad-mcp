# -*- coding: utf-8 -*-
"""
ASME drawing rules used by the drawing generator, encoded as plain functions (no FreeCAD import),
so a generator, a linter or an MCP tool can share them.  Ported from the light_weight_arm
project (cad/drawings/asme_rules.py, synced 2026-11-15); the projected-height source is passed in (set_proj_heights).

References - VERIFIED against ASME Y14.5-2018 (licensed copy supplied by the user, 2026-11-14):
  Y14.5 4.3.1(b),(c)  millimetre values: whole numbers without decimal point or zero; no trailing zero
                      after the last significant decimal digit (bilateral tolerances: 5.3.1 differs).
  Y14.5 4.4.4         leaders: inclined straight line + short horizontal shoulder to the text mid-height;
                      adjacent leaders parallel; arrowhead on a hole edge, dot inside a surface outline.
  Y14.5 4.4.4.2       leader to a circle or arc is radial.
  Y14.5 4.4.5.x       notes, dimensions, FCFs, datum symbols read from the bottom of the sheet.
  Y14.5 App. B        floating fastener T = H - F; fixed fastener T = (H - F)/2 is the case WHEN A
                      PROJECTED TOLERANCE ZONE IS USED (B-4) -> tapped holes carry (P) + height (10.4.1).
  Y14.5 4.1(b),(c)    each necessary dimension shown OR DEFINED BY MODEL DATA; model data shall be queried when
                      dimensions are not displayed.  5.6: in an undimensioned model, basic values are queried
                      relative to the datums referenced in the feature control frames.  -> drawings cite Y14.5 only.
  Sheet format: FreeCAD's bundled ASME ANSI-B template (prebuilt); lettering 3.5 mm.  Y14.100 / Y14.2 / Y14.41 are
  NOT claimed (no copies available to verify).
  ISO 286     H7 / k6 deviations (3-50 mm) cross-checked 2026-11-15 against the ISO 286-1 formulas (i, IT6, IT7,
              k fundamental deviation) and published worked examples (20H7 +21 um, 40H7 +25 um).
Not relied upon: Y14.41-2019, Y14.6, Y14.100, Y14.2 (not claimed on the drawings).
"""
import math

# ---------------------------------------------------------------- Y14.5 1.6.1 metric values
def mm(v, nd=2):
    """Metric dimension text: 38 / 38.5 / 0.25 (no trailing zeros, leading zero kept)."""
    s = ("%." + str(nd) + "f") % v
    if "." in s:                                # integers keep their zeros (80, not 8)
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


DIM_FORMAT = "%.2w"            # TechDraw FormatSpec: 2 decimals, trailing zeros stripped

# ---------------------------------------------------------------- ISO 286 fits (um), nominal ranges in mm
_H7 = [(3, 6, 12, 0), (6, 10, 15, 0), (10, 18, 18, 0), (18, 30, 21, 0), (30, 50, 25, 0)]
_K6 = [(3, 6, 9, 1), (6, 10, 10, 1), (10, 18, 12, 1), (18, 30, 15, 2), (30, 50, 18, 2)]


def _band(tbl, d):
    for lo, hi, es, ei in tbl:
        if lo < d <= hi:
            return es / 1000.0, ei / 1000.0
    return None


def fit_text(d, internal):
    """Bearing-seat fit: housing bore H7, shaft k6 (rotating inner ring, light load)."""
    b = _band(_H7 if internal else _K6, d)
    if not b:
        return None
    up, lo = b
    cls = "H7" if internal else "k6"
    return cls                                   # Y14.5 5.2.1: basic size + tolerance symbol (no inline +a/b)


def fit_limits(d, internal):
    """ISO 286 deviations (mm) behind fit_text, for a limits table or a note: (upper, lower) or None."""
    return _band(_H7 if internal else _K6, d)


# rolling bearings used in the arm (d x D)
BEARINGS = {"6700ZZ": (10, 15), "6701ZZ": (12, 18), "6805ZZ": (25, 37), "688ZZ": (8, 16),
            "MR104ZZ": (4, 10), "MR105ZZ": (5, 10)}
BEARING_OD = sorted({D for _d, D in BEARINGS.values()})
BEARING_ID = sorted({d for d, _D in BEARINGS.values()})


def bearing_seat(d, internal, tol=0.02):
    """True if a cylinder of diameter d is a bearing housing bore (internal) or journal (external)."""
    ref = BEARING_OD if internal else BEARING_ID
    return any(abs(d - r) <= tol for r in ref)


# ---------------------------------------------------------------- position tolerances
CLEAR = {3: (3.2, 3.4), 4: (4.3, 4.5), 5: (5.3, 5.5)}       # ISO 273 fine / medium clearance holes


def screw_for_clearance(d):
    for m, (fine, med) in CLEAR.items():
        if fine - 0.05 <= d <= med + 0.15:
            return m
    return None


PROJ = "Ⓟ"                 # projected tolerance zone modifier
PROJ_H_DEFAULT = 6.0            # projected height = max mating-part thickness (6 mm: thickest clamped
                                # member on this arm is the 5 mm base plate + washer); override per part


def position_tol(d, kind, tapped=False, mate_clear=None, floor=None):
    """Positional tolerance at MMC for a fastener hole.
    clearance hole (floating fastener): T = H - F;  tapped hole (fixed fastener): T = (H' - F) / 2
    where H' is the mating clearance hole.  Returns (value_text, mmc_bool)."""
    if tapped:
        m = mate_clear or 3.3
        t = (m - math.floor(m)) / 2.0
        return mm(t), True
    m = screw_for_clearance(d)
    if m is None:
        return mm(floor or 0.1), False
    t = d - m
    return mm(t), True


# ---------------------------------------------------------------- datum reference frame
def drf(pattern_size_B):
    """A = primary plane; B = first hole group.  A pattern of >= 2 holes constrains rotation about A,
    so C would add nothing and is not referenced.  Returns the datum letters for location FCFs."""
    return ["A", "B"] if pattern_size_B >= 2 else ["A", "B", "C"]


# ---------------------------------------------------------------- lint (text rules)
def lint_text(s):
    """Y14.5 metric value lint on an annotation string -> list of issues."""
    import re
    out = []
    for m in re.finditer(r"(?<![\w.])(\d+\.\d*0)(?![\d])", s):
        out.append("trailing zero: %s" % m.group(1))
    for m in re.finditer(r"(?<![\d])(\.\d+)", s):
        out.append("missing leading zero: %s" % m.group(1))
    return out


DOWELS = (2.0, 3.0, 4.0, 5.0)


def dowel_hole(d, tol=0.02):
    """Dowel (ISO 8734 m6 pin) hole: H7 slip/transition fit."""
    return any(abs(d - x) <= tol for x in DOWELS)


_PROJ = {}


def set_proj_heights(heights):
    """Load measured projected tolerance zone heights: {"<part>|<thread>": mm} or {"<thread>": mm}.
    (Ported from the arm project, where they came from cad/fab/projected_heights.json.)"""
    _PROJ.clear()
    _PROJ.update({str(k): float(v) for k, v in (heights or {}).items()})


def proj_height(part, thread):
    """Projected tolerance zone height (mm) for `part`'s tapped `thread` holes: the thickest mating stack on the
    screw-head side, measured in the assembly.  None when not supplied (caller uses PROJ_H_DEFAULT and warns)."""
    v = _PROJ.get("%s|%s" % (part, thread))
    return v if v is not None else _PROJ.get(str(thread))
