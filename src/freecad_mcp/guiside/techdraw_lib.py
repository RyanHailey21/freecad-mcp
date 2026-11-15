# -*- coding: utf-8 -*-
"""GUI-side TechDraw helpers (sent as source with each MCP call; runs inside FreeCAD).

Native TechDraw objects only: DrawSVGTemplate (bundled or project templates,
EditableTexts fields), DrawViewPart / DrawProjGroup / DrawViewSection /
DrawViewDetail, DrawViewDimension (TheoreticalExact = basic), DrawViewBalloon
(datum feature symbols, feature control frames, hole callouts), cosmetic
circles/lines and DrawRichAnno notes.

Coordinates: view X/Y and note positions are page mm; balloon origins/targets
and cosmetic geometry are model mm from the view centre (Y up, scaled with the
view); dimension label positions are page mm offsets from the view centre.
Gotcha handled here: page.addView() resets X/Y and Scale, so they are set
after adding, with ScaleType="Custom".
The pure ASME Y14.5 rules (fcmcp_asme_rules) and the Qt-scene layout engine
(fcmcp_layout) are installed as modules by the MCP server before this runs.
"""
import glob
import math
import os
import time

import FreeCAD as App

try:
    import fcmcp_asme_rules as R
except ImportError:                    # library loaded without the helper modules (compile tests)
    R = None
try:
    import fcmcp_layout as L
except ImportError:
    L = None

V = App.Vector
TEXT_H = 3.5            # lettering height (mm) for balloons and dimensions; TechDraw's default is ~5 mm
SYM = {"position": "⌖", "flatness": "⏥", "perpendicularity": "⟂", "circularity": "○",
       "cylindricity": "⌭", "straightness": "⏤", "runout": "↗", "total_runout": "⌰",
       "parallelism": "∥", "angularity": "∠", "profile_surface": "⌓", "profile_line": "⌒",
       "concentricity": "◎", "symmetry": "⌯"}
TOKENS = {"{dia}": "⌀", "{mmc}": "Ⓜ", "{lmc}": "Ⓛ", "{cbore}": "⌴", "{csk}": "⌵",
          "{depth}": "↧", "{deg}": "°", "{pm}": "±", "{sq}": "□", "{r}": "R"}
for _k, _v in SYM.items():
    TOKENS["{%s}" % _k] = _v


def T(text):
    """Expand {dia}, {position}, ... tokens."""
    s = str(text)
    for k, v in TOKENS.items():
        s = s.replace(k, v)
    return s


def _doc(name):
    d = App.getDocument(name) if name in App.listDocuments() else None
    if d is None:
        raise ValueError("document %r is not open" % name)
    return d


def _obj(doc, name):
    o = doc.getObject(name) or (doc.getObjectsByLabel(name) or [None])[0]
    if o is None:
        raise ValueError("object %r not found in %s" % (name, doc.Name))
    return o


def _vec(v, default=None):
    if v is None:
        return default
    return V(float(v[0]), float(v[1]), float(v[2]) if len(v) > 2 else 0.0)


# ------------------------------------------------------------------ templates / pages
def find_template(name):
    if os.path.isfile(name):
        return name
    root = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates")
    stem = name[:-4] if name.lower().endswith(".svg") else name
    hits = sorted(glob.glob(os.path.join(root, "**", stem + ".svg"), recursive=True))
    if not hits:
        hits = sorted(glob.glob(os.path.join(root, "**", "*" + stem + "*.svg"), recursive=True))
    if not hits:
        raise ValueError("template %r not found (looked in %s)" % (name, root))
    return hits[0]


def list_templates(p):
    root = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates")
    pat = (p.get("filter") or "").lower()
    out = [os.path.relpath(f, root).replace("\\", "/") for f in glob.glob(os.path.join(root, "**", "*.svg"), recursive=True)]
    return sorted(f for f in out if pat in f.lower())


def create_page(p):
    name = p["doc"]
    if p.get("new_document", True):
        if name in App.listDocuments():
            if not p.get("replace", False):
                raise ValueError("document %r already open (pass replace=true to close and recreate it)" % name)
            App.closeDocument(name)
        doc = App.newDocument(name)
    else:
        doc = _doc(name)
    page = doc.addObject("TechDraw::DrawPage", p.get("page") or "Page")
    tpl = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
    tpl.Template = find_template(p.get("template") or "ANSIB_Landscape")
    page.Template = tpl
    fields = {k: T(v) for k, v in (p.get("fields") or {}).items()}
    et = dict(tpl.EditableTexts)
    unknown = sorted(k for k in fields if k not in et)
    et.update({k: v for k, v in fields.items() if k in et})
    tpl.EditableTexts = et
    doc.recompute()
    return {"doc": doc.Name, "page": page.Name, "template": tpl.Template,
            "size_mm": [round(float(tpl.Width), 1), round(float(tpl.Height), 1)],
            "fields": sorted(et), "unknown_fields": unknown}


def set_fields(p):
    doc = _doc(p["doc"])
    page = _obj(doc, p.get("page") or "Page")
    tpl = page.Template
    et = dict(tpl.EditableTexts)
    unknown = sorted(k for k in p["fields"] if k not in et)
    et.update({k: T(v) for k, v in p["fields"].items() if k in et})
    tpl.EditableTexts = et
    return {"updated": sorted(k for k in p["fields"] if k in et), "unknown_fields": unknown}


# ------------------------------------------------------------------ views
def _edges(view):
    out, i = [], 0
    while True:
        try:
            e = view.getEdgeByIndex(i)
        except Exception:
            break
        if e is None:
            break
        out.append((i, e))
        i += 1
    return out


def _vertices(view):
    out, i = [], 0
    while True:
        try:
            v = view.getVertexByIndex(i)
        except Exception:
            break
        if v is None:
            break
        out.append((i, v.Point))
        i += 1
    return out


def _sig(v):
    es = _edges(v)
    if not es:
        return None
    xs = [p for _i, e in es for p in (e.Vertexes[0].Point.x, e.Vertexes[-1].Point.x)]
    ys = [p for _i, e in es for p in (e.Vertexes[0].Point.y, e.Vertexes[-1].Point.y)]
    return (len(es), round(min(xs), 3), round(max(xs), 3), round(min(ys), 3), round(max(ys), 3))


def wait_views(doc, views, timeout=30.0):
    """Views are done when their HLR geometry is present AND unchanged over 3 polls (TechDraw computes in a
    background thread: an early read can return partial geometry and a vertex numbering that changes).
    Stable *stale* geometry also passes this; part_drawing adds an extent check (stale-geometry guard)."""
    import FreeCADGui as Gui
    views = [v for v in views if v.isDerivedFrom("TechDraw::DrawViewPart")]
    end = time.time() + timeout
    last, same = None, 0
    while True:
        doc.recompute()
        Gui.updateGui()
        busy = any(getattr(v, "isComputing", lambda: False)() for v in views if hasattr(v, "isComputing"))
        sig = None if busy else tuple(_sig(v) for v in views)
        if sig is not None and None not in sig:
            same = same + 1 if sig == last else 0
            last = sig
            if same >= 2:
                return True
        if time.time() > end:
            return bool(sig) and None not in sig
        time.sleep(0.3)


def refresh_views(doc, views=None, timeout=30.0):
    """Re-run hidden-line removal at the views' CURRENT scale (items do not recompute on a scale change
    alone) and wait for stable geometry - before anything is measured, laid out or exported."""
    views = views or [o for o in doc.Objects if o.TypeId in ("TechDraw::DrawProjGroupItem", "TechDraw::DrawViewPart",
                                                             "TechDraw::DrawViewSection", "TechDraw::DrawViewDetail")]
    for v in views:
        v.touch()
    doc.recompute()
    return wait_views(doc, views, timeout)


def rebind_dims(doc):
    """Re-point vertex dimensions at the vertices that are at their recorded positions (RefPts) now:
    TechDraw renumbers vertices on recompute."""
    n = 0
    for d in doc.Objects:
        rp = getattr(d, "RefPts", "")
        if d.TypeId != "TechDraw::DrawViewDimension" or not rp:
            continue
        view = d.References2D[0][0]
        verts = _vertices(view)
        new = []
        for t in rp.split(";"):
            x, y = (float(c) for c in t.split(","))
            hit = [i for i, q in verts if abs(q.x - x) < 1e-3 and abs(q.y - y) < 1e-3]
            if not hit:
                break
            new.append("Vertex%d" % hit[0])
        if len(new) == len(rp.split(";")):
            old = list(d.References2D[0][1]) if isinstance(d.References2D[0][1], (list, tuple)) else \
                [r[1] for r in d.References2D]
            if old != new:
                d.References2D = [(view, r) for r in new]
                n += 1
    doc.recompute()
    return n


def place(page, v, x=None, y=None, scale=None):
    """page.addView() resets X/Y/Scale: add first, then set custom scale and position."""
    if v not in page.Views:
        page.addView(v)
    if scale is not None and hasattr(v, "Scale"):
        if hasattr(v, "ScaleType"):
            v.ScaleType = "Custom"
        v.Scale = float(scale)
    if x is not None:
        v.X = float(x)
    if y is not None:
        v.Y = float(y)
    return v


def _sources(doc, spec):
    if spec.get("brep"):
        import Part
        s = Part.Shape()
        s.importBrep(spec["brep"])
        f = doc.addObject("Part::Feature", spec.get("source_name") or "DrawingPart")
        f.Shape = s
        if f.ViewObject:
            f.ViewObject.Visibility = False
        return [f]
    src = spec.get("source")
    if isinstance(src, str):
        src = [src]
    if not src:
        raise ValueError("view needs 'source' (object names) or 'brep'")
    return [_obj(doc, n) for n in src]


