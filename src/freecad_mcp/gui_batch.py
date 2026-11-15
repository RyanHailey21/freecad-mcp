"""Parallel FreeCAD GUI batch runner (``gui_batch`` / ``techdraw_batch``).

One FreeCAD GUI handles about one TechDraw sheet per 30-90 s and runs one job at a time.  This runner
starts N *private* FreeCAD GUI processes, each runs a user script on its own slice of work items and
exits; results come back as one job (job_status / progress).

Isolation: every child gets its own ``FREECAD_USER_HOME`` (a private profile under the cache dir), so the
MCP addon (it lives in the user's Mod folder) is not loaded there - no RPC server, nothing on port 9875 -
and the shared GUI instance is never touched.  The profile is seeded with a COPY of the user's user.cfg
(``user_cfg="auto"``): TechDraw renders with the user's preferences (with factory defaults a leaderless
feature control frame showed its zero-length leader as a line through the symbol cells).  The user's
own user.cfg is only read, never written.

Work items: ``{"id": str, "args": <json>, "fingerprint": str?, "outputs": [paths]?}`` (a bare JSON value
is wrapped as ``{"id": str(i), "args": value}``).  The child exec()s ``scripts`` in order into one
namespace and calls ``entry(args)`` - or ``entry(**args)`` with ``kwargs=True``.  An item whose
fingerprint equals the last successful run's and whose outputs all exist is skipped.

Time budget: the parent watches each child's current item; an item over ``item_timeout`` is reported as
``timeout``, that child is killed and a fresh one continues with the rest of its slice.  A child that
dies mid-item gets the item marked ``crashed`` and is restarted the same way.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .freecad_launcher import find_freecad_executable
from .parallel import cache_root

GB = 1024 ** 3
PER_INSTANCE_GB = 1.5          # resident size of one FreeCAD GUI running a TechDraw sheet (conservative)
RESERVE_GB = 2.0               # keep this much RAM free for the OS / the shared FreeCAD
STARTUP_TIMEOUT = 180.0        # s for a child to start its first item

CHILD = r'''
# fcmcp gui_batch child (runs inside a private FreeCAD GUI)
import json, os, sys, time, traceback
import FreeCAD as App

_P = json.load(open(os.environ["FCMCP_BATCH_PARAMS"], encoding="utf-8"))
_DIR = _P["status_dir"]


def _w(name, data):
    tmp = os.path.join(_DIR, name + ".tmp%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=str)
    os.replace(tmp, os.path.join(_DIR, name))


class _Tee:
    def __init__(self, path):
        self.fh = open(path, "a", encoding="utf-8", buffering=1)
    def write(self, s):
        self.fh.write(s)
    def flush(self):
        self.fh.flush()


def _run():
    sys.stdout = sys.stderr = _Tee(_P["log"])
    ns = {"__name__": "__fcmcp_batch__", "BATCH_ITEM": None}
    try:
        for sc in _P["scripts"]:
            ns["__file__"] = sc
            exec(compile(open(sc, encoding="utf-8").read(), sc, "exec"), ns)
        fn = ns[_P["entry"]]
    except Exception:
        _w("setup_error.%s.json" % _P["worker"], {"error": traceback.format_exc()[-3000:]})
        os._exit(3)
    for it in _P["items"]:
        _w("current.%s.json" % _P["worker"], {"id": it["id"], "t": time.time(), "pid": os.getpid()})
        t0 = time.time()
        ns["BATCH_ITEM"] = it
        try:
            a = it.get("args")
            res = fn(**a) if (_P.get("kwargs") and isinstance(a, dict)) else fn(a)
            out = {"id": it["id"], "status": "ok", "s": round(time.time() - t0, 1), "result": res}
        except Exception as exc:
            out = {"id": it["id"], "status": "error", "s": round(time.time() - t0, 1),
                   "error": "%s: %s" % (type(exc).__name__, exc), "traceback": traceback.format_exc()[-3000:]}
        try:
            json.dumps(out.get("result"))
        except Exception:
            out["result"] = repr(out.get("result"))[:2000]
        _w("done.%s.json" % hashlib.sha1(it["id"].encode()).hexdigest()[:16], out)
        for name in list(App.listDocuments()):          # every item starts from a clean session
            try:
                App.closeDocument(name)
            except Exception:
                pass
    _w("exit.%s.json" % _P["worker"], {"t": time.time()})
    os._exit(0)


import hashlib
from PySide import QtCore
QtCore.QTimer.singleShot(300, _run)
'''


def user_cfg_path() -> Path | None:
    """The user's FreeCAD preferences (user.cfg): newest versioned profile first (v1-1/...), then the flat one."""
    roots = []
    if sys.platform == "win32":
        roots.append(Path(os.environ.get("APPDATA", "")) / "FreeCAD")
    else:
        roots += [Path.home() / ".config" / "FreeCAD", Path.home() / ".local" / "share" / "FreeCAD",
                  Path.home() / "Library" / "Application Support" / "FreeCAD"]
    for root in roots:
        if not root.is_dir():
            continue
        versioned = sorted((d for d in root.glob("v*-*") if (d / "user.cfg").is_file()), reverse=True)
        for c in [d / "user.cfg" for d in versioned] + [root / "user.cfg"]:
            if c.is_file():
                return c
    return None


