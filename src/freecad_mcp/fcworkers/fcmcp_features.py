# -*- coding: utf-8 -*-
"""Feature recognition for manufacturing drawings - pure Part, no GUI, no TechDraw.

Runs headless (``freecadcmd``, unit-tested on fixture solids) and inside the GUI, where the
TechDraw library imports it as the module ``fcmcp_features``.  Ported from the light_weight_arm
drawing generator (cad/drawings/batch_drawings.py, 2026-11-15) after three independent reviews:

* hole sides - near/far side is decided by probing where a feature opens; counterbores and
  countersinks are probed in the RING between the hole and the outer diameter, never on the axis
  (that is open space inside a through hole);
* countersinks carry the included angle measured from the cone geometry; their side is probed just
  outside the big diameter (the arm version probed inside the cone, where both sides are void, so a
  far-side countersink read as near side);
* blind holes drilled from both faces are one EACH SIDE feature;
* side (non-axial) holes are split by their extent along the axis and record which end is open
  (``open_dir``), so FAR SIDE can be decided per view;
* a D-bore flat must be at least ``DFLAT_MIN`` deep, so seam slivers are not called D-bores;
* shaft features: E-clip grooves are counted along the shaft axis, drive flats are planes parallel
  to the shaft axis, hex drives (6 flats at 60 deg) are reported with their across-flats size.

Coordinates: the shape is expected oriented for drawing (``oriented()``: Z = main hole axis).
"""
import math

import FreeCAD as App

V = App.Vector
R_MIN, R_MAX = 0.9, 20.0      # hole radius range (mm)
DFLAT_MIN = 0.1               # D-bore flat depth below which a chord is a seam/slit artefact
GROOVE_R = (3.5,)             # groove bottom radii counted as E-clip grooves (DIN 6799 size 7 on D10)


def _cyl_concave(f):
    """(is_hole, centre, axis) for a cylindrical face: concave = the material is outside the radius."""
    srf = f.Surface
    u0, u1, v0, v1 = f.ParameterRange
    p = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
    n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
    ax, c = V(srf.Axis), V(srf.Center)
    radial = (p - c) - ax * ((p - c).dot(ax))
    if radial.Length < 1e-9:
        return None
    return n.dot(radial) < 0, c, ax


def hole_axis_counts(shape):
    cnt = [set(), set(), set()]
    for f in shape.Faces:
        srf = f.Surface
        if srf.__class__.__name__ != "Cylinder" or not (0.9 <= srf.Radius <= 12.5):
            continue
        cc = _cyl_concave(f)
        if not cc or not cc[0]:
            continue
        _h, c, ax = cc
        for i, comp in enumerate((ax.x, ax.y, ax.z)):
            if abs(abs(comp) - 1) < 1e-3:
                cnt[i].add((round(c.x, 1), round(c.y, 1), round(c.z, 1), round(srf.Radius, 2)))
    return [sum(k[3] for k in c) for c in cnt]      # weighted by radius: the main bore sets the view axis


def oriented(shape):
    """Copy re-axed for drawing: Z = the axis most holes run along (else the thinnest),
    X = longest remaining extent; centred in XY, Z from 0."""
    s = shape.copy()
    bb = s.BoundBox
    Ls = [bb.XLength, bb.YLength, bb.ZLength]
    hc = hole_axis_counts(s)
    zax = max(range(3), key=lambda i: (hc[i], -Ls[i])) if max(hc) > 0 else min(range(3), key=lambda i: Ls[i])
    rest = sorted([i for i in range(3) if i != zax], key=lambda i: -Ls[i])
    order = [rest[0], rest[1], zax]
    cols = [V(*[1.0 if k == i else 0.0 for k in range(3)]) for i in order]
    m = App.Matrix(cols[0].x, cols[0].y, cols[0].z, 0, cols[1].x, cols[1].y, cols[1].z, 0,
                   cols[2].x, cols[2].y, cols[2].z, 0, 0, 0, 0, 1)
    if m.determinant() < 0:                       # keep right-handed: flip the view axis
        m.A31, m.A32, m.A33 = -m.A31, -m.A32, -m.A33
    s.transformShape(m)
    c = s.BoundBox.Center
    s.translate(V(-c.x, -c.y, -s.BoundBox.ZMin))
    return s


def _opener(shape):
    def _open(x, y, z):
        try:
            return not shape.isInside(V(x, y, z), 1e-4, True)
        except Exception:
            return False
    return _open