def add_views(p):
    doc = _doc(p["doc"])
    page = _obj(doc, p.get("page") or "Page")
    made, created_parts = [], {}
    for spec in p["views"]:
        typ = spec.get("type", "part")
        name = spec.get("name") or {"part": "View", "projgroup": "ProjGroup", "section": "Section",
                                    "detail": "Detail"}.get(typ, "View")
        if typ in ("part", "projgroup", "section"):
            key = spec.get("brep") or tuple(spec.get("source") or ())
            if spec.get("brep") and key in created_parts:
                src = created_parts[key]
            else:
                src = _sources(doc, spec) if (spec.get("brep") or spec.get("source")) else None
                if spec.get("brep"):
                    created_parts[key] = src
        if typ == "part":
            v = doc.addObject("TechDraw::DrawViewPart", name)
            v.Source = src
            v.Direction = _vec(spec.get("direction"), V(0, 0, 1))
            if spec.get("x_direction"):
                v.XDirection = _vec(spec["x_direction"])
            if "hidden" in spec:
                v.HardHidden = bool(spec["hidden"])
            place(page, v, spec.get("x"), spec.get("y"), spec.get("scale"))
            made.append(v)
        elif typ == "projgroup":
            v = doc.addObject("TechDraw::DrawProjGroup", name)
            page.addView(v)
            v.Source = src
            v.ProjectionType = spec.get("projection_type", "Third angle")
            v.addProjection("Front")
            v.Anchor.Direction = _vec(spec.get("direction"), V(0, 0, 1))
            if spec.get("x_direction"):
                v.Anchor.XDirection = _vec(spec["x_direction"])
            for pr in spec.get("projections", ["Right", "Top"]):
                if pr != "Front":
                    v.addProjection(pr)
            if spec.get("scale") is not None:
                v.ScaleType = "Custom"
                v.Scale = float(spec["scale"])
            if spec.get("x") is not None:
                v.X, v.Y = float(spec["x"]), float(spec.get("y", v.Y))
            if "spacing" in spec:
                v.AutoDistribute = True
                v.spacingX = v.spacingY = float(spec["spacing"])
            for sub in v.Views:
                if "hidden" in spec:
                    sub.HardHidden = bool(spec["hidden"])
            made.append(v)
        elif typ == "section":
            base = _obj(doc, spec["base_view"])
            v = doc.addObject("TechDraw::DrawViewSection", name)
            v.BaseView = base
            v.Source = src or base.Source
            n = _vec(spec.get("normal"), V(0, -1, 0))
            v.SectionNormal = n
            v.SectionOrigin = _vec(spec.get("origin"), V(0, 0, 0))
            v.SectionDirection = spec.get("section_direction", "Down")
            v.Direction = _vec(spec.get("direction"), n)
            v.XDirection = _vec(spec.get("x_direction"), base.XDirection)
            v.SectionSymbol = spec.get("symbol", "A")
            place(page, v, spec.get("x"), spec.get("y"), spec.get("scale", getattr(base, "Scale", 1.0)))
            made.append(v)
        elif typ == "detail":
            base = _obj(doc, spec["base_view"])
            v = doc.addObject("TechDraw::DrawViewDetail", name)
            v.BaseView = base
            v.Source = base.Source
            v.Direction = base.Direction
            v.XDirection = base.XDirection
            v.AnchorPoint = _vec(spec.get("anchor"), V(0, 0, 0))
            v.Radius = float(spec.get("radius", 10))
            v.Reference = spec.get("reference", "A")
            place(page, v, spec.get("x"), spec.get("y"), spec.get("scale", 2 * getattr(base, "Scale", 1.0)))
            made.append(v)
        else:
            raise ValueError("unknown view type %r" % typ)
        if spec.get("label"):
            made[-1].Label = spec["label"]
    parts = [s for v in made for s in (v.Views if v.isDerivedFrom("TechDraw::DrawProjGroup") else [v])]
    ok = wait_views(doc, parts, float(p.get("wait", 30)))
    out = []
    for v in made:
        subs = v.Views if v.isDerivedFrom("TechDraw::DrawProjGroup") else [v]
        for s in subs:
            out.append({"name": s.Name, "label": s.Label, "type": s.TypeId.split("::")[1],
                        "x": round(float(s.X), 2), "y": round(float(s.Y), 2),
                        "scale": round(float(getattr(s, "Scale", 1.0)), 4), "edges": len(_edges(s))})
    return {"views": out, "geometry_ready": ok}


