# -*- coding: utf-8 -*-
"""Headless hole edge-distance / break-out check for BREP shapes.

A *hole* is a set of concave cylindrical faces sharing one axis and radius
(OCC splits a hole into half-faces). For holes ``min_d..max_d`` mm diameter:

BROKEN  not a full 360 deg AND it meets a neighbouring face at a sharp edge
        (it breaks out through the part edge, a pocket or a window). Partial
        cylinders blending tangentially into neighbours (slot ends, fillets)
        are intended and ignored.
FAIL    wall from a full hole to the nearest side-bounding face < fail_wall
WARN    wall < max(warn_wall, warn_ratio * d)

Side-bounding faces are faces not lying across the hole axis, so plate faces,
counterbore floors and the hole's own coaxial counterbore/chamfer are ignored.

Params: {"tasks": [[fp, brep_path], ...], "opts": {...}, "out", "progress"}
Result: {"results": {fp: {"holes": n, "rows": [[status, d, [x,y,z], value], ...]}}}
"""
import math
import time
import traceback

import FreeCAD as App
import Part

import fcmcp_common as C

P = C.load_params()
O = dict(fail_wall=1.0, warn_wall=1.5, warn_ratio=0.5, min_d=2.0, max_d=12.0, tangent_deg=3.0,
         search_margin=8.0)
O.update(P.get("opts") or {})


def _mid_normal(face, pnt):
    u, v = face.Surface.parameter(pnt)
    return face.normalAt(u, v)


def hole_groups(shape):
    groups = {}
    for f in shape.Faces:
        s = f.Surface
        if s.__class__.__name__ != "Cylinder":
            continue
        d = 2 * s.Radius
        if not (O["min_d"] <= d <= O["max_d"]):
            continue
        u0, u1, v0, v1 = f.ParameterRange
        p = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
        n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        ax, c = App.Vector(s.Axis), App.Vector(s.Center)
        radial = (p - c) - ax * ((p - c).dot(ax))
        if radial.Length < 1e-9 or n.dot(radial) > 0:        # convex: boss / shaft, not a hole
            continue
        if ax.x < -1e-9 or (abs(ax.x) < 1e-9 and (ax.y < -1e-9 or (abs(ax.y) < 1e-9 and ax.z < 0))):
            ax = ax * -1
        foot = c - ax * c.dot(ax)
        key = tuple(round(x, 2) for x in (ax.x, ax.y, ax.z, foot.x, foot.y, foot.z, s.Radius))
        g = groups.setdefault(key, dict(faces=[], centre=c, axis=ax, r=s.Radius, span=0.0))
        g["faces"].append(f)
        g["span"] += math.degrees(u1 - u0)
    return groups


def breaks_out(shape, g):
    ids = [f.hashCode() for f in g["faces"]]
    for f in g["faces"]:
        for e in f.Edges:
            if e.Curve.__class__.__name__ not in ("Line", "LineSegment"):
                continue
            pm = e.valueAt((e.FirstParameter + e.LastParameter) / 2)
            for nb in shape.ancestorsOfType(e, Part.Face):
                if nb.hashCode() in ids:
                    continue
                try:
                    a = _mid_normal(f, pm).getAngle(_mid_normal(nb, pm))
                except Exception:
                    continue
                if math.degrees(a) > O["tangent_deg"]:
                    return True
    return False


def ligament(shape, g):
    ax, c, r = g["axis"], g["centre"], g["r"]
    ids = {f.hashCode() for f in g["faces"]}
    own_edges = {e.hashCode() for f in g["faces"] for e in f.Edges}
    hole = Part.Shell(g["faces"]) if len(g["faces"]) > 1 else g["faces"][0]
    bb = hole.BoundBox
    bb.enlarge(r + O["search_margin"])
    best = 1e9
    for h in shape.Faces:
        if h.hashCode() in ids or any(e.hashCode() in own_edges for e in h.Edges):
            continue
        if not h.BoundBox.intersect(bb):
            continue
        sn = h.Surface.__class__.__name__
        if sn == "Plane":
            try:
                n = h.normalAt(*h.ParameterRange[0::2])
            except Exception:
                continue
            if abs(n.dot(ax)) > 0.5:
                continue
        elif sn == "Cylinder" and abs(App.Vector(h.Surface.Axis).dot(ax)) > 0.99:
            d = (h.Surface.Center - c) - ax * ((h.Surface.Center - c).dot(ax))
            if d.Length < 1e-3:
                continue
        try:
            best = min(best, hole.distToShape(h)[0])
        except Exception:
            pass
    return best


def check(shape):
    rows, nh = [], 0
    for g in hole_groups(shape).values():
        c, d = g["centre"], 2 * g["r"]
        where = [round(c.x, 2), round(c.y, 2), round(c.z, 2)]
        if g["span"] < 359.0:
            if breaks_out(shape, g):
                rows.append(["BROKEN", round(d, 3), where, round(g["span"], 1)])
            continue
        nh += 1
        lig = ligament(shape, g)
        if lig < O["fail_wall"]:
            rows.append(["FAIL", round(d, 3), where, round(lig, 3)])
        elif lig < max(O["warn_wall"], O["warn_ratio"] * d):
            rows.append(["WARN", round(d, 3), where, round(lig, 3)])
    return {"holes": nh, "rows": rows}


t0 = time.time()
res, errors = {}, []
tasks = P["tasks"]
prog = C.Progress(P.get("progress"), len(tasks))
for i, (fp, path) in enumerate(tasks):
    try:
        res[fp] = check(C.load_brep(path))
    except Exception as exc:
        errors.append("%s: %s" % (fp, exc))
        res[fp] = {"holes": 0, "rows": [["ERROR", 0, [0, 0, 0], str(exc)[:120]]]}
    prog(i + 1)
C.write_json(P["out"], {"results": res, "errors": errors[:20], "elapsed": round(time.time() - t0, 3)})
