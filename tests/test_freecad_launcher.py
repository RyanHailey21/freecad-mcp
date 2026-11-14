"""On-demand FreeCAD launch: never at server start, once per need, detached."""

import subprocess
import xmlrpc.client

import pytest

from freecad_mcp import freecad_launcher as fl
from freecad_mcp import server


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(fl, "_last_launch", None)
    monkeypatch.setattr(fl, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(fl.time, "sleep", lambda _s: None)
    monkeypatch.delenv("FREECAD_MCP_AUTOSTART", raising=False)
    monkeypatch.delenv("FREECAD_EXE", raising=False)
    server.state.freecad_connection = None
    yield
    server.state.freecad_connection = None


def test_already_running_does_not_launch(monkeypatch):
    monkeypatch.setattr(fl, "rpc_reachable", lambda *a: True)
    monkeypatch.setattr(fl, "_spawn", lambda exe: pytest.fail("must not launch"))
    result = fl.ensure_freecad_running("localhost")
    assert result.status == "already_running"


def test_launches_once_then_waits_for_rpc(monkeypatch):
    answers = iter([False, False, False, True])
    monkeypatch.setattr(fl, "rpc_reachable", lambda *a: next(answers))
    monkeypatch.setattr(fl, "find_freecad_executable", lambda: "freecad.exe")
    spawned = []
    monkeypatch.setattr(fl, "_spawn", lambda exe: spawned.append(exe))
    result = fl.ensure_freecad_running("localhost", wait=30)
    assert result.status == "started"
    assert spawned == ["freecad.exe"]


def test_second_call_does_not_spawn_a_second_freecad(monkeypatch):
    monkeypatch.setattr(fl, "_last_launch", fl.time.monotonic())  # still loading
    answers = iter([False, False, True])
    monkeypatch.setattr(fl, "rpc_reachable", lambda *a: next(answers))
    monkeypatch.setattr(fl, "_spawn", lambda exe: pytest.fail("must not spawn twice"))
    result = fl.ensure_freecad_running("localhost", wait=30)
    assert result.status == "already_running"


def test_remote_host_is_never_launched(monkeypatch):
    monkeypatch.setattr(fl, "rpc_reachable", lambda *a: False)
    monkeypatch.setattr(fl, "_spawn", lambda exe: pytest.fail("must not launch"))
    with pytest.raises(fl.FreeCADLaunchError, match="local host"):
        fl.ensure_freecad_running("10.0.0.5")


def test_autostart_can_be_disabled(monkeypatch):
    monkeypatch.setenv("FREECAD_MCP_AUTOSTART", "0")
    monkeypatch.setattr(fl, "rpc_reachable", lambda *a: False)
    monkeypatch.setattr(fl, "_spawn", lambda exe: pytest.fail("must not launch"))
    with pytest.raises(fl.FreeCADLaunchError, match="disabled"):
        fl.ensure_freecad_running("localhost")


def test_missing_executable_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(fl, "rpc_reachable", lambda *a: False)
    monkeypatch.setattr(fl, "find_freecad_executable", lambda: None)
    with pytest.raises(fl.FreeCADLaunchError, match="FREECAD_EXE"):
        fl.ensure_freecad_running("localhost")


def test_a_stale_launch_is_retried_after_the_guard_window(monkeypatch):
    monkeypatch.setattr(fl, "_last_launch", fl.time.monotonic() - fl.RELAUNCH_GUARD - 1)
    answers = iter([False, False, True])
    monkeypatch.setattr(fl, "rpc_reachable", lambda *a: next(answers))
    monkeypatch.setattr(fl, "find_freecad_executable", lambda: "freecad.exe")
    spawned = []
    monkeypatch.setattr(fl, "_spawn", lambda exe: spawned.append(exe))
    assert fl.ensure_freecad_running("localhost", wait=30).status == "started"
    assert spawned == ["freecad.exe"]


def test_timeout_when_rpc_never_comes_up(monkeypatch):
    monkeypatch.setattr(fl, "rpc_reachable", lambda *a: False)
    monkeypatch.setattr(fl, "find_freecad_executable", lambda: "freecad.exe")
    monkeypatch.setattr(fl, "_spawn", lambda exe: None)
    ticks = iter(range(0, 1000))
    monkeypatch.setattr(fl.time, "monotonic", lambda: next(ticks))
    with pytest.raises(fl.FreeCADLaunchError, match="did not answer"):
        fl.ensure_freecad_running("localhost", wait=3)


def test_http_rejection_counts_as_running(monkeypatch):
    class Conn:
        def __init__(self, *a, **k): ...
        def ping(self):
            raise xmlrpc.client.ProtocolError("u", 401, "Unauthorized", {})
        def disconnect(self): ...

    monkeypatch.setattr(fl, "FreeCADConnection", Conn)
    assert fl.rpc_reachable("localhost", 9875, None) is True


def test_spawn_is_detached_from_the_mcp_pipes(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured.update(kw)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    fl._spawn("freecad.exe")
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL


EXE = r"C:\FreeCAD\bin\freecad.exe"


def _windows(monkeypatch):
    monkeypatch.setattr(fl.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)


def test_windows_spawn_prefers_job_breakaway_through_a_shell_that_exits(monkeypatch):
    _windows(monkeypatch)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("WMI not needed"))
    fl._spawn(EXE)
    [(cmd, kw)] = calls
    assert cmd[:3] == ["cmd.exe", "/c", "start"] and cmd[-1] == EXE
    assert kw["creationflags"] & fl._CREATE_BREAKAWAY_FROM_JOB


def test_windows_spawn_uses_wmi_when_the_job_forbids_breakaway(monkeypatch):
    _windows(monkeypatch)

    def no_breakaway(cmd, **kw):
        raise OSError("access denied")

    runs = []

    class Done:
        returncode = 0

    monkeypatch.setattr(subprocess, "Popen", no_breakaway)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: runs.append(cmd) or Done())
    fl._spawn(EXE)
    [cmd] = runs
    assert cmd[0] == "powershell.exe" and "Win32_Process" in cmd[-1] and EXE in cmd[-1]


def test_windows_spawn_falls_back_to_plain_start_if_wmi_fails(monkeypatch):
    _windows(monkeypatch)
    popen_calls = []

    def popen(cmd, **kw):
        popen_calls.append(kw["creationflags"])
        if len(popen_calls) == 1:
            raise OSError("access denied")

    class Failed:
        returncode = 5

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Failed())
    fl._spawn(EXE)
    assert len(popen_calls) == 2
    assert not popen_calls[1] & fl._CREATE_BREAKAWAY_FROM_JOB


def test_tool_calls_launch_but_server_startup_does_not(monkeypatch):
    launches = []
    monkeypatch.setattr(server, "ensure_freecad_running", lambda *a, **k: launches.append(a))

    class Down:
        def __init__(self, *a, **k): ...
        def ping(self):
            raise OSError("refused")
        def disconnect(self): ...

    monkeypatch.setattr(server, "FreeCADConnection", Down)
    with pytest.raises(Exception):
        server.get_freecad_connection(launch=False)
    assert launches == []  # startup self-check never starts FreeCAD
    with pytest.raises(Exception):
        server.get_freecad_connection()
    assert len(launches) == 1  # a real tool call does


def test_start_freecad_tool_reports_failure_as_json(monkeypatch):
    def boom(*a, **k):
        raise fl.FreeCADLaunchError("nope")

    monkeypatch.setattr(server, "ensure_freecad_running", boom)
    [content] = server.start_freecad(None, wait_seconds=1)
    assert '"status": "failed"' in content.text and "nope" in content.text