def view_geometry(p):
    """Compact list of a view's circles, straight lines and extreme vertices (model mm from view centre)."""
    doc = _doc(p["doc"])
    v = _obj(doc, p["view"])
    wait_views(doc, [v], 10)
    lim = int(p.get("limit", 60))
    circles, lines, arcs = [], [], []
    for i, e in _edges(v):
        c = e.Curve
        cn = c.__class__.__name__
        if cn == "Circle":
            full = abs(abs(e.LastParameter - e.FirstParameter) - 2 * math.pi) < 1e-3
            row = ["Edge%d" % i, round(c.Radius, 3), round(c.Center.x, 2), round(c.Center.y, 2)]
            (circles if full else arcs).append(row)
        elif cn in ("Line", "LineSegment") and len(e.Vertexes) == 2:
            a, b = e.Vertexes[0].Point, e.Vertexes[1].Point
            lines.append(["Edge%d" % i, round(a.x, 2), round(a.y, 2), round(b.x, 2), round(b.y, 2),
                          round(e.Length, 2)])
    lines.sort(key=lambda r: -r[5])
    verts = _vertices(v)
    ext = {}
    if verts:
        for key, fn in (("xmin", lambda q: q.x), ("ymin", lambda q: q.y)):
            i, q = min(verts, key=lambda t: fn(t[1]))
            ext[key] = ["Vertex%d" % i, round(q.x, 2), round(q.y, 2)]
        for key, fn in (("xmax", lambda q: q.x), ("ymax", lambda q: q.y)):
            i, q = max(verts, key=lambda t: fn(t[1]))
            ext[key] = ["Vertex%d" % i, round(q.x, 2), round(q.y, 2)]
    # group equal circles: radius -> count
    groups = {}
    for c in circles:
        groups[c[1]] = groups.get(c[1], 0) + 1
    return {"view": v.Name, "scale": float(getattr(v, "Scale", 1.0)), "edges": len(_edges(v)), "vertices": len(verts),
            "circle_radii": sorted(([r, n] for r, n in groups.items()), key=lambda t: -t[0]),
            "circles": circles[:lim], "arcs": arcs[:lim // 2], "longest_lines": lines[:lim // 2], "extremes": ext}


# ------------------------------------------------------------------ reference resolution
def resolve_ref(view, ref, tol=0.05):
    """'Edge3' | {"circle": r, "center": [x, y]} | {"vertex": [x, y]} | {"line": [[x0,y0],[x1,y1]]}
    | {"extreme": "xmin|xmax|ymin|ymax"} -> subelement name or None."""
    if isinstance(ref, str):
        return ref
    tol = float(ref.get("tol", tol))
    if "circle" in ref:
        r = float(ref["circle"])
        c0 = ref.get("center")
        best = None
        for i, e in _edges(view):
            c = e.Curve
            if c.__class__.__name__ == "Circle" and abs(c.Radius - r) < max(tol, 1e-3):
                if c0 is None:
                    return "Edge%d" % i
                d = math.hypot(c.Center.x - c0[0], c.Center.y - c0[1])
                if d < max(0.2, tol) and (best is None or d < best[0]):
                    best = (d, "Edge%d" % i)
        return best[1] if best else None
    if "vertex" in ref:
        x, y = ref["vertex"]
        best = None
        for i, q in _vertices(view):
            d = math.hypot(q.x - x, q.y - y)
            if d < max(tol, 0.05) and (best is None or d < best[0]):
                best = (d, "Vertex%d" % i)
        return best[1] if best else None
    if "line" in ref:
        (x0, y0), (x1, y1) = ref["line"]
        for i, e in _edges(view):
            if e.Curve.__class__.__name__ in ("Line", "LineSegment") and len(e.Vertexes) == 2:
                a, b = e.Vertexes[0].Point, e.Vertexes[1].Point
                for (u, w) in ((a, b), (b, a)):
                    if abs(u.x - x0) < tol and abs(u.y - y0) < tol and abs(w.x - x1) < tol and abs(w.y - y1) < tol:
                        return "Edge%d" % i
        return None
    if "extreme" in ref:
        verts = _vertices(view)
        if not verts:
            return None
        k = ref["extreme"]
        fn = (lambda t: t[1].x) if k[0] == "x" else (lambda t: t[1].y)
        i, _q = (min if k.endswith("min") else max)(verts, key=fn)
        return "Vertex%d" % i
    raise ValueError("bad reference %r" % (ref,))


# ------------------------------------------------------------------ annotations
def _balloon(doc, page, view, text, origin, at, shape="Rectangle", end="Filled triangle", name="Balloon",
             kink=None):
    b = doc.addObject("TechDraw::DrawViewBalloon", name)
    b.SourceView = view
    page.addView(b)
    b.OriginX, b.OriginY = float(origin[0]), float(origin[1])
    b.X, b.Y = float(at[0]), float(at[1])
    b.Text = T(text)
    b.BubbleShape = shape
    b.EndType = end
    if kink is not None:
        b.KinkLength = float(kink)
    return b


def _font(obj, size=None):
    """Y14.2-size lettering (3.5 mm) and 2.5 mm arrows on balloons and dimensions."""
    vo = getattr(obj, "ViewObject", None)
    if vo is None:
        return
    for prop in ("Fontsize", "FontSize"):
        if hasattr(vo, prop):
            setattr(vo, prop, float(size or TEXT_H))
    if hasattr(vo, "Arrowsize"):
        vo.Arrowsize = 2.5


def _anchors(b, centres, r):
    """Every instance of the callout's feature: the layout attaches the leader to the best one."""
    try:
        if "Anchors" not in b.PropertiesList:
            b.addProperty("App::PropertyString", "Anchors", "Layout")
            b.addProperty("App::PropertyFloat", "AnchorR", "Layout")
        b.Anchors = ";".join("%.4f,%.4f" % (float(x), float(y)) for x, y in centres)
        b.AnchorR = float(r)
    except Exception:
        pass
    return b


def _label_for(kind, it, o):
    """Labels drive the layout engine: Datum_* / FCF_g<n> carry no free leader, '_g<n>' items move together."""
    if it.get("label"):
        return it["label"]
    g = it.get("group")
    if kind == "datum":
        return "Datum_%s" % it["letter"] + ("_g%s" % g if g is not None else "")
    if g is not None:
        if kind == "fcf" and not it.get("leader_to"):
            return "FCF_g%s" % g
        return "%s_g%s" % (it.get("name") or kind.capitalize(), g)
    return None


def _fcf_text(item):
    if item.get("cells"):
        cells = [SYM.get(c, T(c)) for c in item["cells"]]
    else:
        tol = T(item.get("tolerance", ""))
        if item.get("diameter") and not tol.startswith("⌀"):
            tol = "⌀" + tol
        if item.get("modifier"):
            tol += {"mmc": "Ⓜ", "lmc": "Ⓛ"}.get(item["modifier"], T(item["modifier"]))
        cells = [SYM.get(item["symbol"], T(item["symbol"])), tol] + list(item.get("datums", []))
    return "|".join(cells)


def html_lines(lines, size_pt=8, font="osifont"):
    body = "<br/>".join(T(l).replace("&", "&amp;").replace("<", "&lt;") for l in lines)
    return '<html><body style="font-family:%s; font-size:%dpt;">%s</body></html>' % (font, size_pt, body)


def annotate(p):
    doc = _doc(p["doc"])
    page = _obj(doc, p.get("page") or "Page")
    made, problems, lint_out = [], [], []
    pending_cosmetic = set()
    for n, it in enumerate(p["items"]):
        kind = it["kind"]
        view = _obj(doc, it["view"]) if it.get("view") else None
        name = it.get("name") or kind.capitalize()
        try:
            if kind == "datum":
                at = it.get("at") or [it["origin"][0], it["origin"][1] + 5]
                o = _balloon(doc, page, view, it["letter"], it["origin"], at, kink=0,   # straight leader, no shoulder
                             name=name if it.get("name") else "Datum_" + it["letter"])
            elif kind == "fcf":
                at = it["at"]
                if it.get("leader_to"):
                    o = _balloon(doc, page, view, _fcf_text(it), it["leader_to"], at, end=it.get("end", "Filled arrow"), name=name)
                else:
                    o = _balloon(doc, page, view, _fcf_text(it), at, at, end="None", name=name, kink=0)
            elif kind == "callout":
                o = _balloon(doc, page, view, it["text"], it["origin"], it["at"], shape="None",
                             end=it.get("end", "Filled arrow"), name=name)
            elif kind == "text":       # leaderless view-attached text line (e.g. 2nd callout line)
                o = _balloon(doc, page, view, it["text"], it["at"], it["at"], shape="None", end="None", name=name, kink=0)
            elif kind == "balloon":
                o = _balloon(doc, page, view, it["text"], it["origin"], it["at"], shape=it.get("shape", "Circular"),
                             end=it.get("end", "Filled arrow"), name=name)
            elif kind == "dimension":
                if view.Name in pending_cosmetic:
                    wait_views(doc, [view], 10)
                    pending_cosmetic.discard(view.Name)
                refs = [resolve_ref(view, r) for r in it["refs"]]
                if any(r is None for r in refs):
                    problems.append({"item": n, "name": name, "error": "unresolved reference(s) %s" % it["refs"]})
                    continue
                d = doc.addObject("TechDraw::DrawViewDimension", name)
                d.Type = it.get("type", "Distance")
                d.References2D = [(view, r) for r in refs]
                pts = []                               # remember WHERE the vertices are: TechDraw renumbers
                for r in refs:                         # them on recompute; rebind_dims() re-resolves them
                    if r.startswith("Vertex"):
                        try:
                            q = view.getVertexByIndex(int(r[6:])).Point
                            pts.append("%.5f,%.5f" % (q.x, q.y))
                        except Exception:
                            pass
                if pts and len(pts) == len(refs):
                    try:
                        d.addProperty("App::PropertyString", "RefPts", "Layout")
                        d.RefPts = ";".join(pts)
                    except Exception:
                        pass
                fmt = it.get("format")
                if fmt is None and R is not None and d.Type != "Angle":   # Y14.5 4.3.1 metric values
                    fmt = {"Diameter": "⌀", "Radius": "R"}.get(d.Type, "") + R.DIM_FORMAT
                if fmt:
                    d.FormatSpec = T(fmt)
                page.addView(d)
                if it.get("basic") and hasattr(d, "TheoreticalExact"):
                    d.TheoreticalExact = True
                if it.get("over_tolerance") is not None:
                    d.EqualTolerance = it.get("under_tolerance") is None
                    d.OverTolerance = float(it["over_tolerance"])
                    if it.get("under_tolerance") is not None:
                        d.UnderTolerance = float(it["under_tolerance"])
                if it.get("at") is not None:
                    d.X, d.Y = float(it["at"][0]), float(it["at"][1])
                o = d
            elif kind == "cosmetic_circle":
                tag = view.makeCosmeticCircle(_vec(it.get("center", [0, 0])), float(it["r"]))
                pending_cosmetic.add(view.Name)
                made.append({"kind": kind, "tag": tag})
                continue
            elif kind == "cosmetic_line":
                tag = view.makeCosmeticLine(_vec(it["p0"]), _vec(it["p1"]))
                pending_cosmetic.add(view.Name)
                made.append({"kind": kind, "tag": tag})
                continue
            elif kind == "centerline":
                refs = [resolve_ref(view, r) for r in it["refs"]]
                if any(r is None for r in refs):
                    problems.append({"item": n, "error": "unresolved reference(s)"})
                    continue
                tag = view.makeCenterLine(refs, int(it.get("mode", 0)))
                made.append({"kind": kind, "tag": tag})
                continue
            elif kind == "note":
                r = doc.addObject("TechDraw::DrawRichAnno", name)
                page.addView(r)
                r.AnnoText = it.get("html") or html_lines(it.get("lines", []), int(it.get("size_pt", 8)))
                r.MaxWidth = float(it.get("width", 120))
                r.ShowFrame = bool(it.get("frame", False))
                r.X, r.Y = float(it["x"]), float(it["y"])
                o = r
            else:
                problems.append({"item": n, "error": "unknown kind %r" % kind})
                continue
            if o.TypeId in ("TechDraw::DrawViewBalloon", "TechDraw::DrawViewDimension"):
                _font(o, it.get("font_size"))
            lab = _label_for(kind, it, o)
            if lab:
                o.Label = lab
            if it.get("anchors") and o.TypeId == "TechDraw::DrawViewBalloon":
                _anchors(o, it["anchors"], it.get("anchor_r", 0.0))
            if R is not None:
                texts = [str(o.Text)] if o.TypeId == "TechDraw::DrawViewBalloon" else                     ([T(x) for x in it.get("lines", [])] if kind == "note" else [])
                for tx in texts:
                    for issue in R.lint_text(tx):
                        lint_out.append({"item": n, "name": o.Name, "issue": issue})
            made.append({"kind": kind, "name": o.Name, "label": o.Label})
        except Exception as exc:
            problems.append({"item": n, "name": name, "error": "%s: %s" % (type(exc).__name__, exc)})
    doc.recompute()
    res = {"created": made, "problems": problems, "text_lint": lint_out}
    if p.get("layout") and L is not None:
        lay = run_layout(doc, page, p)
        res["layout"] = lay["report"]
        res["conflicts"] = lay["conflicts"]
    return res


# ------------------------------------------------------------------ export
def _close_page_window(page):
    try:
        import FreeCADGui as Gui
        from PySide import QtGui  # noqa: F401
        mw = Gui.getMainWindow()
        from PySide import QtWidgets
        area = mw.findChild(QtWidgets.QMdiArea)
        for w in area.subWindowList():
            if w.windowTitle().startswith(page.Label) or w.windowTitle().startswith(page.Name):
                w.close()
    except Exception:
        pass


def export_page(p):
    import FreeCADGui as Gui
    import TechDrawGui
    doc = _doc(p["doc"])
    page = _obj(doc, p.get("page") or "Page")
    doc.recompute()
    Gui.ActiveDocument = Gui.getDocument(doc.Name)
    page.ViewObject.doubleClicked()          # the template is drawn only while the page is open
    for _ in range(int(p.get("settle_loops", 6))):
        Gui.updateGui()
        time.sleep(0.2)
    out_dir = p["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    stem = p.get("stem") or doc.Name
    res = {"doc": doc.Name, "page": page.Name}
    if p.get("pdf", True):
        pdf = os.path.join(out_dir, stem + ".pdf")
        TechDrawGui.exportPageAsPdf(page, pdf)
        res["pdf"] = pdf
    if p.get("svg"):
        svg = os.path.join(out_dir, stem + ".svg")
        TechDrawGui.exportPageAsSvg(page, svg)
        res["svg"] = svg
    if p.get("save_fcstd"):
        fc = os.path.join(out_dir, stem + ".FCStd")
        doc.saveAs(fc)
        res["fcstd"] = fc
    if p.get("close_document"):
        App.closeDocument(doc.Name)
    elif p.get("close_view", False):
        _close_page_window(page)
    return res


def close_document(p):
    if p["doc"] in App.listDocuments():
        App.closeDocument(p["doc"])
        return True
    return False



# ------------------------------------------------------------------ layout (overlap-free annotations) + lint
def _open_page(doc, page, loops=6):
    """TechDraw builds the Qt scene (and draws the template) only while the page is open."""
    import FreeCADGui as Gui
    Gui.ActiveDocument = Gui.getDocument(doc.Name)
    page.ViewObject.doubleClicked()
    for _ in range(int(loops)):
        Gui.updateGui()
        time.sleep(0.2)


def _configure_layout(page, p):
    tpl = page.Template
    W = float(getattr(tpl, "Width", 431.8))
    H = float(getattr(tpl, "Height", 279.4))
    return L.configure(W, H, p.get("frame"), p.get("keepout"))


def _classify(doc, remaining):
    """layout.collisions() pairs -> label / leader / dimension-line conflicts."""
    out = {"labels": [], "leaders": [], "dimension_lines": []}
    for name, why in remaining:
        o = doc.getObject(name)
        row = {"object": name, "label": o.Label if o is not None else name, "issue": why}
        if why.startswith("leader crosses leader") or why.startswith("leader through"):
            out["leaders"].append(row)
        elif why.startswith("leader crosses "):
            out["dimension_lines"].append(row)        # across a dimension or extension line
        else:
            out["labels"].append(row)                 # label on label / view / template, outside frame
    out["count"] = len(remaining)
    return out


def run_layout(doc, page, p=None):
    """Datum symbols onto their FCFs, then the retrying overlap-free placement; re-measured conflicts."""
    p = p or {}
    refresh_views(doc)                            # HLR at the current scale before anything is measured
    _open_page(doc, page)
    _configure_layout(page, p)
    L._refresh(doc)
    rebound = rebind_dims(doc)                    # vertex dimensions back onto their recorded points
    L._refresh(doc)
    snapped = L.snap_datums(doc, page)            # first, so the layout moves each finished group as one unit
    L.snap_allover(doc, page)                     # ALL OVER under the profile FCF, before layout too
    report, _rem = L.best_layout(doc, page, time_budget=float(p.get("layout_budget_s", 240)))
    remaining = L.collisions(L.measure(doc, page))
    return {"report": report, "datums_snapped": snapped, "dims_rebound": rebound,
            "conflicts": _classify(doc, remaining)}


def _datum_check(doc):
    """Every datum letter referenced in an FCF is defined on the sheet; no FCF references its own datum."""
    texts = {o.Label: str(o.Text) for o in doc.Objects if o.TypeId == "TechDraw::DrawViewBalloon"}
    defined = {v.strip() for k, v in texts.items() if k.startswith("Datum_")}
    used, selfref = set(), []
    for k, v in texts.items():
        if k.startswith("FCF_"):
            used |= {c.strip() for c in v.split("|")[2:] if len(c.strip()) == 1 and c.strip().isalpha()}
    for k, v in texts.items():
        if k.startswith("Datum_") and "_g" in k:
            f = texts.get("FCF_g" + k.rsplit("_g", 1)[1], "")
            if v.strip() in [c.strip() for c in f.split("|")[2:]]:
                selfref.append(v.strip())
    return {"undefined": sorted(used - defined), "self_referenced": sorted(set(selfref))}


def _text_lint(doc):
    out = []
    for o in doc.Objects:
        if o.TypeId == "TechDraw::DrawViewBalloon":
            for issue in R.lint_text(str(o.Text)):
                out.append({"object": o.Name, "label": o.Label, "issue": issue})
    return out


def lint(p):
    """Read-only sheet check: layout.collisions on the rendered page (no objects are moved or saved)."""
    import FreeCADGui as Gui
    opened = False
    if p.get("path"):
        path = p["path"]
        hit = [d for d in App.listDocuments().values() if os.path.normcase(os.path.abspath(d.FileName or "")) ==
               os.path.normcase(os.path.abspath(path))]
        if hit:
            doc = hit[0]
        else:
            doc = App.openDocument(path)
            opened = True
            doc.recompute()                           # rebuild view geometry of the freshly opened copy
    else:
        doc = _doc(p["doc"])
    pages = [o for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"]
    page = _obj(doc, p["page"]) if p.get("page") else (pages[0] if pages else None)
    if page is None:
        raise ValueError("no TechDraw page in %s" % doc.Name)
    try:
        _open_page(doc, page)
        for _ in range(4):
            Gui.updateGui()
            time.sleep(0.15)
        _configure_layout(page, p)
        items = L.measure(doc, page)
        res = {"doc": doc.Name, "page": page.Name, "measured": len(items),
               "frame": [round(v, 1) for v in L.FRAME], "keepout": [[round(v, 1) for v in b] for b in L.TEMPLATE],
               "conflicts": _classify(doc, L.collisions(items)), "datums": _datum_check(doc),
               "text": _text_lint(doc)}
        res["views_outside_frame"] = L.views_outside_frame(doc, page)
        res["clean"] = (res["conflicts"]["count"] == 0 and not res["datums"]["undefined"] and
                        not res["datums"]["self_referenced"] and not res["views_outside_frame"])
        return res
    finally:
        if opened and p.get("close", True):
            App.closeDocument(doc.Name)


# ------------------------------------------------------------------ one-call part drawing
# Ported from the light_weight_arm batch generator (cad/drawings/batch_drawings.py + make_drawings.py,
# synced 2026-11-15 after three independent drawing reviews).  Feature recognition is the headless module
# fcmcp_features (F); project specifics (process table, extra notes, name rules) come in p["config"].
import re as _re

SCALES = [10, 5, 4, 3, 2, 1.5, 1, 0.75, 0.5, 0.4, 0.3, 0.25, 0.2, 0.1, 0.05]
DIA, MMC, CBORE, DEPTH, CSK = "⌀", "Ⓜ", "⌴", "↧", "⌵"
# Built-in process profiles.  Flags: mmc (position at MMC per fastener rules), fits ("h7": H7 on bearing
# seats + dowel holes | "ream": bearing seats finished REAM/BORE | None), tapped (hole radius -> thread),
# projected (projected tolerance zone on tapped holes), journals (k6 journal callouts), two_view, hidden,
# shaft (datum A on the diameter, h6 diameter), flatness (FCF on the datum face), insert_d (heat-set insert
# hole diameter), density (g/cm3).  Notes may use the tokens @THREADS @MASK @INSERT @SHAFT @SHIMMAT @CSK
# @THK {step} {dxf}; "; HOLES MARKED REAM/BORE..." is dropped when the part has no finished holes.
TAPPED_DEFAULT = {"1.25": "M3x0.5-6H", "1.40": "M3x0.5-6H", "1.70": "M4x0.7-6H", "2.10": "M5x0.8-6H"}
PROCESS = {
    "machined": dict(title2="", title3="", pos="0.05", prof="0.1", tol={}, mmc=True, fits="h7",
                     tapped=TAPPED_DEFAULT, projected=True, journals=True, two_view=False, hidden=True, density=2.70,
                     notes=["@MASK", "BREAK ALL SHARP EDGES 0.2-0.4.", "@THREADS"]),
    "plate": dict(title2="", title3="", pos="0.1", prof="0.2", tol={}, mmc=True, fits="ream", tapped={},
                  projected=False, journals=False, two_view=True, hidden=False, density=1.55,
                  notes=["CUT PROFILE AND THRU HOLES FROM {dxf} (1:1, mm); HOLES MARKED REAM/BORE: CUT 0.3 UNDERSIZE "
                         "ON DIA, THEN FINISH TO THE CALLOUT."]),
    "printed": dict(title2="", title3="", pos="0.2", prof="0.3", tol={}, mmc=False, fits="ream", tapped={},
                    projected=False, journals=False, two_view=False, hidden=True, density=1.30, insert_d=4.0,
                    notes=["PRINT FROM STEP {step}; HOLES MARKED REAM/BORE: PRINT 0.3 UNDERSIZE ON DIA, THEN FINISH TO "
                           "THE CALLOUT.", "@INSERT"]),
    "shaft": dict(title2="", title3="", pos="0.05", prof="0.05", tol={}, mmc=False, fits=None, tapped={},
                  projected=False, journals=False, two_view=False, hidden=True, shaft=True, density=7.85,
                  notes=["@SHAFT"]),
    "shim": dict(title2="", title3="", pos="0.05", prof="0.1", tol={}, mmc=False, fits=None, tapped={},
                 projected=False, journals=False, two_view=True, hidden=False, flatness="0.02", density=7.9,
                 notes=["@SHIMMAT"]),
}
MATE_CLEAR = {"M3": 3.3, "M4": 4.5, "M5": 5.5}


def scale_txt(sc):
    from fractions import Fraction
    f = Fraction(sc).limit_denominator(100)
    return "%d:%d" % (f.numerator, f.denominator)


def pick_scale(w, h, max_w, max_h, scales=SCALES):
    for sc in scales:
        if w * sc <= max_w and h * sc <= max_h:
            return sc
    return scales[-1]


def _F():
    import fcmcp_features
    return fcmcp_features


def oriented(shape):
    return _F().oriented(shape)


def hole_table(shape):
    return _F().hole_table(shape)


def process_profile(name, config=None):
    """Built-in profile, overlaid with config["processes"][name] (a config may also add new processes)."""
    cfg = (config or {}).get("processes") or {}
    base = dict(PROCESS.get(name) or PROCESS.get((cfg.get(name) or {}).get("base", "machined")) or PROCESS["machined"])
    base.update(cfg.get(name) or {})
    return base


def _tapped_thread(h, tapped):
    tapped = tapped or {}
    for k in ("%.2f" % h["r"], "%g" % round(h["r"], 2), "%.1f" % h["r"]):
        if k in tapped:
            return tapped[k]
    return None


def _override(h, name, t, config):
    """config["hole_overrides"]: [{"names": regex, "diameter": d, "tol": 0.1, "text": "..."}]; text tokens
    {t} (part thickness) and {tap_depth} (min(8, max(t/2 - 2, 4)): tapped from both ends of a rod)."""
    d = 2 * h["r"]
    for ov in (config or {}).get("hole_overrides") or []:
        if ov.get("names") and not _re.search(ov["names"], name or ""):
            continue
        if abs(d - float(ov["diameter"])) <= float(ov.get("tol", 0.1)):
            return T(ov["text"]).format(t=R.mm(t, 1), tap_depth=R.mm(min(8.0, max(t / 2 - 2.0, 4.0)), 1))
    return None


def hole_text(h, tapped=None, process="machined", fits=None, proc=None, plain=False, t=0.0, name="", config=None):
    """Y14.5 hole callout text (metric values per 4.3.1, fits as the tolerance symbol only per 5.2.1):
    thread or diameter, fit, D-bore, THRU / depth / EACH SIDE, counterbore (+ FAR SIDE), countersink with
    its included angle (+ FAR SIDE, 4.5.12)."""
    proc = proc or process_profile(process)
    if fits is True:
        fits = proc.get("fits") or "h7"
    elif fits is None:
        fits = proc.get("fits")
    if plain:
        fits, tapped = None, {}
    elif tapped is None:
        tapped = proc.get("tapped") or {}
    ov = _override(h, name, t, config)
    if ov:
        return ov
    d = 2 * h["r"]
    thr = _tapped_thread(h, tapped)
    if thr:
        if h["thru"]:
            return "%s THRU" % thr
        return "%s %s%s%s" % (thr, DEPTH, R.mm(max(h["depth"] - 1.5, 1.0), 1),   # >= 3P tap runout
                              " EACH SIDE" if h.get("each_side") else "")
    s = DIA + R.mm(d, 2)
    cb_seat = bool(h.get("cbore")) and fits and R.bearing_seat(h["cbore"][0], True)
    if fits == "h7" and not cb_seat and (R.bearing_seat(d, True) or R.dowel_hole(d)):
        s += " " + R.fit_text(d, True)
    elif fits == "ream" and not cb_seat and R.bearing_seat(d, True) and not h.get("dflat"):
        s += " " + R.fit_text(d, True) + (" REAM" if d <= 20 else " BORE")     # a reamer cannot finish a D-bore
    if proc.get("insert_d") and abs(d - float(proc["insert_d"])) < 0.05:
        s += " INSERT"
    if h.get("dflat"):
        s += " D-BORE, FLAT %s FROM ℄" % R.mm(h["dflat"], 2)
    s += " THRU" if h["thru"] else " %s%s%s" % (DEPTH, R.mm(h["depth"], 1), " EACH SIDE" if h.get("each_side") else "")
    if h.get("cbore"):
        s += "  %s%s%s%s %s%s%s" % (CBORE, DIA, R.mm(h["cbore"][0], 1),
                                 (" " + R.fit_text(h["cbore"][0], True)) if cb_seat else "", DEPTH, R.mm(h["cbore"][1], 1),
                                 " FAR SIDE" if h.get("cb_far") else "")
    if h.get("csk"):
        cd, ang, far = h["csk"]
        s += "  %s%s%s X %s°%s" % (CSK, DIA, R.mm(cd, 1), R.mm(round(ang), 0), " FAR SIDE" if far else "")
    return s


def _view_k(view):
    """Factor between getVisible/HiddenEdges (page units: x scale, y flipped) and getEdgeByIndex (model
    units), derived from the data itself rather than trusted from view.Scale."""
    sc_ = float(getattr(view, "Scale", 1.0)) or 1.0
    try:
        ve_, ie_ = view.getVisibleEdges() or [], [e for _i, e in _edges(view)]
        if ve_ and ie_:
            wi = max(e.BoundBox.XMax for e in ie_) - min(e.BoundBox.XMin for e in ie_)
            if wi > 1e-9:
                sc_ = (max(e.BoundBox.XMax for e in ve_) - min(e.BoundBox.XMin for e in ve_)) / wi
    except Exception:
        pass
    return sc_


def full_circles(view, hidden_ok=False):
    """Holes as TechDraw draws them: arcs grouped by (centre, radius) whose spans add up to a full circle
    (split circles merged; slot-end half arcs excluded).  hidden_ok adds circles drawn only hidden (FAR SIDE
    features), converted from page units; otherwise hidden-only circles are dropped."""
    import Part
    acc = {}
    for i, e in _edges(view):
        c = e.Curve
        if c.__class__.__name__ != "Circle":
            continue
        k = (round(c.Center.x, 3), round(c.Center.y, 3), round(c.Radius, 3))
        a = acc.setdefault(k, [i, c, 0.0])
        a[2] += abs(e.LastParameter - e.FirstParameter)
    out = [(i, c) for (i, c, span) in acc.values() if span >= 1.5 * math.pi]
    if hidden_ok:
        try:
            sc_ = _view_k(view)
            seen_ = {(round(c.Center.x, 2), round(c.Center.y, 2), round(c.Radius, 2)) for _i, c in out}
            hacc = {}
            for e in view.getHiddenEdges() or []:
                c = e.Curve
                if c.__class__.__name__ != "Circle":
                    continue
                k_ = (round(c.Center.x / sc_, 2), round(-c.Center.y / sc_, 2), round(c.Radius / sc_, 2))
                hacc[k_] = hacc.get(k_, 0.0) + abs(e.LastParameter - e.FirstParameter)
            for k_, span in hacc.items():
                if span >= 1.5 * math.pi and k_ not in seen_:
                    seen_.add(k_)
                    out.append((-1, Part.Circle(V(k_[0], k_[1], 0), V(0, 0, 1), k_[2])))
        except Exception:
            pass
        return out
    return [ic for ic in out if not _hidden_circle(view, ic[1])]   # never point a leader at a hidden line


def _circle_keys(edges, sc=1.0):
    ks = set()
    for e in edges or []:
        c = getattr(e, "Curve", None)
        if c is not None and c.__class__.__name__ == "Circle":
            for sy in (1, -1):
                ks.add((round(c.Center.x / sc, 1), round(sy * c.Center.y / sc, 1), round(c.Radius / sc, 1)))
    return ks


def _hidden_circle(view, c):
    """True if this circle is drawn only as a hidden line (far-face hole seen through the part)."""
    try:
        sc_ = _view_k(view)
        vis, hid = _circle_keys(view.getVisibleEdges(), sc_), _circle_keys(view.getHiddenEdges(), sc_)
    except Exception:
        return False
    if not vis and not hid:
        return False
    k = (round(c.Center.x, 1), round(c.Center.y, 1), round(c.Radius, 1))
    return k in hid and k not in vis


def _real_extreme(view, axis, lo):
    """Extreme REAL vertex (an edge endpoint) - TechDraw also lists arc/circle centres as vertices."""
    ends = []
    for _j, e_ in _edges(view):
        for q_ in (e_.Vertexes[0].Point, e_.Vertexes[-1].Point):
            ends.append((q_.x, q_.y))
    best, bv = None, None
    for i, q in _vertices(view):
        if not any(abs(q.x - ex) < 1e-4 and abs(q.y - ey) < 1e-4 for ex, ey in ends):
            continue
        val = q.x if axis == 0 else q.y
        if best is None or (val < best if lo else val > best):
            best, bv = val, "Vertex%d" % i
    return bv, best


def _part_shape(p):
    import Part
    if p.get("brep"):                       # .brep, or .step/.stp/.iges via Part.Shape.read
        shape = Part.Shape()
        if p["brep"].lower().endswith((".brep", ".brp")):
            shape.importBrep(p["brep"])
        else:
            shape.read(p["brep"])
        return shape
    sdoc = _doc(p["source_doc"])
    return Part.getShape(_obj(sdoc, p["object"]), "", needSubElement=False, refine=False, transform=False)


def _own(cs, hs):
    """Circles at THIS group's hole positions (front view: x = X, y = +/-Y), not any circle of that radius."""
    for sy in (1, -1):
        m_ = [c for c in cs if any(abs(c.Center.x - h_["x"]) < 0.1 and abs(c.Center.y - sy * h_["y"]) < 0.1 for h_ in hs)]
        if m_:
            return m_
    return []


def part_drawing(p):  # noqa: C901 - one sheet, top to bottom
    """Complete reduced-dimension part drawing from one shape (see the MCP tool docstring)."""
    F = _F()
    config = p.get("config") or {}
    if p.get("config_path"):
        import json as _json
        with open(p["config_path"], encoding="utf-8") as fh:
            config = dict(_json.load(fh), **config)
    proc_name = p.get("process") or "machined"
    proc = process_profile(proc_name, config)
    if p.get("tapped") is not None:
        proc["tapped"] = p["tapped"]
    if p.get("fits") is not None:                     # False: no fits; True: the process default (or H7); or "h7"/"ream"
        proc["fits"] = (proc.get("fits") or "h7") if p["fits"] is True else (p["fits"] or None)
    bearings = dict(config.get("bearings") or {}, **(p.get("bearings") or {}))
    if bearings:
        R.BEARINGS.update({k: tuple(v) for k, v in bearings.items()})
        R.BEARING_OD[:] = sorted({D for _d, D in R.BEARINGS.values()})
        R.BEARING_ID[:] = sorted({d for d, _D in R.BEARINGS.values()})
    R.set_proj_heights(dict(config.get("projected_heights") or {}, **(p.get("projected_heights") or {})))
    part_name = p.get("part_name") or p.get("stem") or p["doc"]
    plain = bool(config.get("plain_names") and _re.search(config["plain_names"], part_name))
    warnings = []
    shape = _part_shape(p)
    if p.get("orient", True):
        shape = F.oriented(shape)
    bb = shape.BoundBox
    Lx, Ly, Lz = bb.XLength, bb.YLength, bb.ZLength
    density = float(p.get("density_g_cm3") or proc.get("density") or 0)
    mass = shape.Volume * density / 1000.0 if density else None
    fields = dict(proc.get("tol") or {})
    for k_, f_ in (("DrawingTitle2", "title2"), ("DrawingTitle3", "title3")):
        fields[k_] = proc.get(f_) or ""               # never leave the template's "Drawing Title 2" placeholder
    fields.update(p.get("fields") or {})
    if mass is not None and "Weight" not in fields:
        fields["Weight"] = R.mm(mass, 1) + " g"
    info = create_page({"doc": p["doc"], "template": p.get("template") or config.get("template") or "ANSIB_Landscape",
                        "fields": fields, "replace": True})
    doc = App.getDocument(info["doc"])
    page = doc.getObject(info["page"])
    W, H = info["size_mm"]
    kx, ky = W / 431.8, H / 279.4                     # layout tuned on ANSI B, scaled to the sheet
    L.configure(W, H, p.get("frame"), p.get("keepout"))
    part = doc.addObject("Part::Feature", "Part")
    part.Shape = shape
    if part.ViewObject:
        part.ViewObject.Visibility = False
    thin = Lz < 0.15 * Ly                             # plate-like: a top view would be a sliver
    projections = p.get("projections")
    if projections is None:
        projections = ["Right"] if (proc.get("two_view") or thin) else ["Right", "Top"]
    three = "Top" in projections
    sc = float(p["scale"]) if p.get("scale") else pick_scale(
        Lx + Lz + (40 if three else 25), Ly + (Lz + 30 if three else 0), 215.0 * kx, (140.0 if three else 110.0) * ky)
    hidden = bool(p.get("hidden", proc.get("hidden", True)))
    pg = doc.addObject("TechDraw::DrawProjGroup", "ProjGroup")
    page.addView(pg)
    pg.Source = [part]
    pg.ProjectionType = p.get("projection_type", "Third angle")
    front = pg.addProjection("Front")
    pg.Anchor.Direction = V(0, 0, 1)
    pg.Anchor.XDirection = V(1, 0, 0)
    subs = {"Front": front}
    for pr in projections:
        if pr != "Front":
            subs[pr] = pg.addProjection(pr)
    side, topv = subs.get("Right"), subs.get("Top")
    for v in pg.Views:                                # hidden lines ON before the first compute, or
        v.HardHidden = hidden                         # getHiddenEdges() stays empty

    def _set_scale(val):                              # every item at the sheet scale, not just the anchor,
        pg.ScaleType = "Custom"                       # and touched: items do not re-run HLR on a scale change
        pg.Scale = val
        for v_ in pg.Views:
            if hasattr(v_, "ScaleType"):
                v_.ScaleType = "Custom"
            v_.Scale = val
            v_.touch()
        doc.recompute()
    _set_scale(sc)
    for prop, val in (("AutoDistribute", True), ("spacingX", 35.0), ("spacingY", 35.0)):
        if hasattr(pg, prop):
            setattr(pg, prop, val)
    pg.X, pg.Y = (150 * kx, 150 * ky) if three else (120 * kx, 165 * ky)   # 3-view: room left of the front view
    views = list(pg.Views)
    iso, isc = None, None
    if p.get("iso", True):
        iso = doc.addObject("TechDraw::DrawViewPart", "Iso")
        iso.Source = [part]
        iso.Direction = V(1, -1, 1.2)
        isc = pick_scale(max(Lx, Ly, Lz) * 1.15, max(Lx, Ly, Lz) * 1.0, 70 * kx, 50 * ky)   # iso shows all extents
        place(page, iso, 372 * kx, 232 * ky, isc)
        iso.Label = "Iso"
        views.append(iso)
    wait_views(doc, views, float(p.get("wait", 40)))
    grp_views = list(pg.Views)
    if not p.get("scale"):                           # any projection-group view outside the frame -> smaller scale
        for _ in range(5):
            refresh_views(doc, grp_views + ([iso] if iso else []), 20)   # measured: refresh like export does
            _open_page(doc, page, 2)
            L._refresh(doc)
            out = [k for k in L.views_outside_frame(doc, page) if not k.startswith("Iso")]
            if not out or sc == SCALES[-1]:
                break
            sc = SCALES[SCALES.index(sc) + 1] if sc in SCALES else pick_scale(1, 1, sc * 0.99, sc * 0.99)
            _set_scale(sc)
            wait_views(doc, grp_views, 20)
    tpl = page.Template
    et = dict(tpl.EditableTexts)
    if "scale" in et and "scale" not in (p.get("fields") or {}):
        et["scale"] = scale_txt(sc)
        tpl.EditableTexts = et
    wait_views(doc, grp_views, 20)                    # settled geometry before reading vertices

    # stale-geometry guard: a view's x-extent must equal the part's true extent (front/top: Lx, side: Lz);
    # geometry left over from an earlier scale is stable, so polling for "no change" cannot catch it
    def _xext(v):
        bbs = [e.BoundBox for _i, e in _edges(v)]
        return (max(b.XMax for b in bbs) - min(b.XMin for b in bbs)) if bbs else 0.0
    want = [(front, Lx)] + ([(side, Lz)] if side is not None else []) + ([(topv, Lx)] if topv is not None else [])
    for _try in range(6):
        bad_ = [v for v, L_ in want if abs(_xext(v) - L_) > 0.02 * L_ + 0.05]
        if not bad_:
            break
        refresh_views(doc, bad_, 20)
    else:
        warnings.append("view geometry did not settle to the part extents (stale HLR)")

    items = []
    fv = front.Name
    # ---------------- overall size dims (front: X and Y extents, side: thickness)
    vxl, xl = _real_extreme(front, 0, True)
    vxh, xh = _real_extreme(front, 0, False)
    vyl, yl = _real_extreme(front, 1, True)
    vyh, yh = _real_extreme(front, 1, False)
    ebb = [e_.BoundBox for _i, e_ in _edges(front)]
    if not ebb:
        raise RuntimeError("front view has no geometry")
    exl, exh = min(b.XMin for b in ebb), max(b.XMax for b in ebb)
    eyl, eyh = min(b.YMin for b in ebb), max(b.YMax for b in ebb)
    vert_ok = bool(vxl and vyl) and (xh - xl) > 0.9 * (exh - exl) and (yh - yl) > 0.9 * (eyh - eyl)
    xl, xh, yl, yh = exl, exh, eyl, eyh               # true outline extents (a circle has only seam vertices)
    circles = full_circles(front)                     # holes only (split arcs merged), not slot-end arcs
    circles_all = full_circles(front, hidden_ok=True)
    big = max(circles, key=lambda ic: ic[1].Radius) if circles else None
    ext = max(xh - xl, yh - yl, 1e-6)
    round_part = big is not None and 0.9 * ext <= 2 * big[1].Radius <= 1.05 * ext
    want_dims = p.get("overall_dims", True)
    shaft = bool(proc.get("shaft"))
    REF = "(%.1w)"                                    # reference sizes: parentheses, 1 decimal
    if round_part:
        cx_, cy_, rr = big[1].Center.x, big[1].Center.y, big[1].Radius
        xl, xh, yl, yh = cx_ - rr, cx_ + rr, cy_ - rr, cy_ + rr
        if want_dims:
            hc_ = [cc for (_i, cc) in circles_all if cc.Radius < rr - 0.05]

            def _clear(th):
                return all(abs(-math.sin(th) * (cc.Center.x - cx_) + math.cos(th) * (cc.Center.y - cy_)) >
                           cc.Radius + 1.0 / sc for cc in hc_)
            ths = sorted((math.radians(a_) for a_ in range(0, 180, 3)), key=lambda th: abs(th - math.radians(45)))
            th_ = next((th for th in ths if _clear(th)), math.radians(45))   # the diameter line crosses no hole
            items.append(dict(kind="dimension", view=fv, refs=["Edge%d" % big[0]], type="Diameter",
                              format=DIA + R.DIM_FORMAT, name="Dim_D",
                              at=[(cx_ + math.cos(th_) * rr) * sc + math.cos(th_) * 14,
                                  (cy_ + math.sin(th_) * rr) * sc + math.sin(th_) * 14]))

    def vpt(nm):
        return front.getVertexByIndex(int(nm[6:])).Point
    skipped = []
    dimw_x = None                                     # model-x of the width/diameter dimension line
    # extension lines on the side where they are shortest; if they would run across most of the part either
    # way, the envelope is given as a REF note instead (the profile is basic, from the model)
    if want_dims and not round_part and vxl and vxh and vert_ok:
        pa, pb = vpt(vxl), vpt(vxh)
        below, above = max(pa.y - yl, pb.y - yl), max(yh - pa.y, yh - pb.y)
        if min(below, above) > 0.25 * (yh - yl) + 2.0:
            skipped.append("L")
        else:
            yoff = -(yh - yl) * sc / 2 - 14 if below <= above else (yh - yl) * sc / 2 + 14
            items.append(dict(kind="dimension", view=fv, refs=[vxl, vxh], type="DistanceX", format=REF,
                              at=[0, yoff], name="Dim_L"))
    if want_dims and not round_part and not vert_ok:
        skipped += ["L", "W"]
    if want_dims and vyl and vyh and not round_part and vert_ok:
        pa, pb = vpt(vyl), vpt(vyh)
        right, left = max(xh - pa.x, xh - pb.x), max(pa.x - xl, pb.x - xl)
        if min(right, left) > 0.25 * (xh - xl) + 2.0:
            skipped.append("W")
        else:
            xoff = ((xh - xl) * sc / 2 + 12) if right <= left else (-(xh - xl) * sc / 2 - 14)
            items.append(dict(kind="dimension", view=fv, refs=[vyl, vyh], type="DistanceY", at=[xoff, 0], name="Dim_W",
                              format=(DIA + R.DIM_FORMAT + " h6") if shaft else REF))
            dimw_x = (xh + 12.0 / sc) if right <= left else (xl - 14.0 / sc)
    datums = p.get("datums", True)
    sxl = sxh = None
    sxlv = sxhv = 0.0
    if side is not None:
        sxl, sxlv = _real_extreme(side, 0, True)
        sxh, sxhv = _real_extreme(side, 0, False)
    if sxl and sxh and abs(sxhv - sxlv) > 0.05 and not shaft:   # shafts: seen end-on -> datum A = axis
        tw_ = abs(sxhv - sxlv) * sc
        if want_dims:
            items.append(dict(kind="dimension", view=side.Name, refs=[sxl, sxh], type="DistanceX",
                              format=R.DIM_FORMAT, at=[(tw_ / 2 + 7.0) if tw_ < 12.0 else 0, -(yh - yl) * sc / 2 - 12],
                              name="Dim_T"))
        cand_ = []
        for _j, e_ in _edges(side):
            p0_, p1_ = e_.Vertexes[0].Point, e_.Vertexes[-1].Point
            if abs(p0_.x - sxhv) < 0.02 and abs(p1_.x - sxhv) < 0.02 and abs(p1_.y - p0_.y) > 0.5:
                cand_.append((abs(p1_.y - p0_.y), (p0_.y + p1_.y) / 2))
        ya_ = max(cand_)[1] if cand_ else 0.0          # midpoint of the longest edge on the datum face
        k_ = abs(sxhv - sxlv) / Lz if Lz > 1e-6 else 1.0  # view-geometry units per model mm (side x-extent = Lz):
        k_ = k_ if k_ > 1e-6 else 1.0                    # balloon origins are model mm, never edge coordinates
        if datums:
            items.append(dict(kind="datum", view=side.Name, letter="A", origin=[sxhv / k_, ya_ / k_],
                              at=[sxhv / k_ + 10 / sc, ya_ / k_]))
        if proc.get("flatness"):                       # flatness as an FCF on the datum face, not a note
            yf_ = ya_ / k_ + (yh - yl) * 0.3
            items.append(dict(kind="fcf", view=side.Name, cells=["flatness", str(proc["flatness"])],
                              leader_to=[sxhv / k_, yf_], at=[sxhv / k_ + 16.0 / sc, yf_ + 6.0 / sc], name="FCF_flat",
                              label="FCF_flat"))
    elif datums:
        # round part / shaft seen end-on: datum A = axis of the diameter, symbol on the end of the diameter's
        # dimension line (aligned with the dimension line -> the feature of size / its axis)
        xd = dimw_x if dimw_x is not None else xh + 12.0 / sc
        da = dict(kind="datum", view=fv, letter="A", origin=[xd, yh + 3.0 / sc], at=[xd, yh + 12.0 / sc])
        if dimw_x is not None:                        # moves with the dimension during layout (same group)
            for it in items:
                if it.get("name") == "Dim_W":
                    it["label"] = "Dim_W_g90"
            da["label"] = "Datum_A_g90"
        items.append(da)

    # ---------------- holes: callouts + position FCFs (DRF A|B|C)
    holes, side_holes, t = F.hole_table(shape) if p.get("hole_callouts", True) else ([], [], Lz)
    side_holes_all = list(side_holes)
    pos_floor = str(p.get("position_tolerance") or proc["pos"])

    def htext(h):
        return hole_text(h, proc.get("tapped"), proc_name, proc.get("fits"), proc, plain, t, part_name, config)

    def tol_cell(d_, txt):
        tp = txt.startswith("M") or txt.startswith("Tr")
        thread = txt.split("x")[0].split()[0] if tp else None
        if proc.get("fits") == "h7" and not plain and R.bearing_seat(d_, True):
            tv, mmc_ = "0.02", False                       # bearing seat: RFS, tight
        elif proc.get("mmc"):
            tv, mmc_ = R.position_tol(d_, proc_name, tapped=tp, floor=float(pos_floor),
                                      mate_clear=MATE_CLEAR.get(thread) if tp else None)
        else:
            tv, mmc_ = pos_floor, False                    # process tolerance, RFS
        cell = DIA + tv + (MMC if mmc_ else "")
        if tp and proc.get("projected") and thread in MATE_CLEAR:   # Y14.5 App. B-4 / 10.4.1: fixed fastener
            ph = R.proj_height(part_name, thread)
            if ph is None:
                ph = R.PROJ_H_DEFAULT
                w_ = "no projected height for %s %s holes: default %s mm used (pass projected_heights)" % (
                    part_name, thread, R.mm(ph))
                if w_ not in warnings:
                    warnings.append(w_)
            cell += " " + R.PROJ + R.mm(ph)
        return cell

    groups = {}
    for h in holes:
        groups.setdefault(htext(h), []).append(h)
    order = sorted(groups.items(), key=lambda kv: -kv[1][0]["r"])
    max_c = int(p.get("max_callouts", 7))
    tg = []
    for gi, (txt, hs) in enumerate(order[:max_c]):
        r = hs[0]["r"]
        cand = _own([c for (_i, c) in circles if abs(c.Radius - r) < 0.02], hs)
        if not cand and all(h_.get("far") for h_ in hs):  # blind from the far face: shown hidden -> FAR SIDE
            cand = _own([c for (_i, c) in circles_all if abs(c.Radius - r) < 0.02], hs)
            if cand:
                txt = txt + " FAR SIDE"
        if not cand:
            continue
        c = max(cand, key=lambda c: c.Center.x + 0.3 * c.Center.y)        # instance nearest the text column
        tg.append((gi, txt, hs, r, c))
    called = [x[0] for x in tg]
    farside = [(t_, h_) for gi_, (t_, h_) in enumerate(order[:max_c]) if gi_ not in called]
    over = order[max_c:]
    for t_, h_ in farside:
        warnings.append("hole group %dX %s has no callout (no circle in the front view; specified in a note)"
                        % (len(h_), t_))
    for t_, h_ in over:
        warnings.append("hole group %dX %s has no callout (beyond max_callouts=%d; specified in a note)"
                        % (len(h_), t_, max_c))
    BW, BH = 56.0 / sc, 21.0 / sc                       # callout + FCF (datum symbol LEFT of the FCF), model mm
    rows = []
    DRF = R.drf(len(tg[0][2]) if tg else 0)              # pattern B clocks the part -> no C
    if len(tg) < 2:
        DRF = DRF[:2]                                    # no second hole group -> no datum C on this sheet
    letters = ["B", "C"]
    roles = [t_[0] for t_ in tg]
    for (gi, txt, hs, r, c) in sorted(tg, key=lambda t_: t_[4].Center.x):
        ox, oy = c.Center.x + r * 0.707, c.Center.y + r * 0.707
        role = roles.index(gi)                        # datum role by rank among VISIBLE groups
        cb_part = None
        if datums and (role == 0 or (role == 1 and "C" in DRF)) and ("  " + CBORE) in txt:
            txt, cb_part = txt.split("  " + CBORE, 1)   # the datum is the HOLE; the counterbore gets its own
            cb_part = CBORE + cb_part                    # callout + FCF, so the datum symbol is unambiguous
        label = ("%dX " % len(hs) if len(hs) > 1 else "") + txt
        if topv is None:                               # 2-view sheet: blocks above the front view
            x = min(max(c.Center.x, xl + BW / 2 - 10 / sc), xh - BW / 2 + 10 / sc) if (xh - xl) > BW else (xl + xh) / 2
            row = 0
            while True:
                if row >= len(rows):
                    rows.append([])
                if all(abs(x - q) >= BW for q in rows[row]):
                    rows[row].append(x)
                    break
                row += 1
            y0 = yh + 8.0 / sc + row * BH
        else:                                          # 3-view sheet (top view above): blocks left of the front view
            x = xl - BW / 2 - 12.0 / sc
            y0 = yh - 4.0 / sc - len(rows) * BH
            rows.append([x])
        pool = circles_all if txt.endswith("FAR SIDE") else circles
        anchors = [[cc.Center.x, cc.Center.y] for cc in _own([cc for (_i, cc) in pool if abs(cc.Radius - r) < 0.02], hs)]
        # Y14.5 Fig 6-30: the FCF sits directly below the hole callout (10.5 mm: clears the measured FCF frame)
        items.append(dict(kind="callout", view=fv, text=label, origin=[ox, oy], at=[x, y0 + 10.5 / sc],
                          name="Call_g%d" % gi, label="Call_g%d" % gi, anchors=anchors, anchor_r=r))
        if not datums:
            continue
        tcell = tol_cell(2 * r, txt)
        if role == 0:
            cells = ["position", tcell, "A"]
        elif role == 1 and "C" in DRF:
            cells = ["position", tcell, "A", "B"]          # this group IS datum C: never self-referenced
        else:
            cells = ["position", tcell] + DRF
        items.append(dict(kind="fcf", view=fv, cells=cells, at=[x, y0], name="FCF_g%d" % gi, label="FCF_g%d" % gi))
        if role == 0 or (role == 1 and "C" in DRF):
            items.append(dict(kind="datum", view=fv, letter=letters[role], origin=[x, y0 - 3.6 / sc],
                              at=[x, y0 - 10.0 / sc], label="Datum_%s_g%d" % (letters[role], gi)))
        if cb_part:
            rcb = hs[0]["cbore"][0] / 2.0
            lab2 = ("%dX " % len(hs) if len(hs) > 1 else "") + cb_part
            pool_ = circles_all if cb_part.endswith("FAR SIDE") else circles
            anc2 = [[cc.Center.x, cc.Center.y] for (_i, cc) in pool_ if abs(cc.Radius - rcb) < 0.02 and
                    any(abs(cc.Center.x - h_["x"]) < 0.1 and abs(abs(cc.Center.y) - abs(h_["y"])) < 0.1 for h_ in hs)]
            items.append(dict(kind="callout", view=fv, text=lab2, origin=[c.Center.x - rcb * 0.707, c.Center.y - rcb * 0.707],
                              at=[x, y0 - BH + 10.5 / sc], name="Call_g%d" % (30 + gi), label="Call_g%d" % (30 + gi),
                              anchors=anc2, anchor_r=rcb))
            items.append(dict(kind="fcf", view=fv, at=[x, y0 - BH], name="FCF_g%d" % (30 + gi), label="FCF_g%d" % (30 + gi),
                              cells=["position", DIA + ("0.02" if "H7" in cb_part else pos_floor), "A", letters[role]]))
    # ---------------- side holes: callout + FCF where they are seen as circles (real frames, not note text)
    sgroups, side_called = {}, set()
    for h in side_holes:
        sgroups.setdefault(htext(h), []).append(h)
    drf_s = DRF[:2] if tg else ["A"]
    for si, (txt, hs) in enumerate(sgroups.items()):
        r = hs[0]["r"]
        ax_ = hs[0]["ax"]
        vw, cand, far_ = None, [], False
        for vw_, along, mp in ((side, 0, lambda q, sx, sy: (sx * (q.z - Lz / 2), sy * q.y)),
                               (topv, 1, lambda q, sx, sy: (sx * q.x, sy * (q.z - Lz / 2)))):
            if vw_ is None or abs((ax_.x, ax_.y)[along]) < 0.99:
                continue                            # this view does not see the group as circles
            vis_c = [c for (_i, c) in full_circles(vw_) if abs(c.Radius - r) < 0.02]
            hid_c = [c for (_i, c) in full_circles(vw_, hidden_ok=True) if abs(c.Radius - r) < 0.02
                     and not any(abs(c.Center.x - w.Center.x) < 0.05 and abs(c.Center.y - w.Center.y) < 0.05 for w in vis_c)]

            def _hits(cs, sx, sy):
                out_ = []
                for h_ in hs:
                    px, py = mp(h_["c"], sx, sy)
                    m_ = [c for c in cs if abs(c.Center.x - px) < 0.1 and abs(c.Center.y - py) < 0.1]
                    if not m_:
                        return None
                    out_.append(m_[0])
                return out_
            for cs, isfar in ((vis_c, False), (hid_c, True)):   # visible match first; hidden -> FAR SIDE
                for sx in (1, -1):
                    for sy in (1, -1):
                        hit = _hits(cs, sx, sy)
                        if hit and not cand:
                            vw, cand, far_ = vw_, hit, isfar
            if cand:
                break
        if vw is None:
            continue
        od_ = hs[0].get("open_dir")
        if od_ is not None:                         # right view looks from +X, top view from +Y (third angle)
            far_ = (od_.x < 0) if vw is side else (od_.y < 0)
        if far_:
            txt = txt + " FAR SIDE"
        c = max(cand, key=lambda c: c.Center.x + 0.3 * c.Center.y)
        gid = 50 + si
        y0 = c.Center.y + r + 10.0 / sc
        x = c.Center.x + r + 24.0 / sc
        label = ("%dX " % len(hs) if len(hs) > 1 else "") + txt
        items.append(dict(kind="callout", view=vw.Name, text=label, origin=[c.Center.x + r * 0.707, c.Center.y + r * 0.707],
                          at=[x, y0 + 10.5 / sc], name="Call_g%d" % gid, label="Call_g%d" % gid,
                          anchors=[[cc.Center.x, cc.Center.y] for cc in cand], anchor_r=r))
        if datums:
            items.append(dict(kind="fcf", view=vw.Name, cells=["position", tol_cell(2 * r, txt)] + drf_s, at=[x, y0],
                              name="FCF_g%d" % gid, label="FCF_g%d" % gid))
        side_called.add(txt.replace(" FAR SIDE", ""))
    side_holes = [h for h in side_holes if htext(h) not in side_called]
    # ---------------- bearing journals (external cylinders on a bearing bore size): fit callout + FCF
    hidden_journals = []
    journals_present = False
    if proc.get("journals") and not plain:
        seen = set()
        for f in shape.Faces:
            srf = f.Surface
            if srf.__class__.__name__ != "Cylinder" or abs(abs(V(srf.Axis).z) - 1) > 1e-3:
                continue
            u0, u1, v0, v1 = f.ParameterRange
            p_ = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
            n_ = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
            c_ = V(srf.Center)
            rad = (p_ - c_) - V(srf.Axis) * ((p_ - c_).dot(V(srf.Axis)))
            if rad.Length < 1e-9 or n_.dot(rad) < 0 or not R.bearing_seat(2 * srf.Radius, False):
                continue                                   # concave, or not a bearing journal size
            key = round(srf.Radius, 2)
            if key in seen:
                continue
            seen.add(key)
            journals_present = True
            cand = [cc for (_i, cc) in circles if abs(cc.Radius - srf.Radius) < 0.02]
            txt = "%s%s %s  Ra 0.8" % (DIA, R.mm(2 * srf.Radius), R.fit_text(2 * srf.Radius, False))
            if not cand:                                   # journal behind the front face: FAR SIDE callout
                cand = [cc for (_i, cc) in circles_all if abs(cc.Radius - srf.Radius) < 0.02]
                if cand:
                    txt += " FAR SIDE"
            if not cand:
                hidden_journals.append(txt)
                continue
            cc = cand[0]
            gi = 20 + len(seen)
            items.append(dict(kind="callout", view=fv, text=txt,
                              origin=[cc.Center.x - srf.Radius * 0.707, cc.Center.y - srf.Radius * 0.707],
                              at=[xl - 34.0 / sc, yl + 2.0 / sc], name="Call_g%d" % gi, label="Call_g%d" % gi,
                              anchors=[[cc.Center.x, cc.Center.y]], anchor_r=srf.Radius))
            if datums:
                items.append(dict(kind="fcf", view=fv, cells=["position", DIA + "0.02"] + (DRF[:2] if tg else ["A"]),
                                  at=[xl - 34.0 / sc, yl + 2.0 / sc - 10.5 / sc], name="FCF_g%d" % gi,
                                  label="FCF_g%d" % gi))
    drf = DRF if tg else ["A"]
    # ---------------- all-around profile, from the end of the part with fewer holes; ALL OVER beneath it
    prof_tol = p.get("profile_tolerance", proc.get("prof"))
    if prof_tol:
        ncl = sum(1 for (_i, cc) in circles if cc.Center.x < xl + 0.25 * (xh - xl))
        ncr = sum(1 for (_i, cc) in circles if cc.Center.x > xh - 0.25 * (xh - xl))
        px_ = (xh - 0.04 * (xh - xl)) if ncr < ncl else (xl + 0.04 * (xh - xl))
        vpts = [[q.x, q.y] for _i, q in _vertices(front)][:400]
        items.append(dict(kind="fcf", view=fv, cells=["profile_surface", str(prof_tol)] + (drf if datums else []),
                          leader_to=[px_, yh], at=[px_ + (22 / sc if px_ < (xl + xh) / 2 else -22 / sc), yh + 8 / sc],
                          name="FCF_profile", label="Prof_g99", anchors=vpts, anchor_r=0.0))
        items.append(dict(kind="note", html='<html><body style="font-family:osifont; font-size:10pt;">ALL OVER</body></html>',
                          x=float(pg.X) + px_ * sc, y=float(pg.Y) + yh * sc, width=30, name="AllOver", label="AllOver_g99"))
    # ---------------- notes (feature-driven: no boilerplate for absent features)
    texts = [htext(h_) for h_ in holes + side_holes_all]
    has_threads = any(("M3x" in s_ or "M4x" in s_ or "M5x" in s_ or s_.startswith("M") or "Tr8" in s_) for s_ in texts)
    model_ref = p.get("model_ref") or os.path.basename(p.get("brep") or "") or part_name
    dxf_ref = p.get("dxf_ref") or (os.path.splitext(model_ref)[0] + ".dxf")
    lines = []
    if p.get("model_note", True):
        lines.append(("DIMS/TOLS PER ASME Y14.5-2018. UNDIMENSIONED GEOMETRY IS BASIC, DEFINED BY THE MODEL %s%s.%s%s") % (
            model_ref, (" AND DXF " + dxf_ref) if proc_name == "plate" else "",
            (" THE PROFILE FCF APPLIES ALL OVER EXCEPT FEATURES THAT CARRY A SIZE TOLERANCE, A FIT OR THEIR OWN FCF "
             "(Y14.5 11.3.1.4).") if prof_tol else "",
            " THREADED HOLES ARE MODELLED AS PLAIN HOLES: THE THREAD CALLOUT GOVERNS." if has_threads else ""))
    names = list(p.get("instances") or [])
    if names:
        hand = ""
        if len(names) > 1:
            mirror = any(n_.endswith(("_R", "R")) for n_ in names) and any(n_.endswith(("_L", "L")) for n_ in names)
            hand = (" %s SHOWN; %s IS ITS MIRROR IMAGE." % (names[0], ", ".join(names[1:]))
                    if mirror and not F.symmetric(shape) else " ALL IDENTICAL (%s SHOWN)." % names[0])
        lines.append("QTY %d%s: %s.%s" % (len(names), (" " + p["qty_per"]) if p.get("qty_per") else "", ", ".join(names), hand))
    ft = F.features(shape)
    for s_ in list(proc.get("notes") or []):
        if s_ == "@CSK":
            continue                                # countersinks are specified in the hole callouts (4.5.12)
        if s_ == "@THREADS":
            if not has_threads:
                continue
            s_ = "THREADS PER ASME B1.13M, CLASS 6H."
        elif s_ == "@MASK":
            if not any("H7" in x_ for x_ in texts) and not journals_present:
                s_ = "FINISH: CLEAR ANODIZE PER MIL-A-8625 TYPE II CLASS 1."
            else:
                s_ = "FINISH: CLEAR ANODIZE PER MIL-A-8625 TYPE II CLASS 1; MASK ALL TOLERANCED FIT FEATURES."
        elif s_ == "@INSERT":
            ins = proc.get("insert_d")
            if not ins or not any(abs(2 * h_["r"] - float(ins)) < 0.05 for h_ in holes + side_holes_all):
                continue
            s_ = DIA + "%s HOLES: M3 HEAT-SET INSERTS, LENGTH NOT TO EXCEED WALL/HOLE DEPTH, INSTALL AT 230 °C." % R.mm(float(ins))
        elif s_ == "@SHAFT":
            ng_ = ft["grooves"]
            ops = ["CUT TO LENGTH"] + (["STEP"] if ft["step"] else []) + \
                (["%s PER DIN 6799 SIZE 7 (%s7 x 0.94)" % ("%dX E-CLIP GROOVES" % ng_ if ng_ > 1 else "E-CLIP GROOVE", DIA)]
                 if ng_ else []) + (["DRIVE FLATS AS MODELLED (MATE THE D-BORES)"] if ft["flat"] else [])
            s_ = "; ".join(ops) + (". MACHINE THESE FEATURES BY GRINDING (CASE HARDENED STOCK)." if len(ops) > 1 else ".")
        elif s_ == "@SHIMMAT":
            s_ = ("MATERIAL: 301 STAINLESS FULL-HARD SHIM STOCK, %s mm THICK. CUT FROM STEP {step} (LASER OR PHOTO-ETCH)."
                  % R.mm(Lz, 2) if Lz <= 0.5 else
                  "MATERIAL: 301 STAINLESS 1/2-HARD SHEET, %s mm THICK. LASER CUT FROM STEP {step}, DEBURR." % R.mm(Lz, 2))
        s_ = s_.replace("@THK", R.mm(Lz, 2) + " mm")
        if "; HOLES MARKED REAM/BORE" in s_ and not any(
                (" REAM" in x_ or " BORE" in x_.replace("D-BORE", "")) for x_ in texts):
            s_ = s_.split("; HOLES MARKED REAM/BORE")[0] + "."       # no finished holes on this part
        lines.append(s_.format(step=model_ref, dxf=dxf_ref))
    for hx in ft.get("hex") or []:
        if config.get("note_hex", True) and not any(part_name.startswith(k_) for k_ in (config.get("extra_notes") or {})):
            lines.append("HEX DRIVE %s AF %s AS MODELLED%s." % ("HOLE" if hx["hole"] else "BOSS", R.mm(hx["af"], 2),
                                                               " (SHARP CORNERS; RELIEF OUTSIDE THE FLATS PERMITTED)"
                                                               if hx["hole"] else ""))
    lines += [str(s_) for s_ in (p.get("notes") or [])]
    if side_holes:
        sg = {}
        for h in side_holes:
            sg[htext(h)] = sg.get(htext(h), 0) + 1
        lines.append("SIDE HOLES (LOCATION PER MODEL, ⌖ %s%s|%s): " % (DIA, pos_floor, "|".join(drf_s)) +
                     "; ".join("%dX %s" % (n_, s_) for s_, n_ in sg.items()))
        warnings.append("side holes not seen as circles in any view (note only): %s" % "; ".join(sg))
    if farside or over:
        parts_ = ["%dX %s ⌖ %s|%s" % (len(h_), t_, tol_cell(2 * h_[0]["r"], t_), "|".join(drf)) for t_, h_ in farside + over]
        lines.append("HOLES WITHOUT CALLOUT (LOCATION PER MODEL): " + "; ".join(parts_))
    if hidden_journals:
        lines.append("JOURNAL / PILOT NOT SEEN AS A CIRCLE IN THE FRONT VIEW (LOCATION PER MODEL, ⌖ %s0.02|%s): %s." % (
            DIA, "|".join(DRF[:2] if tg else ["A"]), "; ".join(hidden_journals)))
        warnings.append("journals without callout: %s" % "; ".join(hidden_journals))
    for key_, txt_ in (config.get("extra_notes") or {}).items():
        if part_name.startswith(key_):
            lines.append(str(txt_))
    if skipped:
        lines.append("ENVELOPE %s x %s x %s (REF)." % (R.mm(xh - xl, 1), R.mm(yh - yl, 1), R.mm(Lz, 1)))
    if mass is not None:
        lines.append("MASS %s g (REF)." % R.mm(mass, 1))
    if lines:
        items.append(dict(kind="note", lines=["NOTES:"] + ["%d. %s" % (i + 1, s) for i, s in enumerate(lines)],
                          size_pt=7, x=106 * kx, y=74 * ky, width=150 * kx, name="Notes"))
    if iso is not None:
        items.append(dict(kind="note", lines=["ISOMETRIC (REF)", "SCALE " + scale_txt(isc)], size_pt=8,
                          x=372 * kx, y=196 * ky, width=40, name="Cap_iso"))
    items.extend(p.get("extra_items") or [])
    ann = annotate({"doc": doc.Name, "page": page.Name, "items": items})
    res = {"doc": doc.Name, "page": page.Name, "scale": sc, "size_mm": [round(Lx, 2), round(Ly, 2), round(Lz, 2)],
           "process": proc_name, "views": [v.Name for v in views], "annotations": len(ann["created"]),
           "hole_groups": len(tg), "drf": drf if datums else [], "envelope_note": bool(skipped), "warnings": warnings,
           "problems": ann["problems"], "text_lint": ann["text_lint"]}
    if p.get("layout", True):
        lay = run_layout(doc, page, p)
        res["layout"] = lay["report"]
        res["conflicts"] = lay["conflicts"]
    if p.get("out_dir"):
        res.update(export_page({"doc": doc.Name, "page": page.Name, "out_dir": p["out_dir"], "stem": p.get("stem"),
                                "save_fcstd": p.get("save_fcstd", True), "close_document": p.get("close", True)}))
    return res
