"""Calls that outlast the client's request timeout are handed to the job queue.

Observed on the arm project (2026-11-14..16): 57 execute_code/headless calls died
with the client's "Request timed out" at 60-70 s although they passed timeout=300..1800;
the work finished in FreeCAD but the result was lost and the calls were re-run.
"""

import json
import threading
import time

import pytest
from mcp.types import TextContent

from freecad_mcp import jobs, server
from freecad_mcp.jobs import JobManager, run_with_deadline


def test_fast_call_returns_inline_and_leaves_no_job() -> None:
    mgr = JobManager()
    assert run_with_deadline("fast", lambda: 42, budget_s=5, manager=mgr) == ("done", 42)
    assert mgr.list() == []


def test_fast_failure_reraises_original_exception() -> None:
    mgr = JobManager()

    def boom() -> None:
        raise ValueError("bad input: x")

    with pytest.raises(ValueError, match="bad input: x"):
        run_with_deadline("boom", boom, budget_s=5, manager=mgr)
    assert mgr.list() == []


def test_slow_call_becomes_a_job_that_keeps_its_result() -> None:
    mgr = JobManager()
    release = threading.Event()

    def slow() -> str:
        release.wait(5)
        return "finished"

    kind, job = run_with_deadline("slow", slow, budget_s=0.1, manager=mgr)
    assert kind == "job" and job.state == "running"
    assert mgr.get(job.id) is job
    release.set()
    assert mgr.wait(job.id, 5).state == "done"
    assert job.result == "finished"


def test_zero_budget_disables_handoff() -> None:
    assert run_with_deadline("x", lambda: "now", budget_s=0) == ("done", "now")


def test_wait_returns_as_soon_as_job_finishes() -> None:
    mgr = JobManager()
    job = mgr.start("quick", lambda _j: time.sleep(0.05) or 1)
    t = time.time()
    mgr.wait(job.id, 10)
    assert time.time() - t < 2 and job.state == "done"


def test_wait_cap_stays_under_client_limit() -> None:
    assert jobs.WAIT_CAP_S < 60


def test_tool_wrapper_hands_off_and_job_status_returns_original_content(monkeypatch) -> None:
    from freecad_mcp import cad_tools

    real = jobs.run_with_deadline
    monkeypatch.setattr(server, "run_with_deadline", lambda name, fn: real(name, fn, budget_s=0.1))
    release = threading.Event()
    reply = [TextContent(type="text", text="script output")]

    def _handoff_probe_tool() -> list[TextContent]:
        release.wait(5)
        return reply

    first = server.tool(_handoff_probe_tool)()  # the real wrapper (also registers the probe tool)
    body = json.loads(first[-1].text)
    assert body["status"] == "running" and body["tool"] == "_handoff_probe_tool"
    assert "job_status" in body["note"] and "Do not re-run" in body["note"]

    tools: dict = {}
    cad_tools.register(lambda f: tools.setdefault(f.__name__, f), server.state, lambda: None)
    release.set()
    out = tools["job_status"](job_id=body["job_id"], wait_s=5)
    assert json.loads(out[0].text)["state"] == "done"
    assert out[1:] == reply
