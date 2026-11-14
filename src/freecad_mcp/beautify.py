"""Automated "beautify" pass for a FreeCAD document.

Rounds sharp external edges, colours parts by their role, ghosts reference geometry, smooths curve
display and frames an isometric view. Everything runs inside FreeCAD as one script, so a model is
beautified in a single round trip. The pass is conservative about geometry: it only fillets
convex straight edges between flat faces, tries smaller radii when a fillet fails, and keeps a result
only if it is a valid single solid whose volume changed by less than ``max_volume_change_pct``.
Circular edges (bores, pins) and protected objects are never touched, so mating features survive.
"""

import json
from typing import Any

from .freecad_client import FreeCADConnection
from .responses import ToolResponse, add_screenshot_if_available, text_response

PALETTE_NAMES = ("workshop", "carbon", "clean")
BEGIN, END = "BEAUTIFY_JSON_BEGIN", "BEAUTIFY_JSON_END"
DEFAULT_PROTECT = ("Ref_*",)
MAX_PROTECT = 50

_SCRIPT = r'''
import fnmatch
import re
import FreeCAD as App
import FreeCADGui as Gui
import Part

P = PARAMS
PALETTES = {
    "workshop": {"structure": (0.20, 0.22, 0.25), "accent": (0.93, 0.45, 0.10), "grip": (0.07, 0.07, 0.08),
                 "metal": (0.78, 0.80, 0.83), "spring": (0.82, 0.62, 0.20), "drive": (0.30, 0.33, 0.38),
                 "default": (0.55, 0.58, 0.62)},
    "carbon": {"structure": (0.12, 0.13, 0.15), "accent": (0.10, 0.65, 0.80), "grip": (0.05, 0.05, 0.06),
               "metal": (0.70, 0.72, 0.75), "spring": (0.85, 0.70, 0.30), "drive": (0.22, 0.24, 0.28),
               "default": (0.40, 0.42, 0.46)},
    "clean": {"structure": (0.88, 0.89, 0.90), "accent": (0.20, 0.45, 0.85), "grip": (0.15, 0.15, 0.17),
              "metal": (0.72, 0.74, 0.78), "spring": (0.90, 0.72, 0.28), "drive": (0.55, 0.58, 0.62),
              "default": (0.75, 0.77, 0.80)},
}
ROLE_RULES = [
    ("spring", r"spring|coil"),
    ("grip", r"pad|tpu|rubber|gasket|seal"),
    ("drive", r"pinion|gear|motor|servo|actuator|belt|pulley"),
    ("metal", r"rail|shaft|rod|pin(?![a-z])|screw|bolt|axle|bearing|washer"),
    ("accent", r"jaw|finger|claw|slider|carriage|tooth|gripper"),
    ("structure", r"mount|plate|base|post|block|flange|bracket|frame|housing|body|chassis|cover"),
]
SHININESS = {"metal": 0.85, "spring": 0.7, "grip": 0.1, "structure": 0.35, "accent": 0.45, "drive": 0.5}


def darker(c, k=0.45):
    return tuple(max(0.0, x * k) for x in c)


def matches(obj, patterns):
    names = (obj.Name.lower(), (obj.Label or "").lower())
    return any(fnmatch.fnmatch(n, p.lower()) for p in patterns for n in names)


def role_of(obj):
    text = (obj.Name + " " + (obj.Label or "")).lower()
    for role, rx in ROLE_RULES:
        if re.search(rx, text):
            return role
    return "default"


def reference_style(obj):
    text = (obj.Name + " " + (obj.Label or "")).lower()
    if re.search(r"donut|ring|torus", text):
        return (0.97, 0.78, 0.22), 0
    if re.search(r"platform|floor|ground|table|tier|wall", text):
        return (0.62, 0.66, 0.72), 82
    if re.search(r"pole|post|pillar", text):
        return (0.80, 0.30, 0.30), 0
    return (0.70, 0.72, 0.76), 70


def convex_straight_edges(shape, min_len):
    out = []
    for e in shape.Edges:
        if e.Length < min_len or e.Curve.TypeId != "Part::GeomLine":
            continue
        faces = shape.ancestorsOfType(e, Part.Face)
        if len(faces) != 2 or any(f.Surface.TypeId != "Part::GeomPlane" for f in faces):
            continue
        mid = e.valueAt(0.5 * (e.FirstParameter + e.LastParameter))
        ns = []
        for f in faces:
            u, v = f.Surface.parameter(mid)
            ns.append(f.normalAt(u, v))
        if ns[0].dot(ns[1]) > 0.999:
            continue
        b = ns[0] + ns[1]
        if b.Length < 1e-6:
            continue
        b.normalize()
        if shape.isInside(mid + b * 0.02, 1e-7, True):
            continue          # concave edge: leave alone
        out.append(e)
    return out


def try_fillet(shape, edges, radius, max_dv):
    for k in (1.0, 0.7, 0.5, 0.3):
        try:
            new = shape.makeFillet(radius * k, edges)
        except Exception:
            continue
        try:
            ok = new.isValid() and len(new.Solids) == 1 and new.Volume > 0
            dv = abs(new.Volume - shape.Volume) / shape.Volume * 100.0
        except Exception:
            continue
        if ok and dv <= max_dv:
            return new, radius * k, dv
    return None, 0.0, 0.0


def fillet_shape(shape, radius, min_len, max_dv):
    if shape.ShapeType not in ("Solid", "Compound") or len(shape.Solids) != 1:
        return None, {"skipped": "not a single solid"}
    solid = shape.Solids[0]
    edges = convex_straight_edges(solid, min_len)
    if not edges:
        return None, {"skipped": "no eligible edges"}
    new, used, dv = try_fillet(solid, edges, radius, max_dv)
    if new is not None:
        return new, {"edges_filleted": len(edges), "edges_eligible": len(edges), "radius_mm": round(used, 3),
                     "volume_change_pct": round(dv, 3)}
    done, n_done, last = solid, 0, 0.0
    for axis in (App.Vector(0, 0, 1), App.Vector(1, 0, 0), App.Vector(0, 1, 0)):
        group = [e for e in convex_straight_edges(done, min_len)
                 if abs(e.tangentAt(e.FirstParameter).dot(axis)) > 0.999]
        if not group:
            continue
        cand, used, dv = try_fillet(done, group, radius, max_dv)
        if cand is not None:
            done, n_done, last = cand, n_done + len(group), used
    if n_done:
        dv = abs(done.Volume - solid.Volume) / solid.Volume * 100.0
        return done, {"edges_filleted": n_done, "edges_eligible": len(edges), "radius_mm": round(last, 3),
                      "volume_change_pct": round(dv, 3), "note": "filleted by edge direction"}
    return None, {"skipped": "fillet failed at every radius", "edges_eligible": len(edges)}


doc = App.getDocument(P["doc_name"])
palette = PALETTES[P["palette"]]
protect = list(P["protect"])
report = {"document": doc.Name, "palette": P["palette"], "dry_run": P["dry_run"], "objects": []}

for obj in doc.Objects:
    if not obj.isDerivedFrom("Part::Feature") or obj.Shape.isNull():
        continue
    entry = {"name": obj.Name}
    is_ref = matches(obj, protect)
    entry["role"] = "reference" if is_ref else role_of(obj)
    if not is_ref and P["fillet_radius"] > 0:
        if obj.TypeId != "Part::Feature":
            entry["geometry"] = {"skipped": "parametric feature (" + obj.TypeId + ")"}
        else:
            new, info = fillet_shape(obj.Shape, P["fillet_radius"], P["min_edge_length"], P["max_volume_change_pct"])
            entry["geometry"] = info
            if new is not None and not P["dry_run"]:
                obj.Shape = new
    report["objects"].append(entry)

if not P["dry_run"]:
    doc.recompute()
    for entry in report["objects"]:
        obj = doc.getObject(entry["name"])
        vo = getattr(obj, "ViewObject", None)
        if vo is None:
            continue
        try:
            if entry["role"] == "reference":
                if not P["style_references"]:
                    continue
                color, alpha = reference_style(obj)
                role = "default"
            else:
                role = entry["role"]
                color, alpha = palette.get(role, palette["default"]), 0
            vo.ShapeColor = color
            vo.LineColor = darker(color)
            vo.LineWidth = 1.0
            vo.Transparency = alpha
            if P["smooth_view"]:
                vo.Deviation = 0.05
                vo.AngularDeflection = 5.0
            try:
                vo.ShapeAppearance = (App.Material(DiffuseColor=color, SpecularColor=(0.8, 0.8, 0.8),
                                                   Shininess=SHININESS.get(role, 0.4), Transparency=alpha / 100.0),)
            except Exception:
                pass
        except Exception as exc:
            entry["appearance_error"] = str(exc)
    if P["apply_camera"]:
        try:
            Gui.setActiveDocument(doc.Name)
            Gui.activeDocument().activeView().viewIsometric()
            Gui.SendMsgToActiveView("ViewFit")
        except Exception as exc:
            report["camera_error"] = str(exc)

done = [o for o in report["objects"] if "edges_filleted" in o.get("geometry", {})]
report["summary"] = {"objects": len(report["objects"]), "filleted": len(done),
                     "skipped": sum(1 for o in report["objects"] if "skipped" in o.get("geometry", {}))}
print("@@BEGIN@@" + json.dumps(report) + "@@END@@")
'''.replace("@@BEGIN@@", BEGIN).replace("@@END@@", END)


