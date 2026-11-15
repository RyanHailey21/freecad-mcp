"""Model-vs-drawing cross-check for TechDraw part drawings (``techdraw_verify``).

Independent reviews of the light_weight_arm drawings found what lint could not: wrong FAR SIDE, wrong
hole counts, blind depths deeper than the part, wrong countersink angles, threads on clearance holes,
datums referenced but never defined.  This module re-derives the hole features from the MODEL
(``fcmcp_features``, headless) and checks every hole callout on the saved sheet against them.

The extraction runs in ``freecadcmd`` (``EXTRACT_LIB``); parsing and comparison are pure Python.
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------- headless extraction
EXTRACT_LIB = r'''
import os
import re
import FreeCAD as App
import fcmcp_features as F


def _strip(html):
    s = re.sub(r"<br\s*/?>", "\n", html or "")
    return re.sub(r"<[^>]+>", "", s)


def extract(p):
    doc = App.openDocument(p["path"])
    try:
        shape = None
        if p.get("model"):
            import Part
            shape = Part.Shape()
            if p["model"].lower().endswith((".brep", ".brp")):
                shape.importBrep(p["model"])
            else:
                shape.read(p["model"])
            if p.get("orient", True):
                shape = F.oriented(shape)
        else:
            obj = doc.getObject(p.get("object") or "Part")
            if obj is None:
                raise ValueError("no 'Part' object in the drawing; pass model= (STEP/BREP)")
            shape = obj.Shape                     # part_drawing stores the oriented shape it drew
        balloons, notes = [], []
        for o in doc.Objects:
            if o.TypeId == "TechDraw::DrawViewBalloon":
                sv = getattr(o, "SourceView", None)
                balloons.append({"name": o.Name, "label": o.Label, "text": str(o.Text),
                                 "view": sv.Name if sv is not None else None})
            elif o.TypeId == "TechDraw::DrawRichAnno":
                notes.append({"label": o.Label, "text": _strip(str(o.AnnoText))})
        return {"model": F.describe(shape, orient=False), "balloons": balloons, "notes": notes,
                "doc": doc.Name}
    finally:
        App.closeDocument(doc.Name)
'''

# --------------------------------------------------------------------------- callout parsing
DIA, CBORE, CSK, DEPTH = "⌀", "⌴", "⌵", "↧"
_NUM = r"(\d+(?:\.\d+)?)"
TAP_DRILL = {"M2": (1.5, 1.8), "M2.5": (1.95, 2.25), "M3": (2.4, 2.85), "M4": (3.2, 3.6), "M5": (4.1, 4.5),
             "M6": (4.9, 5.3), "M8": (6.6, 7.0), "Tr8": (7.8, 8.3)}


def parse_callout(text: str) -> dict[str, Any] | None:
    """Y14.5 hole callout text -> {count, dia | thread, thru, depth, each_side, far, cbore, cb_far, csk,
    csk_far, fit}.  None when the text is not a hole callout."""
    s = " ".join(text.split())
    m = re.match(r"^(\d+)X\s+", s)
    out: dict[str, Any] = {"count": int(m.group(1)) if m else 1}
    if m:
        s = s[m.end():]
    main, cb, ck = s, None, None
    if CSK in main:
        main, ck = main.split(CSK, 1)
    if CBORE in main:
        main, cb = main.split(CBORE, 1)
    mt = re.match(r"^(M\d+(?:\.\d+)?)x" + _NUM, main) or re.match(r"^(Tr\d+)x", main)
    md = re.match("^" + DIA + _NUM, main)
    if mt:
        out["thread"] = mt.group(1)
    elif md:
        out["dia"] = float(md.group(1))
    elif cb is not None and not main.strip():         # split counterbore callout (datum group): own line
        c = re.match("^" + DIA + _NUM, cb.strip())
        d = re.search(DEPTH + _NUM, cb)
        return {"count": out["count"], "cbore_only": True,
                "cbore": [float(c.group(1)) if c else None, float(d.group(1)) if d else None],
                "cb_far": "FAR SIDE" in cb}
    else:
        return None
    if re.search(r"\s[a-z]\d{1,2}\b", " " + main):      # lower-case fit (k6, h6): an external journal
        out["external"] = True
    out["thru"] = " THRU" in main
    dm = re.search(DEPTH + _NUM, main)
    out["depth"] = float(dm.group(1)) if dm else None
    out["each_side"] = "EACH SIDE" in main or "BOTH ENDS" in main
    out["far"] = main.rstrip().endswith("FAR SIDE")
    out["dflat"] = "D-BORE" in main
    if cb is not None:
        c = re.match("^" + DIA + _NUM, cb.strip())
        d = re.search(DEPTH + _NUM, cb)
        out["cbore"] = [float(c.group(1)) if c else None, float(d.group(1)) if d else None]
        out["cb_far"] = "FAR SIDE" in cb
        if out["cb_far"] and main.rstrip().endswith("FAR SIDE"):
            out["far"] = False
    if ck is not None:
        c = re.match("^" + DIA + _NUM, ck.strip())
        a = re.search(r"X\s*" + _NUM + "°", ck)
        out["csk"] = [float(c.group(1)) if c else None, float(a.group(1)) if a else None]
        out["csk_far"] = "FAR SIDE" in ck
    return out


def _groups(holes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Model holes grouped like the generator: same diameter, thru/depth, counterbore, countersink, sides."""
    g: dict[tuple, dict[str, Any]] = {}
    for h in holes:
        cb = tuple(round(x, 1) for x in h["cbore"]) if h.get("cbore") else None
        ck = (round(h["csk"][0], 1), round(h["csk"][1]), bool(h["csk"][2])) if h.get("csk") else None
        key = (round(2 * h["r"], 2), bool(h["thru"]), None if h["thru"] else round(h["depth"], 1), cb,
               bool(h.get("cb_far")), ck, bool(h.get("each_side")), bool(h.get("far")), "dflat" in h)
        e = g.setdefault(key, dict(h, n=0))
        e["n"] += 1
    return list(g.values())


