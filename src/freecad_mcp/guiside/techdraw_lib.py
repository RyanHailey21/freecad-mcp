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
"""
import glob
import math
import os
import time

import FreeCAD as App

V = App.Vector
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


def wait_views(doc, views, timeout=30.0):
    import FreeCADGui as Gui
    end = time.time() + timeout
    while True:
        doc.recompute()
        Gui.updateGui()
        if all(_edges(v) for v in views if v.isDerivedFrom("TechDraw::DrawViewPart")):
            return True
        if time.time() > end:
            return False
        time.sleep(0.25)


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
    made, problems = [], []
    pending_cosmetic = set()
    for n, it in enumerate(p["items"]):
        kind = it["kind"]
        view = _obj(doc, it["view"]) if it.get("view") else None
        name = it.get("name") or kind.capitalize()
        try:
            if kind == "datum":
                at = it.get("at") or [it["origin"][0], it["origin"][1] + 5]
                o = _balloon(doc, page, view, it["letter"], it["origin"], at, name=name if it.get("name") else "Datum_" + it["letter"])
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
                if it.get("format"):
                    d.FormatSpec = T(it["format"])
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
            made.append({"kind": kind, "name": o.Name})
        except Exception as exc:
            problems.append({"item": n, "name": name, "error": "%s: %s" % (type(exc).__name__, exc)})
    doc.recompute()
    return {"created": made, "problems": problems}


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



# ------------------------------------------------------------------ one-call part drawing
SCALES = [10, 5, 4, 3, 2, 1.5, 1, 0.75, 0.5, 0.4, 0.3, 0.25, 0.2, 0.1, 0.05]


def scale_txt(sc):
    return "%g:1" % sc if sc >= 1 else "1:%g" % (1 / sc)


def pick_scale(w, h, max_w, max_h, scales=SCALES):
    for sc in scales:
        if w * sc <= max_w and h * sc <= max_h:
            return sc
    return scales[-1]


def oriented(shape):
    """Copy with axes permuted: X = longest, Y = middle, Z = shortest extent; centred, Z from 0."""
    s = shape.copy()
    bb = s.BoundBox
    L = [bb.XLength, bb.YLength, bb.ZLength]
    order = sorted(range(3), key=lambda i: -L[i])
    cols = [V(*[1.0 if k == i else 0.0 for k in range(3)]) for i in order]
    m = App.Matrix(cols[0].x, cols[0].y, cols[0].z, 0, cols[1].x, cols[1].y, cols[1].z, 0,
                   cols[2].x, cols[2].y, cols[2].z, 0, 0, 0, 0, 1)
    if m.determinant() < 0:
        m.A31, m.A32, m.A33 = -m.A31, -m.A32, -m.A33
    s.transformShape(m)
    c = s.BoundBox.Center
    s.translate(V(-c.x, -c.y, -s.BoundBox.ZMin))
    return s


def hole_table(shape, rmin=0.9, rmax=12.5):
    """Round holes from concave cylinders grouped by axis/diameter; Z-axial holes get thru/depth/cbore."""
    zmin, zmax = shape.BoundBox.ZMin, shape.BoundBox.ZMax
    holes = {}
    for f in shape.Faces:
        srf = f.Surface
        if srf.__class__.__name__ != "Cylinder" or not (rmin <= srf.Radius <= rmax):
            continue
        u0, u1, v0, v1 = f.ParameterRange
        pt = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
        n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        ax, c = V(srf.Axis), V(srf.Center)
        radial = (pt - c) - ax * ((pt - c).dot(ax))
        if radial.Length < 1e-9 or n.dot(radial) > 0:
            continue
        az = abs(ax.z) > 0.999
        key = (round(c.x, 2), round(c.y, 2), round(c.z, 2) if not az else 0, az, round(srf.Radius, 2))
        bb = f.BoundBox
        h = holes.setdefault(key, dict(r=srf.Radius, x=c.x, y=c.y, axial=az, z0=1e9, z1=-1e9, span=0.0))
        h["z0"], h["z1"] = min(h["z0"], bb.ZMin), max(h["z1"], bb.ZMax)
        h["span"] += math.degrees(u1 - u0)
    full = [h for h in holes.values() if h["span"] >= 359.0]
    res = []
    for h in sorted(full, key=lambda h: h["r"]):
        h["thru"] = h["axial"] and h["z0"] <= zmin + 0.01 and h["z1"] >= zmax - 0.01
        h["depth"] = h["z1"] - h["z0"]
        res.append(h)
    by_xy = {}
    for h in res:
        if h["axial"]:
            by_xy.setdefault((round(h["x"], 1), round(h["y"], 1)), []).append(h)
    final = []
    for hs in by_xy.values():
        hs.sort(key=lambda h: h["r"])
        base = hs[0]
        base["cbore"] = None
        for big in hs[1:]:
            if not big["thru"]:
                base["cbore"] = (2 * big["r"], big["depth"])
        final.append(base)
    side = [h for h in res if not h["axial"]]
    return final, side


def hole_text(h, tapped=None):
    """Y14.5 hole note. tapped: {"1.25": "M3x0.5-6H", ...} maps a blind-hole radius to a thread."""
    tapped = tapped or {}
    t = tapped.get("%.2f" % h["r"]) or tapped.get("%g" % round(h["r"], 2))
    if t and not h["thru"]:
        return "%s ↧%.1f" % (t, max(h["depth"] - 0.5, 1.0))
    s = "⌀%.1f" % (2 * h["r"])
    s += " THRU" if h["thru"] else " ↧%.1f" % h["depth"]
    if h.get("cbore"):
        s += "  ⌴⌀%.1f ↧%.1f" % h["cbore"]
    return s


def part_drawing(p):
    """Complete reduced-dimension part drawing from one shape (see the MCP tool docstring)."""
    import Part
    if p.get("brep"):
        shape = Part.Shape()
        shape.importBrep(p["brep"])
    else:
        sdoc = _doc(p["source_doc"])
        shape = Part.getShape(_obj(sdoc, p["object"]), "", needSubElement=False, refine=False, transform=False)
    if p.get("orient", True):
        shape = oriented(shape)
    bb = shape.BoundBox
    Lx, Ly, Lz = bb.XLength, bb.YLength, bb.ZLength
    density = float(p.get("density_g_cm3", 0) or 0)
    fields = dict(p.get("fields") or {})
    if density and "Weight" not in fields:
        fields["Weight"] = "%.1f g" % (shape.Volume * density / 1000.0)
    info = create_page({"doc": p["doc"], "template": p.get("template") or "ANSIB_Landscape", "fields": fields,
                        "replace": True})
    doc = App.getDocument(info["doc"])
    page = doc.getObject(info["page"])
    W, H = info["size_mm"]
    kx, ky = W / 431.8, H / 279.4                     # layout tuned on ANSI B, scaled to the sheet
    part = doc.addObject("Part::Feature", "Part")
    part.Shape = shape
    if part.ViewObject:
        part.ViewObject.Visibility = False
    projections = p.get("projections")
    thin = Lz < 0.15 * Ly
    if projections is None:
        projections = ["Right"] if thin else ["Right", "Top"]
    sc = float(p["scale"]) if p.get("scale") else pick_scale(Lx + Lz + 25, Ly + Lz + 25, 205 * kx, 150 * ky)
    tpl = page.Template
    et = dict(tpl.EditableTexts)
    if "scale" in et and "scale" not in fields:
        et["scale"] = scale_txt(sc)
        tpl.EditableTexts = et
    hidden = bool(p.get("hidden", True))
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
    pg.ScaleType = "Custom"
    pg.Scale = sc
    pg.X, pg.Y = 120 * kx, 165 * ky
    for v in pg.Views:
        v.HardHidden = hidden
    views = list(pg.Views)
    iso = None
    if p.get("iso", True):
        iso = doc.addObject("TechDraw::DrawViewPart", "Iso")
        iso.Source = [part]
        iso.Direction = V(1, -1, 1.2)
        isc = pick_scale(Lx, Ly, 70 * kx, 50 * ky)
        place(page, iso, 372 * kx, 232 * ky, isc)
        views.append(iso)
    wait_views(doc, views, float(p.get("wait", 40)))
    side = subs.get("Right")

    def extreme(view, axis, lo):
        vs = _vertices(view)
        if not vs:
            return None, None
        i, q = (min if lo else max)(vs, key=lambda t: t[1].x if axis == 0 else t[1].y)
        return "Vertex%d" % i, (q.x if axis == 0 else q.y)

    vxl, xl = extreme(front, 0, True)
    vxh, xh = extreme(front, 0, False)
    vyl, yl = extreme(front, 1, True)
    vyh, yh = extreme(front, 1, False)
    items = []
    if p.get("overall_dims", True) and vxl and vxh and vyl and vyh:
        items.append(dict(kind="dimension", view=front.Name, refs=[vxl, vxh], type="DistanceX", format="%.1f",
                          at=[0, -(yh - yl) * sc / 2 - 14], name="Dim_L"))
        items.append(dict(kind="dimension", view=front.Name, refs=[vyl, vyh], type="DistanceY", format="%.1f",
                          at=[-(xh - xl) * sc / 2 - 14, 0], name="Dim_W"))
        if side is not None:
            sl, slv = extreme(side, 0, True)
            sh, shv = extreme(side, 0, False)
            if sl and sh:
                items.append(dict(kind="dimension", view=side.Name, refs=[sl, sh], type="DistanceX", format="%.1f",
                                  at=[0, -(yh - yl) * sc / 2 - 12], name="Dim_T"))
                if p.get("datums", True):
                    items.append(dict(kind="datum", view=side.Name, letter="A", origin=[shv, (yh - yl) * 0.25],
                                      at=[shv + 8 / sc, (yh - yl) * 0.25]))
    pos_tol = str(p.get("position_tolerance", "0.1"))
    nholes = 0
    side_holes = []
    if p.get("hole_callouts", True) and vxl:
        holes, side_holes = hole_table(shape)
        groups = {}
        for h in holes:
            groups.setdefault(hole_text(h, p.get("tapped")), []).append(h)
        order = sorted(groups.items(), key=lambda kv: -kv[1][0]["r"])
        circles = [e.Curve for _i, e in _edges(front) if e.Curve.__class__.__name__ == "Circle"]
        x_text = (xh - xl) / 2 + 22 / sc
        y_text = (yh - yl) / 2 + 4 / sc
        step = 17.0 / sc
        letters = ["B", "C"]
        for gi, (txt, hs) in enumerate(order[:int(p.get("max_callouts", 7))]):
            r = hs[0]["r"]
            cand = [c for c in circles if abs(c.Radius - r) < 0.02]
            if not cand:
                continue
            c = max(cand, key=lambda c: c.Center.x + 0.3 * c.Center.y)
            ox, oy = c.Center.x + r * 0.707, c.Center.y + r * 0.707
            y = y_text - nholes * step
            items.append(dict(kind="callout", view=front.Name, text=("%dX " % len(hs) if len(hs) > 1 else "") + txt,
                              origin=[ox, oy], at=[x_text, y], name="Call_%d" % nholes))
            if p.get("datums", True):
                drf = ["A"] if nholes == 0 else (["A", "B"] if nholes == 1 else ["A", "B", "C"])
                sym = "perpendicularity" if nholes == 0 else "position"
                items.append(dict(kind="fcf", view=front.Name, cells=[sym, "{dia}" + pos_tol] + drf,
                                  at=[x_text, y - 6.5 / sc], name="FCF_%d" % nholes))
                if nholes < 2:
                    items.append(dict(kind="datum", view=front.Name, letter=letters[nholes],
                                      origin=[x_text - 16 / sc, y - 9.8 / sc], at=[x_text - 24 / sc, y - 13.5 / sc]))
            nholes += 1
    if p.get("profile_tolerance") and vxl:
        drf = ["A", "B", "C"][:1 + min(nholes, 2)] if p.get("datums", True) else []
        items.append(dict(kind="fcf", view=front.Name, cells=["profile_surface", str(p["profile_tolerance"])] + drf,
                          leader_to=[xl + 0.3 * (xh - xl), yh], at=[xl + 0.3 * (xh - xl), yh + 12 / sc],
                          name="FCF_profile"))
    notes = list(p.get("notes") or [])
    if side_holes and p.get("hole_callouts", True):
        sg = {}
        for h in side_holes:
            k = hole_text(h, p.get("tapped"))
            sg[k] = sg.get(k, 0) + 1
        notes.append("SIDE HOLES (LOCATION PER MODEL): " + "; ".join("%dX %s" % (n, s) for s, n in sg.items()))
    if notes:
        lines = ["NOTES:"] + ["%d. %s" % (i + 1, s) for i, s in enumerate(notes)]
        items.append(dict(kind="note", lines=lines, size_pt=7, x=107 * kx, y=66 * ky, width=172 * kx, name="Notes"))
    if iso is not None:
        items.append(dict(kind="note", lines=["ISOMETRIC (REF)", "SCALE " + scale_txt(iso.Scale)], size_pt=8,
                          x=372 * kx, y=196 * ky, width=40, name="Cap_iso"))
    items.extend(p.get("extra_items") or [])
    ann = annotate({"doc": doc.Name, "page": page.Name, "items": items})
    res = {"doc": doc.Name, "page": page.Name, "scale": sc, "size_mm": [round(Lx, 2), round(Ly, 2), round(Lz, 2)],
           "views": [v.Name for v in views], "annotations": len(ann["created"]), "hole_groups": nholes,
           "problems": ann["problems"]}
    if p.get("out_dir"):
        res.update(export_page({"doc": doc.Name, "page": page.Name, "out_dir": p["out_dir"], "stem": p.get("stem"),
                                "save_fcstd": p.get("save_fcstd", True), "close_document": p.get("close", True)}))
    return res