def build_beautify_code(
    doc_name: str,
    fillet_radius: float = 1.0,
    palette: str = "workshop",
    protect: list[str] | None = None,
    min_edge_length: float = 4.0,
    max_volume_change_pct: float = 5.0,
    style_references: bool = True,
    smooth_view: bool = True,
    apply_camera: bool = True,
    dry_run: bool = False,
) -> str:
    """Validate the arguments and return the Python source to run inside FreeCAD."""
    if not isinstance(doc_name, str) or not doc_name.strip():
        raise ValueError("doc_name must be a non-empty string")
    if not isinstance(fillet_radius, (int, float)) or isinstance(fillet_radius, bool) or not 0 <= fillet_radius <= 10:
        raise ValueError("fillet_radius must be a number from 0 to 10 mm (0 skips geometry changes)")
    if palette not in PALETTE_NAMES:
        raise ValueError(f"palette must be one of {', '.join(PALETTE_NAMES)}")
    if not isinstance(min_edge_length, (int, float)) or isinstance(min_edge_length, bool) or min_edge_length <= 0:
        raise ValueError("min_edge_length must be a positive number of mm")
    if not isinstance(max_volume_change_pct, (int, float)) or isinstance(max_volume_change_pct, bool) \
            or not 0 < max_volume_change_pct <= 50:
        raise ValueError("max_volume_change_pct must be greater than 0 and at most 50")
    patterns = list(DEFAULT_PROTECT) if protect is None else list(protect)
    if len(patterns) > MAX_PROTECT or any(not isinstance(p, str) or not p or len(p) > 100 for p in patterns):
        raise ValueError(f"protect must be up to {MAX_PROTECT} non-empty name patterns (e.g. 'Pad_*')")
    params: dict[str, Any] = {
        "doc_name": doc_name, "fillet_radius": float(fillet_radius), "palette": palette, "protect": patterns,
        "min_edge_length": float(min_edge_length), "max_volume_change_pct": float(max_volume_change_pct),
        "style_references": bool(style_references), "smooth_view": bool(smooth_view),
        "apply_camera": bool(apply_camera), "dry_run": bool(dry_run),
    }
    return "import json\nPARAMS = json.loads(" + repr(json.dumps(params)) + ")\n" + _SCRIPT


