"""Hole edge-distance / broken-out-hole manufacturability check (headless, cached, parallel).

Results depend only on a part's own geometry, so they are cached per geometry
fingerprint + thresholds: after an edit only the changed parts are re-checked.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .interference import prepare
from .parallel import JsonCache, cache_root, default_workers, run_workers, split_balanced

DEFAULT_OPTS = dict(fail_wall=1.0, warn_wall=1.5, warn_ratio=0.5, min_d=2.0, max_d=12.0, tangent_deg=3.0,
                    search_margin=8.0)
HOLES_VERSION = "1"


def hole_edge_check(
    source: dict[str, Any],
    pose: Any = None,
    only: str | None = None,
    fail_wall: float = 1.0,
    warn_wall: float = 1.5,
    warn_ratio: float = 0.5,
    min_diameter: float = 2.0,
    max_diameter: float = 12.0,
    tangent_deg: float = 3.0,
    workers: int = 0,
    use_cache: bool = True,
    report_path: str | None = None,
    max_rows: int = 80,
    command: list[str] | None = None,
    timeout: float = 3600,
    job: Any = None,
) -> dict[str, Any]:
    t0 = time.time()
    cancel = getattr(job, "cancel", None)
    report = (lambda d, t, m: job.progress(d, t, m)) if job is not None else None
    opts = dict(DEFAULT_OPTS, fail_wall=fail_wall, warn_wall=warn_wall, warn_ratio=warn_ratio,
                min_d=min_diameter, max_d=max_diameter, tangent_deg=tangent_deg)
    if not 0 < min_diameter <= max_diameter:
        raise ValueError("need 0 < min_diameter <= max_diameter")
    manifest = prepare(source, [pose], command, timeout, cancel=cancel, reuse=use_cache)
    t_prep = time.time() - t0
    rx = re.compile(only) if only else None
    names: dict[str, list[str]] = {}
    for name, _g, fp, _m, _b in manifest["poses"][0]["items"]:
        if rx and not rx.search(name):
            continue
        names.setdefault(fp, []).append(name)
    okey = hashlib.sha1(json.dumps(opts, sort_keys=True).encode()).hexdigest()[:8]
    cache = JsonCache(cache_root() / "holes_cache.json")
    if not use_cache:
        cache.data = {}
    key = lambda fp: f"{HOLES_VERSION}|{fp}|{okey}"  # noqa: E731
    todo = [[fp, manifest["shapes"][fp]["brep"]] for fp in names if key(fp) not in cache]
    t1 = time.time()
    n_workers = max(1, min(workers or default_workers(), len(todo) // 2 or 1))
    if todo:
        faces = {fp: s["faces"] for fp, s in manifest["shapes"].items()}
        bins = split_balanced(todo, lambda t: faces[t[0]] ** 1.5, n_workers)
        results = run_workers("fcmcp_holes.py", [{"tasks": b, "opts": opts} for b in bins], command, timeout,
                              report, cancel, "holes")
        for r in results:
            for fp, v in r["results"].items():
                cache.put(key(fp), v)
        cache.save()
    rows, counts, n_holes = [], {"BROKEN": 0, "FAIL": 0, "WARN": 0, "ERROR": 0}, 0
    for fp, ns in names.items():
        res = cache.get(key(fp))
        n_holes += res["holes"] * len(ns)
        for status, d, where, val in res["rows"]:
            for n in ns:
                counts[status] = counts.get(status, 0) + 1
                row = {"status": status, "part": n, "d": d, "at": where}
                row["rim_deg" if status == "BROKEN" else ("error" if status == "ERROR" else "wall_mm")] = val
                rows.append(row)
    order = {"ERROR": 0, "BROKEN": 1, "FAIL": 2, "WARN": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["part"], r.get("wall_mm", 0) or 0))
    result: dict[str, Any] = {
        "status": "fail" if counts["BROKEN"] or counts["FAIL"] or counts["ERROR"] else ("warn" if counts["WARN"] else "pass"),
        "summary": dict(parts=sum(len(v) for v in names.values()), unique_shapes=len(names), full_holes=n_holes,
                        computed=len(todo), cached=len(names) - len(todo), workers=n_workers if todo else 0,
                        **{k.lower(): v for k, v in counts.items()}),
        "thresholds": opts,
        "rows": rows[:max_rows],
        "timing_s": {"prepare": round(t_prep, 2), "source_reused": bool(manifest.get("reused")), "check": round(time.time() - t1, 2),
                     "total": round(time.time() - t0, 2)},
    }
    if len(rows) > max_rows:
        result["rows_truncated"] = len(rows) - max_rows
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(dict(result, rows=rows), indent=1), encoding="utf-8")
        result["report_path"] = str(report_path)
    return result
