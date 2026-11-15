# -*- coding: utf-8 -*-
"""GUI-side frame capture: assembly simulation frames and/or a camera path -> PNG files.

Runs inside the FreeCAD GUI in chunks (one MCP execute call per chunk) so no
single call approaches the GUI time budget. The generated simulation is cached
in the live namespace between chunks.
"""
import math
import os

import FreeCAD as App
import FreeCADGui as Gui

_STATE = globals().setdefault("_FCMCP_RENDER_STATE", {})


def _lerp(a, b, t):
    return [x + (y - x) * t for x, y in zip(a, b)]


def camera_at(cam, i, n):
    """cam: {"keys": [{"t": 0..1, "position": [..], "target": [..], "up": [..]?}, ...]}
    or {"orbit": {"center", "radius", "height", "start_deg", "end_deg", "target_z"?}} -> (pos, target, up)."""
    t = i / max(1, n - 1)
    if cam.get("orbit"):
        o = cam["orbit"]
        a = math.radians(o.get("start_deg", 0) + (o.get("end_deg", 360) - o.get("start_deg", 0)) * t)
        c = o.get("center", [0, 0, 0])
        r = o["radius"]
        pos = [c[0] + r * math.cos(a), c[1] + r * math.sin(a), c[2] + o.get("height", r * 0.5)]
        return pos, [c[0], c[1], c[2] + o.get("target_z", 0)], o.get("up", [0, 0, 1])
    keys = sorted(cam["keys"], key=lambda k: k["t"])
    if t <= keys[0]["t"]:
        k = keys[0]
        return k["position"], k["target"], k.get("up", [0, 0, 1])
    for a, b in zip(keys, keys[1:]):
        if a["t"] <= t <= b["t"]:
            u = (t - a["t"]) / max(1e-9, b["t"] - a["t"])
            u = u * u * (3 - 2 * u) if cam.get("ease", True) else u
            return (_lerp(a["position"], b["position"], u), _lerp(a["target"], b["target"], u),
                    _lerp(a.get("up", [0, 0, 1]), b.get("up", [0, 0, 1]), u))
    k = keys[-1]
    return k["position"], k["target"], k.get("up", [0, 0, 1])


def set_camera(view, pos, target, up):
    from pivy import coin
    cam = view.getCameraNode()
    cam.position.setValue(coin.SbVec3f(*pos))
    cam.pointAt(coin.SbVec3f(*target), coin.SbVec3f(*up))
    d = math.dist(pos, target)
    if hasattr(cam, "focalDistance"):
        cam.focalDistance.setValue(d)


def render_chunk(p):
    doc = App.getDocument(p["doc"])
    Gui.ActiveDocument = Gui.getDocument(doc.Name)
    view = Gui.ActiveDocument.ActiveView
    asm = None
    n_sim = None
    if p.get("simulation"):
        sim = doc.getObject(p["simulation"]) or (doc.getObjectsByLabel(p["simulation"]) or [None])[0]
        if sim is None:
            raise ValueError("simulation %r not found" % p["simulation"])
        for o in doc.Objects:
            if o.TypeId == "Assembly::AssemblyObject" and (not p.get("assembly") or o.Name == p["assembly"]) \
                    and sim in o.OutListRecursive:
                asm = o
                break
        key = (doc.Name, sim.Name)
        if p.get("regenerate") or _STATE.get("sim") != key:
            asm.generateSimulation(sim)
            _STATE["sim"] = key
        n_sim = asm.numberOfFrames()
    frames = p["frames"]             # absolute frame numbers for this chunk
    total = int(p["total"])          # length of the whole sequence (camera param)
    index0 = int(p["index0"])        # sequence index of frames[0]
    os.makedirs(p["out_dir"], exist_ok=True)
    pat = p.get("pattern", "frame_%05d.png")
    w, h = int(p.get("width", 1280)), int(p.get("height", 720))
    bg = p.get("background", "Current")
    written = []
    hidden = []
    if p.get("hide_joints", True):          # joint / grounded-joint glyphs clutter renders
        for o in doc.Objects:
            if o.TypeId == "Assembly::JointGroup" and o.ViewObject and o.ViewObject.Visibility:
                o.ViewObject.Visibility = False
                hidden.append(o)
    try:
        _frames(p, doc, view, asm, n_sim, frames, index0, total, pat, w, h, bg, written)
    finally:
        for o in hidden:
            o.ViewObject.Visibility = True
    return {"written": len(written), "last": written[-1] if written else None, "sim_frames": n_sim}


def _frames(p, doc, view, asm, n_sim, frames, index0, total, pat, w, h, bg, written):
    for k, f in enumerate(frames):
        if asm is not None:
            asm.updateForFrame(min(int(f), n_sim - 1))
        cam = p.get("camera")
        if cam:
            set_camera(view, *camera_at(cam, index0 + k, total))
            node = view.getCameraNode()
            if cam.get("height") and hasattr(node, "height"):      # orthographic view volume (mm)
                node.height.setValue(float(cam["height"]))
            elif cam.get("fit", True):                             # keep direction, frame everything
                view.fitAll()
        Gui.updateGui()
        path = os.path.join(p["out_dir"], pat % (index0 + k))
        view.saveImage(path, w, h, bg)
        written.append(path)


def sim_frame_count(p):
    doc = App.getDocument(p["doc"])
    sim = doc.getObject(p["simulation"]) or (doc.getObjectsByLabel(p["simulation"]) or [None])[0]
    if sim is None:
        raise ValueError("simulation %r not found" % p["simulation"])
    for o in doc.Objects:
        if o.TypeId == "Assembly::AssemblyObject" and sim in o.OutListRecursive:
            o.generateSimulation(sim)
            _STATE["sim"] = (doc.Name, sim.Name)
            return {"frames": o.numberOfFrames(), "assembly": o.Name}
    raise ValueError("no assembly contains %r" % p["simulation"])
