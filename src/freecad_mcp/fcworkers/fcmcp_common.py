# -*- coding: utf-8 -*-
"""Helpers shared by the headless worker scripts (run inside ``freecadcmd``).

This module is imported by the worker scripts in this directory, never by the
MCP server itself (it needs FreeCAD's ``FreeCAD``/``Part`` modules).

Shape sources
-------------
A *source* is a JSON dict describing where shapes come from:

``{"fcstd": path, "include": regex, "exclude": regex, "group_by": "object"|"container",
"visible_only": bool}``
    Leaf solid objects of a saved document (PartDesign features are represented
    by their Body, booleans by the result). Placement = global placement.

``{"files": [paths]}``
    BREP/STEP/IGES files; each solid of a multi-solid file becomes ``file#i``.

``{"python": path, "function": "shapes", "args": {...}}``
    A provider module. ``function(pose, **args)`` returns an iterable of dicts
    ``{"name", "shape", "placement"?, "group"?}`` (or ``(name, shape[, group])``
    tuples). ``shape`` is in its body frame and ``placement`` positions that
    body; give both to let the cache recognise unchanged relative poses.

Poses: any JSON value; for python providers it is passed through, for FCStd
sources it may be ``{"placements": {obj: [x, y, z, ax, ay, az, deg]}}`` or
``{"simulation": name, "frame": i}``.
"""
import hashlib
import importlib.util
import json
import os
import re
import sys

import FreeCAD as App
import Part


def load_params():
    import builtins
    path = getattr(builtins, "FCMCP_PARAMS_FILE", None) or os.environ.get("FCMCP_PARAMS")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class Progress:
    """Throttled progress file writer: ``done total message``."""

    def __init__(self, path, total, every=1.0):
        import time
        self.path, self.total, self.every, self._t = path, total, every, 0.0
        self._time = time.time

    def __call__(self, done, msg=""):
        if not self.path:
            return
        now = self._time()
        if now - self._t < self.every and done < self.total:
            return
        self._t = now
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write("%d %d %s" % (done, self.total, msg))
        except OSError:
            pass


# ------------------------------------------------------------------ geometry keys
FINGERPRINT_VERSION = "g2"     # bump whenever fingerprint() changes: old BREP/pair/hole cache keys are ignored


def _face_key(f):
    """Face descriptor in the shape's own frame: surface type, area, centroid and normal/axis (1 um grid)."""
    t = f.Surface.__class__.__name__
    try:
        c = f.CenterOfMass
    except Exception:
        c = f.BoundBox.Center
    extra = ""
    srf = f.Surface
    for attr in ("Axis", "Normal"):           # cylinder/cone axis, or plane normal (orientation-insensitive)
        v = getattr(srf, attr, None)
        if v is not None:
            try:
                x, y, z = float(v.x), float(v.y), float(v.z)
                if (x, y, z) < (0.0, 0.0, 0.0):
                    x, y, z = -x, -y, -z
                extra += "%s%.4f,%.4f,%.4f" % (attr[0], x, y, z)
            except Exception:
                pass
            break
    for attr in ("Radius", "Center"):
        v = getattr(srf, attr, None)
        if isinstance(v, float):
            extra += "r%.4f" % v
    return "%s|%.3f|%.3f,%.3f,%.3f|%s" % (t, f.Area, c.x, c.y, c.z, extra)


def fingerprint(shape):
    """Geometry fingerprint of a shape in its own frame (16 hex chars).

    Volume, area, topology counts and the bounding box alone do not change when a hole moves, which made
    the interference/hole caches reuse a stale BREP (false clashes and, worse, false clears). The sorted
    per-face descriptors (type, area, centroid, axis/normal, radius) do."""
    b = shape.BoundBox
    s = "%s/%.3f/%.3f/%d/%d/%d/%.3f,%.3f,%.3f,%.3f,%.3f,%.3f" % (
        FINGERPRINT_VERSION, shape.Volume, shape.Area, len(shape.Faces), len(shape.Edges), len(shape.Vertexes),
        b.XMin, b.YMin, b.ZMin, b.XMax, b.YMax, b.ZMax)
    h = hashlib.sha1(s.encode())
    for k in sorted(_face_key(f) for f in shape.Faces):
        h.update(k.encode())
    return h.hexdigest()[:16]