def countersinks(shape):
    """Concave cones on a Z axis: {(x, y) rounded 0.1: (diameter, included angle deg, None, z of big end)}."""
    cones = {}
    for f in shape.Faces:
        srf = f.Surface
        if srf.__class__.__name__ != "Cone" or abs(abs(V(srf.Axis).z) - 1) > 1e-3:
            continue
        es = [e for e in f.Edges if e.Curve.__class__.__name__ == "Circle"]
        if not es:
            continue
        big = max(es, key=lambda e: e.Curve.Radius)
        sml = min(es, key=lambda e: e.Curve.Radius)
        c_ = big.Curve.Center
        hgt = abs(big.Curve.Center.z - sml.Curve.Center.z)
        if hgt < 1e-6 or big.Curve.Radius - sml.Curve.Radius < 1e-6:
            continue
        inc = 2 * math.degrees(math.atan((big.Curve.Radius - sml.Curve.Radius) / hgt))   # included angle
        cones[(round(c_.x, 1), round(c_.y, 1))] = (2 * big.Curve.Radius, inc, None, c_.z)
    return cones


def hole_table(shape, r_min=R_MIN, r_max=R_MAX, dflat_min=DFLAT_MIN):
    """Round holes from concave cylinders.

    Returns (axial, side, thickness).  Each hole dict: r, x, y, axial, z0, z1, depth, thru, ax, c, t0, t1,
    and for axial holes cbore (dia, depth) | None, cb_far, each_side, far, csk (dia, angle, far) | None,
    dflat (D-bore flat distance from the axis); side holes carry open_dir (Vector | None).
    """
    zmin, zmax = shape.BoundBox.ZMin, shape.BoundBox.ZMax
    holes = {}
    for f in shape.Faces:
        srf = f.Surface
        if srf.__class__.__name__ != "Cylinder" or not (r_min <= srf.Radius <= r_max):
            continue
        cc = _cyl_concave(f)
        if not cc or not cc[0]:
            continue
        _h, c, ax = cc
        u0, u1, _v0, _v1 = f.ParameterRange
        az = abs(ax.z) > 0.999
        bb = f.BoundBox
        if az:
            zr = (round(bb.ZMin, 1), round(bb.ZMax, 1))     # coaxial holes at different depths stay separate
        else:                                                # side holes: also split by extent ALONG the axis
            ts_ = [(vx.Point - c).dot(ax) for vx in f.Vertexes] or [0.0]
            zr = (round(c.z, 2), round(min(ts_), 0), round(max(ts_), 0))
        key = (round(c.x, 2), round(c.y, 2), zr, az, round(srf.Radius, 2))
        h = holes.setdefault(key, dict(r=srf.Radius, x=c.x, y=c.y, axial=az, z0=1e9, z1=-1e9, span=0.0,
                                        ax=ax, c=c, t0=1e9, t1=-1e9))
        h["z0"], h["z1"] = min(h["z0"], bb.ZMin), max(h["z1"], bb.ZMax)
        for vx in f.Vertexes:                       # extent ALONG the hole axis (side holes: not the z box)
            tt = (vx.Point - h["c"]).dot(h["ax"])
            h["t0"], h["t1"] = min(h["t0"], tt), max(h["t1"], tt)
        h["span"] += math.degrees(u1 - u0)
    full = []
    for h in holes.values():
        if h["span"] >= 359.0:
            full.append(h)
        elif h["span"] >= 270.0:                 # D-bore: chord flat at r*cos(half the missing arc)
            df = h["r"] * math.cos(math.radians((360.0 - h["span"]) / 2.0))
            if h["r"] - df > dflat_min:            # a real flat; a 0.0x chord is a slit/seam artefact
                h["dflat"] = df
            full.append(h)
    res = []
    for h in sorted(full, key=lambda h: h["r"]):
        if h["axial"]:
            h["thru"] = h["z0"] <= zmin + 0.01 and h["z1"] >= zmax - 0.01
            h["depth"] = h["z1"] - h["z0"]
        else:
            h["depth"] = h["t1"] - h["t0"]
            e0, e1 = h["c"] + h["ax"] * (h["t0"] - 0.3), h["c"] + h["ax"] * (h["t1"] + 0.3)
            try:
                o0, o1 = (not shape.isInside(e0, 1e-4, True)), (not shape.isInside(e1, 1e-4, True))
            except Exception:
                o0 = o1 = False
            h["thru"] = o0 and o1
            h["open_dir"] = (h["ax"] * -1.0) if (o0 and not o1) else (h["ax"] if (o1 and not o0) else None)
        res.append(h)
    by_xy = {}
    for h in res:
        if h["axial"]:
            by_xy.setdefault((round(h["x"], 1), round(h["y"], 1)), []).append(h)
    _open = _opener(shape)
    cones = countersinks(shape)
    final = []
    for k, hs in by_xy.items():
        hs.sort(key=lambda h: (h["r"], h["z0"]))
        base = hs[0]
        base["cbore"] = None
        base["cb_far"] = False
        for big in hs[1:]:
            if big["r"] > base["r"] + 0.05 and not big["thru"]:
                base["cbore"] = (2 * big["r"], big["depth"])
                rx_ = base["x"] + (base["r"] + big["r"]) / 2.0      # probe in the counterbore RING (the axis is
                base["cb_far"] = not _open(rx_, base["y"], big["z1"] + 0.3)   # the hole's void): material above = far
        same = sorted((h_ for h_ in hs if abs(h_["r"] - base["r"]) < 0.02), key=lambda h_: h_["z0"])
        base["each_side"] = False

        def _solid_gap(a, b):                      # material on the axis between two coaxial holes; a void there
            return b["z0"] > a["z1"] + 0.1 and \
                not _open(a["x"], a["y"], (a["z1"] + b["z0"]) / 2.0)   # (a cavity) makes one straight-through column
        if len(same) == 2 and not any(h_["thru"] for h_ in same) and \
                same[0]["z0"] <= zmin + 0.01 and same[1]["z1"] >= zmax - 0.01 and _solid_gap(same[0], same[1]):
            base["each_side"] = True                    # two blind holes, one from each face, material between
            base["depth"] = max(same[0]["depth"], same[1]["depth"])
        elif len(same) >= 2 and not any(h_["thru"] for h_ in same) and \
                any(_solid_gap(same[i_], same[i_ + 1]) for i_ in range(len(same) - 1)):
            # coaxial blind holes opening on DIFFERENT faces (e.g. into a pocket and from the top), with material
            # between: separate holes, each with its own side - never one "THRU" column (arm sync, ARC-106)
            same.sort(key=lambda h_: h_["z0"])
            base = same[-1]
            base.setdefault("cbore", None)
            base.setdefault("cb_far", False)
            base["each_side"] = False
            hs = [base] + [h_ for h_ in hs if abs(h_["r"] - base["r"]) >= 0.02]
            for extra in same[:-1]:
                extra.update(cbore=None, cb_far=False, csk=None, each_side=False)
                extra["far"] = not _open(extra["x"], extra["y"], extra["z1"] + 0.3) and \
                    _open(extra["x"], extra["y"], extra["z0"] - 0.3)
                final.append(extra)
        lo = min(h_["z0"] for h_ in hs)
        hi = max(h_["z1"] for h_ in hs)
        if not base["each_side"] and _open(base["x"], base["y"], lo - 0.3) and _open(base["x"], base["y"], hi + 0.3):
            base["thru"] = True                         # column (bore + counterbore) open at both ends
        base["far"] = (not base["thru"]) and not base["each_side"] and \
            not _open(base["x"], base["y"], base["z1"] + 0.3) and _open(base["x"], base["y"], base["z0"] - 0.3)
        cs_ = cones.get(k)
        if cs_:                                     # side by where the cone's big end opens: probe just OUTSIDE the
            rx_ = base["x"] + cs_[0] / 2.0 + 0.3   # big diameter (inside it, both sides of the big end are void)
            up_open = _open(rx_, base["y"], cs_[3] + 0.3)
            cs_ = (cs_[0], cs_[1], not up_open)
        base["csk"] = cs_
        final.append(base)
    side = [h for h in res if not h["axial"]]
    return final, side, zmax - zmin


