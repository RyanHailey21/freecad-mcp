# -*- coding: utf-8 -*-
"""Headless stage 1: evaluate a shape source at each pose, export unseen shapes as BREP.

Writes a manifest:
  {"shapes": {fp: {"brep", "faces", "volume", "solids"}},
   "poses": [{"label", "items": [[name, group, fp, mat12, world_bbox6], ...]}],
   "elapsed": {...}}
BREPs live in a content-addressed store (``<store>/<fp>.brep``) shared by all
calls, so unchanged parts are never re-exported.
"""
import json
import os
import time
import traceback

import FreeCAD as App  # noqa: F401

import fcmcp_common as C

P = C.load_params()
t0 = time.time()
out = {"shapes": {}, "poses": [], "errors": []}
try:
    source = P["source"]
    poses = P.get("poses")
    if poses is None:
        poses = C.provider_poses(source)
    store = P["store"]
    os.makedirs(store, exist_ok=True)
    tight = P.get("tight_bbox", False)
    t_build = 0.0
    seen = {}
    for pi, pose in enumerate(poses):
        tb = time.time()
        items = C.source_items(source, pose)
        t_build += time.time() - tb
        rows = []
        for it in items:
            shape, plc = it["shape"], it["placement"]
            memo = seen.get(id(shape))
            if memo is None or memo[0] is not shape:
                fp = C.fingerprint(shape)
                memo = seen[id(shape)] = (shape, fp, shape.BoundBox)   # keep shape alive: ids stay unique
            fp, lbb = memo[1], memo[2]
            if fp not in out["shapes"]:
                path = os.path.join(store, fp + ".brep")
                if not os.path.exists(path):
                    tmp = path + ".%d.tmp" % os.getpid()
                    shape.exportBrep(tmp)
                    os.replace(tmp, path)
                out["shapes"][fp] = {"brep": path, "faces": len(shape.Faces), "volume": shape.Volume,
                                     "solids": len(shape.Solids)}
            if tight:
                world = shape.copy()
                world.Placement = plc.multiply(shape.Placement)
                try:
                    bb = C.bbox6(world.optimalBoundingBox(False, False))
                except Exception:
                    bb = C.bbox6(world.BoundBox)
            else:
                bb = C.transformed_bbox(lbb, plc)
            rows.append([str(it["name"]), str(it.get("group", it["name"])), fp, C.mat12(plc), bb])
        label = pose if isinstance(pose, str) else (json.dumps(pose) if pose is not None else "pose%d" % pi)
        out["poses"].append({"label": label[:80], "items": rows})
    out["elapsed"] = {"build": round(t_build, 3), "total": round(time.time() - t0, 3)}
    out["deps"] = C.dependency_files(source)
except Exception as exc:
    out["errors"].append("%s\n%s" % (exc, traceback.format_exc()))
C.write_json(P["out"], out)
