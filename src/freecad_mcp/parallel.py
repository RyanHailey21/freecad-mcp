"""Run the headless worker scripts in ``fcworkers/`` across parallel ``freecadcmd`` processes.

Each worker gets a JSON params file and writes a JSON result file; progress is
read from small per-worker progress files so long jobs can report status.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from .headless import _clean, detect_freecadcmd

WORKERS_DIR = Path(__file__).with_name("fcworkers")
ProgressFn = Callable[[int, int, str], None]


class WorkerError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def cache_root() -> Path:
    d = Path(os.environ.get("FREECAD_MCP_CACHE") or (Path.home() / ".cache" / "freecad-mcp"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 2))


def _bootstrap(script: str, params_file: str) -> str:
    # Plain data only: paths are embedded as repr() literals.
    return (
        "import builtins, sys\n"
        f"builtins.FCMCP_PARAMS_FILE = {params_file!r}\n"
        f"sys.path.insert(0, {str(WORKERS_DIR)!r})\n"
        f"exec(compile(open({str(WORKERS_DIR / script)!r}, encoding='utf-8').read(), "
        f"{script!r}, 'exec'), {{'__name__': '__main__'}})\n"
    )


def run_workers(
    script: str,
    param_sets: Sequence[dict[str, Any]],
    command: list[str] | None = None,
    timeout: float = 1800,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    label: str = "",
) -> list[dict[str, Any]]:
    """Start one ``freecadcmd`` per params dict, wait for all, return their JSON results.

    ``progress(done, total, msg)`` is called about twice a second with the sum of
    the workers' progress counters. Raises WorkerError when a worker exits
    without writing its result (crash, exception) and Cancelled on ``cancel``.
    """
    command = command or detect_freecadcmd()
    if not command:
        raise WorkerError("freecadcmd not found: install FreeCAD, set FREECAD_MCP_EXECUTABLE or pass --freecadcmd")
    tmp_root = cache_root() / "tmp"  # under HOME so Flatpak sandboxes can see it
    tmp_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="fcmcp-", dir=tmp_root))
    procs = []
    totals = []
    try:
        for i, params in enumerate(param_sets):
            stem = work / f"w{i}"
            params = dict(params, out=str(stem) + ".out.json", progress=str(stem) + ".progress")
            pfile = str(stem) + ".in.json"
            Path(pfile).write_text(json.dumps(params), encoding="utf-8")
            code_file = str(stem) + ".py"
            Path(code_file).write_text(_bootstrap(script, pfile), encoding="utf-8")
            log = open(str(stem) + ".log", "w", encoding="utf-8", errors="replace")
            argv = command + ["-c", f"exec(open({code_file!r}, encoding='utf-8').read())"]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=flags if os.name == "nt" else 0)
            procs.append((p, params, log, stem))
            totals.append(len(params.get("tasks", [])) or 1)
        total = sum(totals)
        deadline = time.time() + timeout
        while True:
            alive = [p for p, *_ in procs if p.poll() is None]
            if progress:
                done = 0
                for (p, params, _log, stem), n in zip(procs, totals):
                    if p.poll() is not None:
                        done += n
                        continue
                    try:
                        done += int(Path(params["progress"]).read_text().split()[0])
                    except (OSError, ValueError, IndexError):
                        pass
                progress(done, total, label)
            if not alive:
                break
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled")
            if time.time() > deadline:
                raise WorkerError(f"{script}: workers did not finish within {timeout:g} s")
            time.sleep(0.25)
        results = []
        for p, params, log, stem in procs:
            log.close()
            out = Path(params["out"])
            if not out.exists():
                text = _clean(Path(str(stem) + ".log").read_text(encoding="utf-8", errors="replace"))
                raise WorkerError(f"{script} worker exited {p.returncode} without a result:\n{text[-3000:]}")
            results.append(json.loads(out.read_text(encoding="utf-8")))
        return results
    finally:
        for p, _params, log, _stem in procs:
            if p.poll() is None:
                p.kill()
                try:
                    p.wait(5)
                except Exception:
                    pass
            if not log.closed:
                log.close()
        if not os.environ.get("FREECAD_MCP_KEEP_WORK"):
            for f in work.glob("*"):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                work.rmdir()
            except OSError:
                pass


def split_balanced(items: Sequence[Any], cost: Callable[[Any], float], n: int) -> list[list[Any]]:
    """Greedy longest-processing-time split of ``items`` into at most ``n`` non-empty bins."""
    n = max(1, min(n, len(items)))
    bins: list[list[Any]] = [[] for _ in range(n)]
    loads = [0.0] * n
    for it in sorted(items, key=cost, reverse=True):
        k = loads.index(min(loads))
        bins[k].append(it)
        loads[k] += cost(it)
    return [b for b in bins if b]


class JsonCache:
    """Small persistent key -> value cache in a JSON file, merged on save (multi-process safe enough)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {}
        self.new: dict[str, Any] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def put(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.new[key] = value

    def save(self) -> None:
        if not self.new:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        merged = {}
        if self.path.exists():
            try:
                merged = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                merged = {}
        merged.update(self.new)
        tmp = self.path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(merged), encoding="utf-8")
        os.replace(tmp, self.path)
        self.data = merged
        self.new = {}