def hex_drives(shape, tol=1e-3):
    """Hexagonal drives: 6 planar faces parallel to a common axis, normals 60 deg apart, at one distance from
    a common centre line.  [{"af": across flats, "hole": concave?, "axis": [x, y, z], "center": [x, y, z]}]."""
    planes = []
    for fi, f in enumerate(shape.Faces):
        if f.Surface.__class__.__name__ != "Plane":
            continue
        u0, u1, v0, v1 = f.ParameterRange
        n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        planes.append((fi, V(n), f.CenterOfMass))
    out, used = [], set()
    for axis in (V(1, 0, 0), V(0, 1, 0), V(0, 0, 1)):
        par = [(i, n, c) for i, (_f, n, c) in enumerate(planes) if abs(n.dot(axis)) < tol and i not in used]
        # cluster by the centre line: a hex's 6 face centroids are equidistant from it
        for i, n, c in par:
            if i in used:
                continue
            for j, n2, c2 in par:
                if j <= i or j in used or n.dot(n2) > -1 + tol:        # an opposite flat (antiparallel normal)
                    continue
                mid = (c + c2) * 0.5
                d = abs((c2 - c).dot(n))                               # across flats
                if d < 1.0:
                    continue
                group = []
                for k, n3, c3 in par:
                    if k in used:
                        continue
                    off = c3 - mid
                    off = off - axis * off.dot(axis)
                    ang = math.degrees(math.acos(max(-1.0, min(1.0, n3.dot(n)))))
                    if abs(abs(off.dot(n3)) - d / 2) < 0.02 and min(abs(ang - a) for a in (0, 60, 120, 180)) < 0.5:
                        group.append((k, n3, off))
                if len(group) != 6:
                    continue
                hole = all(n3.dot(off) < 0 for _k, n3, off in group)  # normals point to the centre: a hex hole
                used.update(k for k, _n, _o in group)
                out.append({"af": round(d, 3), "hole": hole, "axis": [axis.x, axis.y, axis.z],
                            "center": [round(mid.x, 3), round(mid.y, 3), round(mid.z, 3)],
                            "faces": sorted(planes[k][0] for k, _n, _o in group)})
                break
    return out


