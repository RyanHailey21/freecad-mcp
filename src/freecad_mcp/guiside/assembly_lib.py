# -*- coding: utf-8 -*-
"""Assembly helpers that run in the FreeCAD GUI or headless (``freecadcmd``).

* attach_view_providers: headless-built assemblies lose the Python view
  providers of their joints/simulations/motions (``ViewObject.Proxy is None``),
  which breaks dragging, the solver UI and simulation playback. Re-attach the
  matching ``ViewProvider<Class>`` from the feature's own module.
* solve_check: solve an assembly and report how far parts moved (drift),
  optionally restoring the original placements.
* simulation_check: generate an Assembly simulation, step its frames and
  report solver jumps (frames where a part moves far more than usual).
"""
import math

import FreeCAD as App


def _doc_for(p):
    if p.get("path"):
        return App.openDocument(p["path"])
    if p["doc"] not in App.listDocuments():
        raise ValueError("document %r is not open" % p["doc"])
    return App.getDocument(p["doc"])


def _assembly(doc, name=None):
    if name:
        o = doc.getObject(name) or (doc.getObjectsByLabel(name) or [None])[0]
        if o is None:
            raise ValueError("assembly %r not found" % name)
        return o
    for o in doc.Objects:
        if o.TypeId == "Assembly::AssemblyObject":
            return o
    raise ValueError("no Assembly::AssemblyObject in %s" % doc.Name)


def attach_view_providers(p):
    if not App.GuiUp:
        raise RuntimeError("view providers need the FreeCAD GUI")
    import importlib
    doc = _doc_for(p)
    fixed, failed, ok = [], [], 0
    for o in doc.Objects:
        proxy = getattr(o, "Proxy", None)
        vo = getattr(o, "ViewObject", None)
        if proxy is None or vo is None:
            continue
        if getattr(vo, "Proxy", None) is not None:
            ok += 1
            continue
        mod_name, cls = type(proxy).__module__, "ViewProvider" + type(proxy).__name__
        try:
            mod = importlib.import_module(mod_name)
            vp_cls = getattr(mod, cls, None)
            if vp_cls is None:
                failed.append([o.Name, "%s.%s missing" % (mod_name, cls)])
                continue
            vp_cls(vo)
            fixed.append([o.Name, "%s.%s" % (mod_name, cls)])
        except Exception as exc:
            failed.append([o.Name, "%s: %s" % (type(exc).__name__, exc)])
    doc.recompute()
    return {"doc": doc.Name, "attached": len(fixed), "already_ok": ok, "fixed": fixed[:50], "failed": failed[:20]}


def _parts(asm):
    out = {}
    for o in asm.OutListRecursive:
        if o.TypeId in ("App::Part", "App::Link", "Part::Feature", "PartDesign::Body") and hasattr(o, "Placement"):
            out[o.Name] = o
    return out


def _drift(parts, before):
    rows = []
    for k, o in parts.items():
        a, b = before[k], o.Placement
        t = (b.Base - a.Base).Length
        r = math.degrees(b.Rotation.multiply(a.Rotation.inverted()).Angle)
        rows.append([k, round(t, 5), round(r, 5)])
    rows.sort(key=lambda r: -(r[1] + r[2]))
    return rows


def solve_check(p):
    doc = _doc_for(p)
    asm = _assembly(doc, p.get("assembly"))
    parts = _parts(asm)
    before = {k: App.Placement(o.Placement) for k, o in parts.items()}
    res = asm.solve()
    rows = _drift(parts, before)
    tol_t, tol_r = float(p.get("tol_mm", 0.01)), float(p.get("tol_deg", 0.01))
    moved = [r for r in rows if r[1] > tol_t or r[2] > tol_r]
    restored = False
    if p.get("restore", True) or (p.get("restore_if_drift") and moved):
        for k, o in parts.items():
            o.Placement = before[k]
        restored = True
    out = {"doc": doc.Name, "assembly": asm.Name, "solve_result": res, "solved": res == 0, "parts": len(parts),
           "max_drift_mm": rows[0][1] if rows else 0.0, "max_drift_deg": max((r[2] for r in rows), default=0.0),
           "moved_parts": len(moved), "worst": rows[:int(p.get("top", 10))], "restored": restored}
    if p.get("path"):
        App.closeDocument(doc.Name)
    return out


def simulation_check(p):
    doc = _doc_for(p)
    sim = None
    if p.get("simulation"):
        sim = doc.getObject(p["simulation"]) or (doc.getObjectsByLabel(p["simulation"]) or [None])[0]
    if sim is None:
        sims = [o for o in doc.Objects if getattr(getattr(o, "Proxy", None), "__class__", type).__name__ == "Simulation"]
        sim = sims[0] if sims else None
    if sim is None:
        raise ValueError("no simulation found")
    asm = _assembly(doc, p.get("assembly")) if p.get("assembly") else None
    if asm is None:
        for o in doc.Objects:
            if o.TypeId == "Assembly::AssemblyObject" and sim in o.OutListRecursive:
                asm = o
                break
    asm = asm or _assembly(doc)
    parts = _parts(asm)
    start = {k: App.Placement(o.Placement) for k, o in parts.items()}
    asm.generateSimulation(sim)
    n = asm.numberOfFrames()
    step = max(1, int(p.get("every", 1)))
    prev = None
    jumps, steps = [], []
    for f in range(0, n, step):
        asm.updateForFrame(f)
        cur = {k: (o.Placement.Base, o.Placement.Rotation) for k, o in parts.items()}
        if prev is not None:
            worst = max(((cur[k][0] - prev[k][0]).Length, k) for k in cur) if cur else (0, "")
            steps.append(worst[0])
            jumps.append([f, round(worst[0], 4), worst[1]])
        prev = cur
    med = sorted(steps)[len(steps) // 2] if steps else 0.0
    factor = float(p.get("jump_factor", 20.0))
    floor = float(p.get("jump_floor_mm", 1.0))
    bad = [j for j in jumps if j[1] > max(floor, factor * med)]
    asm.updateForFrame(0)
    if p.get("restore", True):
        for k, o in parts.items():
            o.Placement = start[k]
    out = {"doc": doc.Name, "assembly": asm.Name, "simulation": sim.Name, "frames": n, "sampled": len(steps) + 1,
           "median_step_mm": round(med, 4), "max_step_mm": round(max(steps), 4) if steps else 0.0,
           "jumps": bad[:30], "status": "jumps" if bad else ("ok" if n else "no_frames")}
    if p.get("path"):
        App.closeDocument(doc.Name)
    return out