def parse_report(message: str) -> dict[str, Any] | None:
    start, end = message.find(BEGIN), message.find(END)
    if start < 0 or end < start:
        return None
    return json.loads(message[start + len(BEGIN):end])


def beautify_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    doc_name: str,
    fillet_radius: float = 1.0,
    palette: str = "workshop",
    protect: list[str] | None = None,
    min_edge_length: float = 4.0,
    max_volume_change_pct: float = 5.0,
    style_references: bool = True,
    smooth_view: bool = True,
    apply_camera: bool = True,
    dry_run: bool = False,
    include_screenshot: bool = True,
) -> ToolResponse:
    try:
        code = build_beautify_code(
            doc_name, fillet_radius, palette, protect, min_edge_length, max_volume_change_pct,
            style_references, smooth_view, apply_camera, dry_run,
        )
    except ValueError as exc:
        return text_response(f"Invalid beautify arguments: {exc}")
    try:
        res = freecad.execute_code(code, 600)
        if not res["success"]:
            return text_response(f"Beautify failed: {res['error']}")
        report = parse_report(res["message"])
        if report is None:
            return text_response(f"Beautify ran but returned no report: {res['message']}")
        response = text_response(json.dumps(report, indent=2))
        skip = only_text_feedback or not include_screenshot or dry_run
        screenshot = None if skip else freecad.get_active_screenshot("Isometric")
        return add_screenshot_if_available(response, screenshot, skip)
    except Exception as exc:
        return text_response(f"Beautify failed: {exc}")