def mat12(placement):
    m = placement.toMatrix()
    return [m.A11, m.A12, m.A13, m.A14, m.A21, m.A22, m.A23, m.A24, m.A31, m.A32, m.A33, m.A34]


def placement12(v):
    m = App.Matrix(v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10], v[11],
                   0, 0, 0, 1)
    return App.Placement(m)


def bbox6(b):
    return [b.XMin, b.YMin, b.ZMin, b.XMax, b.YMax, b.ZMax]


def transformed_bbox(b, plc):
    """World AABB of a body-frame box under placement (corner transform: conservative, cheap)."""
    if plc.isIdentity():
        return bbox6(b)
    xs, ys, zs = [], [], []
    for x in (b.XMin, b.XMax):
        for y in (b.YMin, b.YMax):
            for z in (b.ZMin, b.ZMax):
                v = plc.multVec(App.Vector(x, y, z))
                xs.append(v.x)
                ys.append(v.y)
                zs.append(v.z)
    return [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]


def load_brep(path):
    s = Part.Shape()
    s.importBrep(path)
    return s


# ------------------------------------------------------------------ sources
_DOCS = {}
_MODULES = {}


def _placement_from_list(v):
    return App.Placement(App.Vector(v[0], v[1], v[2]), App.Rotation(App.Vector(v[3], v[4], v[5]), v[6]))


def _open_doc(path):
    path = os.path.abspath(path)
    if path not in _DOCS:
        _DOCS[path] = App.openDocument(path)
    return _DOCS[path]


def _container(obj):
    """Outermost App::Part / Assembly container of obj, or None."""
    best = None
    cur = obj
    seen = set()
    while cur is not None and cur.Name not in seen:
        seen.add(cur.Name)
        parents = [p for p in cur.InList if p.TypeId in ("App::Part", "Assembly::AssemblyObject", "App::Link")]
        if not parents:
            break
        cur = parents[0]
        if cur.TypeId == "App::Part":
            best = cur
    return best


_CONTAINER_TYPES = ("App::Part", "Assembly::AssemblyObject", "Assembly::JointGroup", "App::DocumentObjectGroup",
                    "App::Origin", "App::Line", "App::Plane", "App::Point", "Assembly::AssemblyLink")


def _fcstd_items(source, pose):
    doc = _open_doc(source["fcstd"])
    inc = re.compile(source["include"]) if source.get("include") else None
    exc = re.compile(source["exclude"]) if source.get("exclude") else None
    pose = pose or {}
    if pose.get("simulation") is not None:
        _apply_sim_frame(doc, pose["simulation"], int(pose.get("frame", 0)))
    for name, v in (pose.get("placements") or {}).items():
        o = doc.getObject(name) or (doc.getObjectsByLabel(name) or [None])[0]
        if o is not None:
            o.Placement = _placement_from_list(v)
    out = []
    for obj in doc.Objects:
        if obj.TypeId in _CONTAINER_TYPES:
            continue
        if obj.TypeId != "App::Link" and not hasattr(obj, "Shape"):
            continue
        consumed = any(p.TypeId not in _CONTAINER_TYPES and p.TypeId != "App::Link"
                       and (p.isDerivedFrom("Part::Feature") or p.isDerivedFrom("PartDesign::Body"))
                       for p in obj.InList)
        if consumed:
            continue
        if source.get("visible_only") and not getattr(obj, "Visibility", True):
            continue
        key = obj.Label or obj.Name
        if inc and not (inc.search(key) or inc.search(obj.Name)):
            continue
        if exc and (exc.search(key) or exc.search(obj.Name)):
            continue
        try:
            shape = Part.getShape(obj, "", needSubElement=False, refine=False, transform=False)
        except Exception:
            continue
        if shape.isNull() or not shape.Solids:
            continue
        try:
            plc = obj.getGlobalPlacement()
        except Exception:
            plc = obj.Placement if hasattr(obj, "Placement") else App.Placement()
        grp = obj.Name
        if source.get("group_by") == "container":
            c = _container(obj)
            grp = c.Name if c is not None else obj.Name
        out.append(dict(name=key, shape=shape, placement=plc, group=grp))
    return out


