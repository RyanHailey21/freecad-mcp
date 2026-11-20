"""In-process background job queue for long MCP tool calls.

Long checks (interference sweeps, batch drawing export, frame rendering) can
take minutes. Started with ``background=True`` they run in a server thread and
return a job id at once; ``job_status`` reports progress and, when done, the
compact result. Jobs live in memory until the MCP server exits.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


def _budget_from_env() -> float:
    try:
        value = float(os.environ.get("FREECAD_MCP_SYNC_BUDGET_S", "50"))
    except ValueError:
        return 50.0
    return value if value == value and value >= 0 else 50.0  # NaN/negative -> default


# MCP clients abort a tool call at their own request timeout (Claude Code: about
# 60 s, whatever the tool's ``timeout`` argument says) and the result is lost.
# A call still running after SYNC_BUDGET_S is therefore handed to the job queue
# and the client gets a job id instead. 0 disables the hand-off.
SYNC_BUDGET_S = _budget_from_env()
# job_status must itself answer inside the client's limit.
WAIT_CAP_S = max(1.0, (SYNC_BUDGET_S or 50.0) - 5.0)


@dataclass
class Job:
    id: str
    name: str
    state: str = "running"  # running | done | failed | cancelled
    started: float = field(default_factory=time.time)
    finished: float | None = None
    done: int = 0
    total: int = 0
    message: str = ""
    result: Any = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    exc: BaseException | None = field(default=None, repr=False)
    finished_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def progress(self, done: int, total: int, message: str = "") -> None:
        self.done, self.total = int(done), int(total)
        if message:
            self.message = message

    def summary(self, with_result: bool = True) -> dict[str, Any]:
        end = self.finished or time.time()
        out: dict[str, Any] = {
            "job_id": self.id, "name": self.name, "state": self.state,
            "elapsed_s": round(end - self.started, 1),
        }
        if self.total:
            out["progress"] = f"{self.done}/{self.total}"
            if self.state == "running" and self.done:
                rate = (end - self.started) / self.done
                out["eta_s"] = round(rate * (self.total - self.done), 1)
        if self.message:
            out["message"] = self.message
        if self.error:
            out["error"] = self.error
        if with_result and self.state == "done":
            out["result"] = self.result
        return out


class JobManager:
    MAX_HISTORY = 50

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, name: str, fn: Callable[[Job], Any], register: bool = True) -> Job:
        """Run ``fn(job)`` in a daemon thread; ``fn`` reports via job.progress and checks job.cancel.

        ``register=False`` keeps the job out of the list until ``register(job)``
        (used by ``run_with_deadline`` so fast calls leave no history).
        """
        job = Job(id=uuid.uuid4().hex[:10], name=name)

        def run() -> None:
            try:
                job.result = fn(job)
                job.state = "cancelled" if job.cancel.is_set() else "done"
            except Exception as exc:  # report, never crash the server
                job.exc = exc
                job.state = "cancelled" if job.cancel.is_set() else "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                if job.state == "failed":
                    job.error += "\n" + traceback.format_exc(limit=6)
            finally:
                job.finished = time.time()
                job.finished_event.set()

        if register:
            self.register(job)
        threading.Thread(target=run, name=f"job-{name}-{job.id}", daemon=True).start()
        return job

    def register(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            self._trim()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)

    def wait(self, job_id: str, seconds: float) -> Job | None:
        job = self.get(job_id)
        if job is not None:
            job.finished_event.wait(max(0.0, seconds))
        return job

    def _trim(self) -> None:
        finished = [j for j in self._jobs.values() if j.state != "running"]
        finished.sort(key=lambda j: j.started)
        for j in finished[: max(0, len(self._jobs) - self.MAX_HISTORY)]:
            self._jobs.pop(j.id, None)


JOBS = JobManager()


class _Inline:
    """Stand-in for Job when a tool runs synchronously."""

    def __init__(self) -> None:
        self.cancel = threading.Event()

    def progress(self, done: int, total: int, message: str = "") -> None:
        pass


def run_or_queue(name: str, fn: Callable[[Any], Any], background: bool) -> Any:
    """Run ``fn`` now (returns its result) or queue it (returns a job summary dict)."""
    if not background:
        return fn(_Inline())
    job = JOBS.start(name, fn)
    return {"job_id": job.id, "state": job.state,
            "hint": f"poll job_status(job_id) (wait_s up to {WAIT_CAP_S:g} blocks until done)"}


def run_with_deadline(name: str, fn: Callable[[], Any], budget_s: float | None = None,
                      manager: JobManager | None = None) -> tuple[str, Any]:
    """Run ``fn()``; if it outlasts ``budget_s``, keep it running as a job.

    Returns ``("done", result)`` when ``fn`` finishes in time (its exception is
    re-raised unchanged), or ``("job", job)`` when it is still running; the job
    is then listed by job_status and keeps the result when it finishes.
    """
    budget = SYNC_BUDGET_S if budget_s is None else budget_s
    if budget <= 0:
        return "done", fn()
    mgr = manager or JOBS
    job = mgr.start(name, lambda _job: fn(), register=False)
    if job.finished_event.wait(budget):
        if job.exc is not None:
            raise job.exc
        return "done", job.result
    job.message = ("handed off after %gs (client time limit); the work cannot be interrupted, "
                   "only its result dropped" % budget)
    mgr.register(job)
    return "job", job