def _dia_matches(c: dict[str, Any], h: dict[str, Any]) -> bool:
    d = 2 * h["r"]
    if "thread" in c:
        lo_hi = TAP_DRILL.get(c["thread"])
        return bool(lo_hi and lo_hi[0] - 0.05 <= d <= lo_hi[1] + 0.05)
    return abs(c["dia"] - d) <= max(0.05, 0.011 + 0.005 * d)    # override texts may round (2.54 -> 2.5)


def compare(data: dict[str, Any], side_holes: bool = True) -> dict[str, Any]:
    """Check every hole callout against the model's hole groups; list model groups nobody called out."""
    model = data["model"]
    thick = model["thickness"]
    axial = _groups(model["holes"])
    side = _groups(model["side_holes"]) if side_holes else []
    findings: list[dict[str, Any]] = []
    used: set[int] = set()
    calls = []
    for b in data["balloons"]:
        if not b["label"].startswith("Call_"):
            continue
        c = parse_callout(b["text"])
        if c is None:
            continue
        calls.append((b, c))

    def add(sev, b, msg):
        findings.append({"severity": sev, "object": b["name"] if b else None, "label": b["label"] if b else None,
                         "callout": b["text"] if b else None, "issue": msg})

    cb_only = [(b, c) for b, c in calls if c.get("cbore_only")]
    external = [b for b, c in calls if c.get("external")]
    for b, c in calls:
        if c.get("cbore_only") or c.get("external"):
            continue
        pool = list(enumerate(axial)) + [(1000 + i, h) for i, h in enumerate(side)]
        cand = [(i, h) for i, h in pool if _dia_matches(c, h)]
        if not cand:
            add("error", b, "no hole of this size in the model")
            continue
        # prefer the group that agrees on the most attributes
        def score(ih):
            _i, h = ih
            s = 0
            s += 3 if h["n"] == c["count"] else 0
            s += 2 if bool(h["thru"]) == c["thru"] else 0
            s += 1 if bool(h.get("cbore")) == bool(c.get("cbore")) else 0
            s += 1 if bool(h.get("csk")) == bool(c.get("csk")) else 0
            s += 1 if (_i in used) is False else 0
            return s
        i, h = max(cand, key=score)
        used.add(i)
        if h["n"] != c["count"]:
            add("error", b, "count %dX on the sheet, %d in the model" % (c["count"], h["n"]))
        if bool(h["thru"]) != c["thru"] and not c["each_side"]:
            add("error", b, "THRU on the sheet but blind in the model" if c["thru"] else
                "blind on the sheet but THRU in the model")
        if c["depth"] is not None and not c["thru"]:
            if c["depth"] >= thick - 0.01 and i < 1000:
                add("error", b, "depth %g is not less than the part thickness %g" % (c["depth"], round(thick, 2)))
            if "thread" in c:
                if c["depth"] > h["depth"] + 0.01:
                    add("error", b, "thread depth %g deeper than the drilled hole %g" % (c["depth"], round(h["depth"], 2)))
            elif abs(c["depth"] - h["depth"]) > 0.06:
                add("error", b, "depth %g on the sheet, %g in the model" % (c["depth"], round(h["depth"], 2)))
        if c["each_side"] != bool(h.get("each_side")) and "thread" not in c and not c["thru"]:
            add("warning", b, "EACH SIDE mismatch (model each_side=%s)" % bool(h.get("each_side")))
        if i < 1000 and not c["thru"] and c["far"] != bool(h.get("far")):
            add("error", b, "FAR SIDE %s on the sheet but the hole opens on the %s face" % (
                "stated" if c["far"] else "missing", "far" if h.get("far") else "near"))
        if not c.get("cbore") and h.get("cbore"):     # split datum callout: the counterbore is its own line
            twin = [x for _b, x in cb_only if x["count"] == c["count"]]
            if twin:
                c = dict(c, cbore=twin[0]["cbore"], cb_far=twin[0]["cb_far"])
        if c.get("cbore"):
            if not h.get("cbore"):
                add("error", b, "counterbore on the sheet, none in the model")
            else:
                if c["cbore"][0] is not None and abs(c["cbore"][0] - h["cbore"][0]) > 0.06:
                    add("error", b, "counterbore %s%g vs model %g" % (DIA, c["cbore"][0], round(h["cbore"][0], 2)))
                if c["cbore"][1] is not None and abs(c["cbore"][1] - h["cbore"][1]) > 0.06:
                    add("error", b, "counterbore depth %g vs model %g" % (c["cbore"][1], round(h["cbore"][1], 2)))
                if c.get("cb_far") != bool(h.get("cb_far")):
                    add("error", b, "counterbore FAR SIDE %s" % ("stated, but it is on the near face" if c.get("cb_far")
                                                              else "missing: it is on the far face"))
        elif h.get("cbore"):
            add("error", b, "the model hole has a counterbore %s%g the sheet does not call out" % (DIA, round(h["cbore"][0], 2)))
        if c.get("csk"):
            if not h.get("csk"):
                add("error", b, "countersink on the sheet, none in the model")
            else:
                if c["csk"][1] is not None and abs(c["csk"][1] - h["csk"][1]) > 1.0:
                    add("error", b, "countersink angle %g° vs model %g°" % (c["csk"][1], round(h["csk"][1], 1)))
                if c.get("csk_far") != bool(h["csk"][2]):
                    add("error", b, "countersink FAR SIDE %s" % ("stated, but it opens on the near face" if c.get("csk_far")
                                                              else "missing: it opens on the far face"))
        elif h.get("csk"):
            add("error", b, "the model hole has a countersink the sheet does not call out")
        if "thread" in c and h.get("cbore"):
            add("warning", b, "thread called out on a counterbored hole (clearance hole?)")
    notes = "\n".join(n["text"] for n in data.get("notes", []))
    for i, h in enumerate(axial):
        if i in used:
            continue
        d = "%s%s" % (DIA, ("%.2f" % (2 * h["r"])).rstrip("0").rstrip("."))
        mentioned = d in notes or any(t in notes for t, rng in TAP_DRILL.items() if rng[0] - 0.05 <= 2 * h["r"] <= rng[1] + 0.05)
        findings.append({"severity": "warning" if mentioned else "error", "object": None, "label": None, "callout": None,
                         "issue": "model hole group %dX %s%s has no callout%s" % (
                             h["n"], d, " THRU" if h["thru"] else " blind", " (only in a note)" if mentioned else "")})
    # datums
    texts = {b["label"]: b["text"] for b in data["balloons"]}
    defined = {v.strip() for k, v in texts.items() if k.startswith("Datum_")}
    used_d = set()
    for k, v in texts.items():
        if k.startswith(("FCF_", "Prof_")):
            used_d |= {c.strip() for c in v.split("|")[2:] if re.fullmatch(r"[A-Z]", c.strip())}
    for x in sorted(used_d - defined):
        findings.append({"severity": "error", "object": None, "label": None, "callout": None,
                         "issue": "datum %s referenced in an FCF but not defined on the sheet" % x})
    errors = sum(1 for f in findings if f["severity"] == "error")
    return {"callouts_checked": len(calls) - len(external), "external_callouts_skipped": len(external),
            "model_hole_groups": len(axial), "model_side_hole_groups": len(side),
            "thickness": thick, "errors": errors, "warnings": len(findings) - errors, "findings": findings,
            "clean": errors == 0}
