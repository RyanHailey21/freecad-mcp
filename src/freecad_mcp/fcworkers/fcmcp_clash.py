# -*- coding: utf-8 -*-
"""Headless stage 2 worker: boolean overlap volume (and optional clearance) per pair.

Params: {"tasks": [[key, fpA, matA, fpB, matB, clearance], ...], "breps": {fp: path},
         "out", "progress"}
Result: {"results": {key: [volume, distance_or_null]}, "elapsed"}
volume -1 marks a failed boolean (inspect manually). distance is computed only
when clearance > 0 and the solids do not overlap.
"""
import time
import traceback

import fcmcp_common as C

P = C.load_params()
t0 = time.time()
base = {}
res = {}
errors = []


def shape(fp, m):
    if fp not in base:
        base[fp] = C.load_brep(P["breps"][fp])
    s = base[fp].copy()
    s.Placement = C.placement12(m).multiply(base[fp].Placement)
    return s


tasks = P["tasks"]
prog = C.Progress(P.get("progress"), len(tasks))
for i, (key, fa, ma, fb, mb, clear) in enumerate(tasks):
    try:
        a, b = shape(fa, ma), shape(fb, mb)
        try:
            v = a.common(b).Volume
        except Exception:
            v = -1.0
        d = None
        if clear and v <= 1e-6:
            try:
                d = a.distToShape(b)[0]
            except Exception:
                d = -1.0
        res[key] = [v, d]
    except Exception as exc:
        errors.append("%s: %s" % (key, exc))
        res[key] = [-1.0, None]
    prog(i + 1)
C.write_json(P["out"], {"results": res, "errors": errors[:20], "elapsed": round(time.time() - t0, 3),
                        "loaded": len(base)})