def free_ram_bytes() -> int | None:
    if sys.platform == "win32":
        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMSTAT()
        m.dwLength = ctypes.sizeof(MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return int(m.ullAvailPhys)
        return None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def instance_cap(requested: int, per_instance_gb: float = PER_INSTANCE_GB, reserve_gb: float = RESERVE_GB,
                 free: int | None = None) -> int:
    """Instances that fit in free RAM (keeping a reserve), at least 1, at most ``requested``."""
    free = free_ram_bytes() if free is None else free
    if free is None:
        return max(1, requested)
    fit = int((free / GB - reserve_gb) // per_instance_gb)
    return max(1, min(requested, fit))


def normalise_items(items: list[Any]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for i, it in enumerate(items):
        if not (isinstance(it, dict) and "args" in it):
            it = {"args": it}
        it = dict(it)
        it["id"] = str(it.get("id", i))
        if it["id"] in seen:
            raise ValueError("duplicate item id %r" % it["id"])
        seen.add(it["id"])
        out.append(it)
    return out


def split_slices(items: list[dict[str, Any]], n: int) -> list[list[dict[str, Any]]]:
    """Round-robin slices (neighbouring items usually cost alike, so this balances better than chunks)."""
    n = max(1, min(n, len(items)))
    return [items[k::n] for k in range(n)]


class SkipCache:
    """Last successful fingerprint per (batch name, item id)."""

    def __init__(self, name: str, root: Path | None = None):
        self.path = (root or cache_root() / "gui_batch") / ("%s.json" % hashlib.sha1(name.encode()).hexdigest()[:16])
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def unchanged(self, it: dict[str, Any]) -> bool:
        fp = it.get("fingerprint")
        return bool(fp) and self.data.get(it["id"]) == fp and all(os.path.exists(o) for o in it.get("outputs") or [])

    def record(self, it: dict[str, Any]) -> None:
        if it.get("fingerprint"):
            self.data[it["id"]] = it["fingerprint"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        os.replace(tmp, self.path)


def _kill(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    else:
        p.kill()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        pass


def _done_name(item_id: str) -> str:
    return "done.%s.json" % hashlib.sha1(item_id.encode()).hexdigest()[:16]


class _Worker:
    def __init__(self, idx: int, items: list[dict[str, Any]], run_dir: Path, spec: dict[str, Any]):
        self.idx, self.todo, self.run_dir, self.spec = idx, list(items), run_dir, spec
        self.proc: subprocess.Popen | None = None
        self.launches = 0
        self.started = 0.0
        self.home = run_dir / ("home%d" % idx)

    @property
    def name(self) -> str:
        return "w%d_%d" % (self.idx, self.launches)

    def launch(self, exe: str) -> None:
        self.launches += 1
        self.home.mkdir(parents=True, exist_ok=True)
        ucfg = self.spec.get("user_cfg")
        if ucfg and not (self.home / "user.cfg").exists():
            import shutil
            shutil.copyfile(ucfg, self.home / "user.cfg")       # read-only use of the user's preferences
        params = dict(self.spec, items=self.todo, worker=self.name, status_dir=str(self.run_dir),
                      log=str(self.run_dir / ("log_w%d.txt" % self.idx)))
        pf = self.run_dir / ("params_%s.json" % self.name)
        pf.write_text(json.dumps(params), encoding="utf-8")
        env = dict(os.environ, FREECAD_USER_HOME=str(self.home), FCMCP_BATCH_PARAMS=str(pf))
        kw: dict[str, Any] = {"env": env, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                              "stdin": subprocess.DEVNULL, "cwd": str(self.run_dir)}
        if sys.platform == "win32" and self.spec.get("minimized", True):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 7                       # SW_SHOWMINNOACTIVE: no focus stealing
            kw["startupinfo"] = si
        self.proc = subprocess.Popen([exe, str(self.run_dir / "child.py")], **kw)
        self.started = time.time()

    def current(self) -> dict[str, Any] | None:
        f = self.run_dir / ("current.%s.json" % self.name)
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def run_batch(scripts: list[str], entry: str, items: list[Any], instances: int = 3, item_timeout: float = 600.0,
              kwargs: bool = False, name: str = "", skip_unchanged: bool = True, executable: str | None = None,
              minimized: bool = True, job: Any = None, cancel: Any = None, per_instance_gb: float = PER_INSTANCE_GB,
              poll: float = 0.5, user_cfg: str | None = "auto") -> dict[str, Any]:
    exe = executable or find_freecad_executable()
    if not exe:
        raise RuntimeError("FreeCAD executable not found (set FREECAD_EXE)")
    scripts = [str(Path(s).resolve()) for s in scripts]
    for s in scripts:
        if not os.path.isfile(s):
            raise FileNotFoundError(s)
    items_n = normalise_items(items)
    name = name or "|".join(scripts) + "#" + entry
    skip = SkipCache(name)
    results: dict[str, dict[str, Any]] = {}
    todo = []
    for it in items_n:
        if skip_unchanged and skip.unchanged(it):
            results[it["id"]] = {"id": it["id"], "status": "skipped", "reason": "fingerprint unchanged"}
        else:
            todo.append(it)
    n_req = max(1, int(instances))
    n = min(instance_cap(n_req, per_instance_gb), max(1, len(todo)))
    run_dir = cache_root() / "gui_batch" / ("run_" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "child.py").write_text(CHILD, encoding="utf-8")
    cfg = user_cfg_path() if user_cfg == "auto" else (Path(user_cfg) if user_cfg else None)
    spec = {"scripts": scripts, "entry": entry, "kwargs": bool(kwargs), "minimized": minimized,
            "user_cfg": str(cfg) if cfg and cfg.is_file() else None}
    workers = [_Worker(k, sl, run_dir, spec) for k, sl in enumerate(split_slices(todo, n))] if todo else []
    t0 = time.time()
    total = len(items_n)

    def progress(msg: str = "") -> None:
        if job is not None:
            job.progress(len(results), total, msg)

    for w in workers:
        w.launch(exe)
    progress("%d instance(s) started" % len(workers))
    try:
        while any(w.todo for w in workers):
            if cancel is not None and cancel.is_set():
                for w in workers:
                    if w.proc:
                        _kill(w.proc)
                for w in workers:
                    for it in w.todo:
                        results.setdefault(it["id"], {"id": it["id"], "status": "cancelled"})
                    w.todo = []
                break
            for w in workers:
                if not w.todo:
                    continue
                # collect finished items
                for it in list(w.todo):
                    f = run_dir / _done_name(it["id"])
                    if f.exists():
                        try:
                            r = json.loads(f.read_text(encoding="utf-8"))
                        except ValueError:
                            continue
                        results[it["id"]] = dict(r, worker=w.idx)
                        if r.get("status") == "ok":
                            skip.record(it)
                        w.todo.remove(it)
                        progress("%s %s" % (it["id"], r.get("status")))
                if not w.todo:
                    continue
                setup_err = run_dir / ("setup_error.%s.json" % w.name)
                if setup_err.exists():
                    err = json.loads(setup_err.read_text(encoding="utf-8"))["error"]
                    for it in w.todo:
                        results[it["id"]] = {"id": it["id"], "status": "error", "error": "script setup failed",
                                             "traceback": err, "worker": w.idx}
                    w.todo = []
                    continue
                cur = w.current()
                alive = w.proc is not None and w.proc.poll() is None
                now = time.time()
                if cur and cur.get("id") in {i["id"] for i in w.todo}:
                    if now - float(cur["t"]) > item_timeout:
                        _kill(w.proc)
                        results[cur["id"]] = {"id": cur["id"], "status": "timeout", "s": round(now - float(cur["t"]), 1),
                                              "error": "over the %g s item budget: instance killed, rest of its slice "
                                                       "continues in a new instance" % item_timeout, "worker": w.idx}
                        w.todo = [i for i in w.todo if i["id"] != cur["id"]]
                        progress("%s timeout" % cur["id"])
                        if w.todo:
                            w.launch(exe)
                        continue
                    if not alive:
                        time.sleep(poll)                 # a done file may land just after exit
                        if (run_dir / _done_name(cur["id"])).exists():
                            continue
                        results[cur["id"]] = {"id": cur["id"], "status": "crashed", "worker": w.idx,
                                              "error": "FreeCAD exited during the item (rc %s)" % w.proc.returncode}
                        w.todo = [i for i in w.todo if i["id"] != cur["id"]]
                        progress("%s crashed" % cur["id"])
                        if w.todo:
                            w.launch(exe)
                        continue
                else:
                    # no item running: still starting up, or between two items (closing documents)
                    since = w.started if cur is None else max(w.started, float(cur["t"]))
                    stuck = now - since > (STARTUP_TIMEOUT if cur is None else STARTUP_TIMEOUT + item_timeout)
                    if alive and not stuck:
                        continue
                    if alive:
                        _kill(w.proc)
                    if not alive and cur is not None and w.launches <= len(items_n) + 1:
                        time.sleep(poll)
                        if any((run_dir / _done_name(i["id"])).exists() for i in w.todo):
                            continue                     # it finished another item just before exiting
                        w.launch(exe)                    # exited between items: continue without blame
                        continue
                    it = w.todo.pop(0)                   # never started its item: blame that item once
                    results[it["id"]] = {"id": it["id"], "status": "crashed", "worker": w.idx,
                                         "error": "FreeCAD did not start the item (rc %s)" % (w.proc.returncode if w.proc else None)}
                    if w.todo:
                        w.launch(exe)
            time.sleep(poll)
    finally:
        for w in workers:
            if w.proc is not None and w.proc.poll() is None:
                try:
                    w.proc.wait(20)                       # child exits by itself after its last item
                except subprocess.TimeoutExpired:
                    _kill(w.proc)
        skip.save()
    for w in workers:                                # attach each worker's log tail to its failures
        log = run_dir / ("log_w%d.txt" % w.idx)
        tail = log.read_text(encoding="utf-8", errors="replace")[-1500:] if log.exists() else ""
        for r in results.values():
            if r.get("worker") == w.idx and r.get("status") not in ("ok", "skipped"):
                r.setdefault("log_tail", tail)
    ordered = [results.get(it["id"], {"id": it["id"], "status": "missing"}) for it in items_n]
    counts: dict[str, int] = {}
    for r in ordered:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"instances": len(workers), "requested_instances": n_req, "user_cfg": spec["user_cfg"], "elapsed_s": round(time.time() - t0, 1),
            "counts": counts, "run_dir": str(run_dir), "items": ordered}
