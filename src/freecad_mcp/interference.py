"""Fast, cached, parallel interference (clash) checking in headless FreeCAD.

Pipeline:
1. *prepare* (one ``freecadcmd``): evaluate the shape source at every pose,
   export unseen shapes to a content-addressed BREP store, return per-pose
   placements and world bounding boxes.
2. *select* (here, no FreeCAD): all pairs per pose, filtered by name regex,
   same-group rule, ignore list and a bounding-box prefilter.
3. *cache*: a pair's overlap depends only on the two body-frame geometries and
   their relative placement, so ``fpA|fpB|rel(A,B)`` keys a persistent cache.
   Identical keys across poses (parts on the same rigid body, static parts)
   collapse to one boolean.
4. *compute* (N parallel ``freecadcmd`` workers): boolean common volume for the
   remaining unique keys, cost-balanced by face counts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .parallel import JsonCache, cache_root, default_workers, run_workers, split_balanced

CACHE_VERSION = "2"   # 2: geometry-hash fingerprints (moved holes no longer reuse stale BREPs)


def _inv_mul(a: list[float], b: list[float]) -> list[float]:
    """inverse(A) * B for 3x4 rigid transforms stored row-major (12 floats)."""
    ra = [[a[0], a[1], a[2]], [a[4], a[5], a[6]], [a[8], a[9], a[10]]]
    ta = [a[3], a[7], a[11]]
    rb = [[b[0], b[1], b[2]], [b[4], b[5], b[6]], [b[8], b[9], b[10]]]
    tb = [b[3], b[7], b[11]]
    # R = Ra^T Rb, t = Ra^T (tb - ta)
    out = []
    d = [tb[k] - ta[k] for k in range(3)]
    for i in range(3):
        row = [sum(ra[k][i] * rb[k][j] for k in range(3)) for j in range(3)]
        row.append(sum(ra[k][i] * d[k] for k in range(3)))
        out.extend(row)
    return out


def relkey(a: list[float], b: list[float], decimals: int = 3) -> str:
    fmt = "%%.%df" % decimals
    return ",".join((fmt % v).replace("-0." + "0" * decimals, "0." + "0" * decimals)
                    for v in _inv_mul(a, b))


def bbox_hit(a: list[float], b: list[float], gap: float = 0.0) -> bool:
    return (a[0] - gap <= b[3] and b[0] - gap <= a[3] and a[1] - gap <= b[4] and b[1] - gap <= a[4]
            and a[2] - gap <= b[5] and b[2] - gap <= a[5])


def _compile(p: str | None) -> re.Pattern[str] | None:
    return re.compile(p) if p else None


def _ignored(a: str, b: str, ignore: list[Any]) -> bool:
    for rule in ignore:
        if isinstance(rule, str):
            if re.search(rule, a) or re.search(rule, b):
                return True
        else:
            ra, rb = rule
            if (re.search(ra, a) and re.search(rb, b)) or (re.search(ra, b) and re.search(rb, a)):
                return True
    return False


def select_pairs(
    manifest: dict[str, Any],
    only: str | None = None,
    against: str | None = None,
    skip_same_group: bool = False,
    ignore: list[Any] | None = None,
    clearance: float = 0.0,
) -> tuple[list[tuple[int, str, str, str, str, dict]], dict[str, int]]:
    """Pairs that survive filters and the bbox prefilter: (pose, nameA, nameB, key, task) tuples."""
    rx_only, rx_against = _compile(only), _compile(against)
    ignore = ignore or []
    out = []
    stats = {"pairs": 0, "bbox_pass": 0}
    for pi, pose in enumerate(manifest["poses"]):
        items = pose["items"]
        sel = [bool(rx_only.search(it[0])) if rx_only else True for it in items]
        oth = [bool(rx_against.search(it[0])) if rx_against else True for it in items]
        for i in range(len(items)):
            na, ga, fa, ma, ba = items[i]
            for j in range(i + 1, len(items)):
                nb, gb, fb, mb, bb = items[j]
                if not ((sel[i] and oth[j]) or (sel[j] and oth[i])):
                    continue
                if skip_same_group and ga == gb:
                    continue
                if ignore and _ignored(na, nb, ignore):
                    continue
                stats["pairs"] += 1
                if not bbox_hit(ba, bb, clearance):
                    continue
                stats["bbox_pass"] += 1
                # canonical order so (A,B) and (B,A) share a cache entry (fresh names: the outer
                # loop's A must not be overwritten)
                if (fb, nb) < (fa, na):
                    n1, n2, f1, f2, m1, m2 = nb, na, fb, fa, mb, ma
                else:
                    n1, n2, f1, f2, m1, m2 = na, nb, fa, fb, ma, mb
                key = f"{CACHE_VERSION}|{f1}|{f2}|{relkey(m1, m2)}|c{clearance:g}"
                out.append((pi, n1, n2, key, {"fa": f1, "ma": m1, "fb": f2, "mb": m2}))
    return out, stats


def _stat(path: str) -> list[float] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _manifest_cache_file(spec: dict[str, Any]) -> Path:
    h = hashlib.sha1(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:20]
    return cache_root() / "manifests" / f"{h}.json"


def cached_manifest(spec: dict[str, Any]) -> dict[str, Any] | None:
    """A previous prepare result for the same spec whose dependency files and BREPs are unchanged."""
    f = _manifest_cache_file(spec)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    deps = data.get("deps") or {}
    if not deps or any(_stat(p) != st for p, st in deps.items()):
        return None
    m = data["manifest"]
    if any(not os.path.exists(s["brep"]) for s in m["shapes"].values()):
        return None
    return m


def prepare(source: dict[str, Any], poses: list[Any] | None, command: list[str] | None,
            timeout: float, tight_bbox: bool = False, cancel=None, reuse: bool = True) -> dict[str, Any]:
    """Evaluate the source (headless) or reuse the last manifest when no dependency file changed."""
    spec = {"v": CACHE_VERSION, "source": source, "poses": poses, "tight": tight_bbox}
    if reuse and not source.get("no_manifest_cache"):
        m = cached_manifest(spec)
        if m is not None:
            m = dict(m, reused=True)
            return m
    store = cache_root() / "breps"
    t0 = time.time()
    (manifest,) = run_workers("fcmcp_prepare.py", [{
        "source": source, "poses": poses, "store": str(store), "tight_bbox": tight_bbox,
    }], command, timeout, cancel=cancel, label="prepare")
    if manifest.get("errors"):
        raise RuntimeError("shape source failed:\n" + "\n".join(manifest["errors"])[-4000:])
    deps = {p: _stat(p) for p in manifest.pop("deps", [])}
    # A file edited while the source was being evaluated must not validate this manifest.
    if deps and all(st is not None and st[0] / 1e9 < t0 for st in deps.values()):
        f = _manifest_cache_file(spec)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"deps": deps, "manifest": manifest}), encoding="utf-8")
        os.replace(tmp, f)
    manifest["dependency_files"] = len(deps)
    return manifest


def interference_check(
    source: dict[str, Any],
    poses: list[Any] | None = None,
    only: str | None = None,
    against: str | None = None,
    skip_same_group: bool = False,
    ignore: list[Any] | None = None,
    tolerance: float = 0.5,
    clearance: float = 0.0,
    workers: int = 0,
    use_cache: bool = True,
    cache_path: str | None = None,
    tight_bbox: bool = False,
    report_path: str | None = None,
    max_results: int = 50,
    command: list[str] | None = None,
    timeout: float = 3600,
    job: Any = None,
) -> dict[str, Any]:
    t0 = time.time()
    cancel = getattr(job, "cancel", None)
    report = (lambda d, t, m: job.progress(d, t, m)) if job is not None else None
    if report:
        report(0, 0, "preparing shapes")
    manifest = prepare(source, poses, command, timeout, tight_bbox, cancel, reuse=use_cache)
    t_prep = time.time() - t0
    pairs, stats = select_pairs(manifest, only, against, skip_same_group, ignore, clearance)
    cache = JsonCache(Path(cache_path) if cache_path else cache_root() / "interference_cache.json")
    if not use_cache:
        cache.data = {}
    unique: dict[str, dict] = {}
    for _pi, _a, _b, key, task in pairs:
        if key not in cache and key not in unique:
            unique[key] = task
    n_keys = len({p[3] for p in pairs})
    faces = {fp: s["faces"] for fp, s in manifest["shapes"].items()}
    breps = {fp: s["brep"] for fp, s in manifest["shapes"].items()}
    todo = [[k, t["fa"], t["ma"], t["fb"], t["mb"], clearance] for k, t in unique.items()]
    n_workers = workers or default_workers()
    # A worker costs ~1-2 s to start; only fan out when there is enough work.
    n_workers = max(1, min(n_workers, len(todo) // 4 or 1))
    t1 = time.time()
    worker_stats = []
    if todo:
        bins = split_balanced(todo, lambda t: faces[t[1]] * faces[t[3]] + 50, n_workers)
        params = [{"tasks": b, "breps": {fp: breps[fp] for t in b for fp in (t[1], t[3])}} for b in bins]
        if report:
            report(0, len(todo), f"{len(todo)} booleans on {len(bins)} workers")
        results = run_workers("fcmcp_clash.py", params, command, timeout, report, cancel, "booleans")
        for r in results:
            for k, v in r["results"].items():
                cache.put(k, v)
            worker_stats.append({"n": len(r["results"]), "s": r["elapsed"], "errors": r.get("errors", [])[:3]})
        cache.save()
    t_comp = time.time() - t1

    labels = [p["label"] for p in manifest["poses"]]
    worst: dict[tuple[str, str], dict[str, Any]] = {}
    near: dict[tuple[str, str], dict[str, Any]] = {}
    failed: dict[tuple[str, str], str] = {}
    per_pose = [0] * len(labels)
    for pi, a, b, key, _task in pairs:
        v, d = cache.get(key)
        pair = (a, b) if a <= b else (b, a)
        if v < 0:
            failed.setdefault(pair, labels[pi])
            continue
        if v > tolerance:
            per_pose[pi] += 1
            w = worst.get(pair)
            if w is None:
                worst[pair] = {"a": pair[0], "b": pair[1], "volume_mm3": round(v, 2), "worst_pose": labels[pi],
                               "poses_hit": 1}
            else:
                w["poses_hit"] += 1
                if v > w["volume_mm3"]:
                    w["volume_mm3"], w["worst_pose"] = round(v, 2), labels[pi]
        elif clearance and d is not None and 0 <= d < clearance:
            n = near.get(pair)
            if n is None or d < n["distance_mm"]:
                near[pair] = {"a": pair[0], "b": pair[1], "distance_mm": round(d, 3), "pose": labels[pi]}
    overlaps = sorted(worst.values(), key=lambda w: -w["volume_mm3"])
    result: dict[str, Any] = {
        "status": "clash" if overlaps or failed else "clear",
        "summary": {
            "poses": len(labels), "shapes": len(manifest["shapes"]),
            "pairs_considered": stats["pairs"], "bbox_overlaps": stats["bbox_pass"],
            "unique_booleans": n_keys, "cache_hits": n_keys - len(todo), "computed": len(todo), "workers": len(worker_stats),
            "overlapping_pairs": len(overlaps), "failed_booleans": len(failed),
            "tolerance_mm3": tolerance,
        },
        "overlaps": overlaps[:max_results],
        "timing_s": {"prepare": round(t_prep, 2),
                     "source_build": "reused (no dependency changed)" if manifest.get("reused")
                     else manifest.get("elapsed", {}).get("build"),
                     "booleans": round(t_comp, 2), "total": round(time.time() - t0, 2)},
    }
    if len(overlaps) > max_results:
        result["overlaps_truncated"] = len(overlaps) - max_results
    if clearance:
        result["clearance_violations"] = sorted(near.values(), key=lambda n: n["distance_mm"])[:max_results]
        result["summary"]["clearance_mm"] = clearance
    if failed:
        result["failed_booleans"] = [{"a": a, "b": b, "pose": p} for (a, b), p in list(failed.items())[:max_results]]
    if len(labels) > 1:
        result["overlaps_per_pose"] = {l: n for l, n in zip(labels, per_pose) if n}
    if worker_stats:
        result["worker_stats"] = worker_stats
    if report_path:
        full = dict(result, overlaps=overlaps, all_pairs=[
            {"pose": labels[pi], "a": a, "b": b, "volume_mm3": cache.get(k)[0], "distance_mm": cache.get(k)[1]}
            for pi, a, b, k, _t in pairs])
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(full, indent=1), encoding="utf-8")
        result["report_path"] = str(report_path)
    return result


def clear_cache(cache_path: str | None = None) -> int:
    path = Path(cache_path) if cache_path else cache_root() / "interference_cache.json"
    n = 0
    if path.exists():
        try:
            n = len(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            pass
        path.unlink()
    return n


def iter_names(manifest: dict[str, Any]) -> Iterable[str]:
    for pose in manifest["poses"]:
        for it in pose["items"]:
            yield it[0]