def _apply_sim_frame(doc, sim_name, frame):
    asm = None
    sim = doc.getObject(sim_name) or (doc.getObjectsByLabel(sim_name) or [None])[0]
    for o in doc.Objects:
        if o.TypeId == "Assembly::AssemblyObject" and (sim is None or sim in o.OutListRecursive):
            asm = o
            break
    if asm is None or sim is None:
        raise RuntimeError("simulation %r / its assembly not found" % sim_name)
    key = (doc.Name, sim.Name)
    if key not in _SIM_DONE:
        asm.generateSimulation(sim)
        _SIM_DONE.add(key)
    asm.updateForFrame(frame)


_SIM_DONE = set()


def _files_items(source):
    out = []
    for path in source["files"]:
        shape = Part.read(path)
        solids = shape.Solids
        base = os.path.splitext(os.path.basename(path))[0]
        if len(solids) <= 1:
            out.append(dict(name=base, shape=shape, placement=App.Placement(), group=base))
        else:
            for i, s in enumerate(solids):
                out.append(dict(name="%s#%d" % (base, i), shape=s, placement=App.Placement(), group=base))
    return out


def _provider_module(path):
    path = os.path.abspath(path)
    if path not in _MODULES:
        d = os.path.dirname(path)
        if d not in sys.path:
            sys.path.insert(0, d)
        spec = importlib.util.spec_from_file_location("fcmcp_provider_%d" % len(_MODULES), path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULES[path] = mod
    return _MODULES[path]


def _python_items(source, pose):
    fn = getattr(_provider_module(source["python"]), source.get("function") or "shapes")
    out = []
    for it in fn(pose, **(source.get("args") or {})):
        if isinstance(it, dict):
            d = dict(it)
        else:
            d = dict(name=it[0], shape=it[1], group=it[2] if len(it) > 2 else it[0])
        d.setdefault("group", d["name"])
        if d.get("placement") is None:
            d["placement"] = App.Placement()
        if d["shape"] is None or d["shape"].isNull():
            continue
        out.append(d)
    return out


def source_items(source, pose=None):
    """List of dicts {name, shape (body frame), placement (world), group} for one pose."""
    if source.get("fcstd"):
        return _fcstd_items(source, pose)
    if source.get("files"):
        return _files_items(source)
    if source.get("python"):
        return _python_items(source, pose)
    raise ValueError("source needs one of 'fcstd', 'files', 'python'")


def dependency_files(source):
    """Files whose change invalidates a prepared manifest: the FCStd / shape files, or for python
    providers every loaded module outside FreeCAD / the Python installation, plus source["deps"] globs."""
    import glob
    files = set()
    if source.get("fcstd"):
        files.add(os.path.abspath(source["fcstd"]))
        for d in _DOCS.values():            # linked documents loaded alongside
            for o in d.Objects:
                if o.TypeId == "App::Link" and getattr(o, "LinkedObject", None) is not None:
                    fn = getattr(o.LinkedObject.Document, "FileName", "")
                    if fn:
                        files.add(os.path.abspath(fn))
    for f in source.get("files") or []:
        files.add(os.path.abspath(f))
    if source.get("python"):
        files.add(os.path.abspath(source["python"]))
        skip = [os.path.abspath(p).lower() for p in {sys.prefix, sys.base_prefix, App.getHomePath(),
                                                     App.getResourceDir(), App.getUserAppDataDir()} if p]
        for m in list(sys.modules.values()):
            fn = getattr(m, "__file__", None)
            if not fn or not fn.endswith(".py"):
                continue
            fa = os.path.abspath(fn)
            if any(fa.lower().startswith(s) for s in skip) or os.path.basename(fa).startswith("fcmcp_"):
                continue
            files.add(fa)
    for pat in source.get("deps") or []:
        files.update(os.path.abspath(f) for f in glob.glob(pat, recursive=True))
    return sorted(files)


def provider_poses(source):
    """Default pose list from the provider's ``poses_function`` (python sources) or [None]."""
    if source.get("python") and source.get("poses_function"):
        mod = _provider_module(source["python"])
        return list(getattr(mod, source["poses_function"])(**(source.get("pose_args") or {})))
    return [None]