def features(shape):
    """Feature flags for note selection (no boilerplate for absent features)."""
    out = dict(csk=[], groove=False, flat=False, step=False, ins=False, grooves=0, hex=[])
    cyl_r = set()
    for f_ in shape.Faces:
        sn = f_.Surface.__class__.__name__
        if sn == "Cone":
            es = [e for e in f_.Edges if e.Curve.__class__.__name__ == "Circle"]
            if es:
                out["csk"].append(round(2 * max(e.Curve.Radius for e in es), 1))
        elif sn == "Cylinder":
            cyl_r.add(round(f_.Surface.Radius, 2))
    out["csk"] = sorted(set(out["csk"]))
    out["groove"] = any(g in cyl_r for g in GROOVE_R)
    out["step"] = any(r < 4.9 and r not in GROOVE_R for r in cyl_r)
    out["hex"] = hex_drives(shape)
    hex_faces = {fi for h in out["hex"] for fi in h["faces"]}
    cyls = [f_ for f_ in shape.Faces if f_.Surface.__class__.__name__ == "Cylinder"]
    if cyls:
        main = max(cyls, key=lambda f_: f_.Surface.Radius)
        ax_, c0 = V(main.Surface.Axis), V(main.Surface.Center)
        gpos = {round((f_.CenterOfMass - c0).dot(ax_), 0) for f_ in cyls
                if any(abs(f_.Surface.Radius - g) < 0.01 for g in GROOVE_R)}
        out["grooves"] = len(gpos)                  # grooves counted along the shaft axis
        for fi, f_ in enumerate(shape.Faces):    # drive flat: a plane PARALLEL to the shaft axis, off the axis
            if f_.Surface.__class__.__name__ == "Plane" and fi not in hex_faces:   # hex flats are a hex drive
                n_ = f_.normalAt(*f_.ParameterRange[0::2])
                if abs(n_.dot(ax_)) < 1e-3 and abs((f_.CenterOfMass - c0).dot(n_)) > 0.5:
                    out["flat"] = True
    return out


def symmetric(shape):
    """True if the part is its own mirror image about a mid-plane (then L/R hands are identical)."""
    c = shape.BoundBox.Center
    for n_ in (V(1, 0, 0), V(0, 1, 0), V(0, 0, 1)):
        try:
            m = shape.mirror(c, n_)
            if shape.common(m).Volume > 0.995 * shape.Volume:
                return True
        except Exception:
            pass
    return False


def _jsonable(h):
    out = {}
    for k, v in h.items():
        if hasattr(v, "x") and hasattr(v, "z"):
            out[k] = [round(v.x, 4), round(v.y, 4), round(v.z, 4)]
        elif isinstance(v, float):
            out[k] = round(v, 4)
        elif isinstance(v, tuple):
            out[k] = [round(x, 4) if isinstance(x, float) else x for x in v]
        else:
            out[k] = v
    return out


def describe(shape, orient=True):
    """JSON-safe summary (holes, side holes, features) - what a model-vs-drawing check needs."""
    s = oriented(shape) if orient else shape
    holes, side, t = hole_table(s)
    return {"thickness": round(t, 4), "holes": [_jsonable(h) for h in holes], "side_holes": [_jsonable(h) for h in side],
            "features": features(s), "symmetric": symmetric(s)}
