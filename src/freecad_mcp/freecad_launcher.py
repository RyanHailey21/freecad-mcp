"""Start the FreeCAD GUI on demand and wait for its MCP RPC addon.

FreeCAD is only launched when an agent actually needs it (the first tool call
that reaches FreeCAD, or an explicit ``start_freecad`` call), never when the
MCP server itself starts. The FreeCAD addon must have "auto start RPC"
enabled (the addon's default for this setup) so the RPC server comes up by
itself once the GUI has loaded.

Environment:
    FREECAD_MCP_AUTOSTART=0       disable all launching
    FREECAD_EXE=<path>            FreeCAD executable to launch
    FREECAD_MCP_START_TIMEOUT=<s> seconds to wait for the RPC server (default 120)
"""

import glob
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import xmlrpc.client
from dataclasses import dataclass

from .freecad_client import FreeCADConnection


logger = logging.getLogger("FreeCADMCPserver")

DEFAULT_START_TIMEOUT = 120.0
POLL_INTERVAL = 1.0
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000  # Windows process creation flag

RELAUNCH_GUARD = 180.0  # seconds a fresh launch is given before another is allowed

_launch_lock = threading.Lock()
_last_launch: float | None = None  # monotonic time of the last spawn


class FreeCADLaunchError(Exception):
    """FreeCAD could not be started or did not become ready in time."""


@dataclass
class LaunchResult:
    status: str  # "already_running" | "started" | "starting"
    detail: str


def autostart_enabled() -> bool:
    return os.environ.get("FREECAD_MCP_AUTOSTART", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def default_timeout() -> float:
    try:
        value = float(os.environ.get("FREECAD_MCP_START_TIMEOUT", DEFAULT_START_TIMEOUT))
    except ValueError:
        return DEFAULT_START_TIMEOUT
    return value if value > 0 else DEFAULT_START_TIMEOUT


def is_local_host(host: str) -> bool:
    return host.strip().lower() in _LOCAL_HOSTS


def rpc_reachable(host: str, port: int, token: str | None) -> bool:
    """True when something answers the FreeCAD RPC port.

    An HTTP-level rejection (for example 401 for a missing token) still means
    FreeCAD is up, so it counts as reachable.
    """
    connection = FreeCADConnection(host=host, port=port, timeout=3, token=token)
    try:
        connection.ping()
        return True
    except xmlrpc.client.ProtocolError:
        return True
    except Exception:
        return False
    finally:
        connection.disconnect()


def find_freecad_executable() -> str | None:
    override = os.environ.get("FREECAD_EXE", "").strip()
    if override:
        return override if os.path.isfile(override) else None

    on_path = shutil.which("freecad")
    if on_path:
        return on_path

    candidates: list[str] = []
    if sys.platform == "win32":
        roots = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ]
        for root in roots:
            if root:
                candidates += sorted(
                    glob.glob(os.path.join(root, "FreeCAD*", "bin", "freecad.exe")),
                    reverse=True,  # newest-looking version directory first
                )
    elif sys.platform == "darwin":
        candidates.append("/Applications/FreeCAD.app/Contents/MacOS/FreeCAD")
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _spawn(executable: str) -> subprocess.Popen | None:
    """Launch FreeCAD fully detached from this server's stdio.

    The MCP server speaks over stdin/stdout, so the child must not inherit
    those pipes, and it must keep running if the MCP client closes the server.
    """
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
        return subprocess.Popen([executable], **kwargs)

    # MCP clients and launchers (uv's freecad-mcp.exe, the MCP SDK's stdio
    # client) tear down the server's whole process tree on exit, by job object
    # or by parent PID. FreeCAD is the user's application and may hold unsaved
    # work, so it must not die with the MCP session. `cmd /c start` hands the
    # launch to a short-lived shell that exits at once, leaving FreeCAD with
    # no live parent in the server's tree; breakaway from any job is attempted
    # first and skipped if the job forbids it.
    command = ["cmd.exe", "/c", "start", "", executable]
    flags = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(command, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        logger.info("FreeCAD launch path: shell hand-off with job breakaway")
        return proc
    except OSError:
        pass
    # The job forbids breakaway (the MCP SDK's stdio client does this), so any
    # process we create would be killed with the session. Ask the WMI service
    # to create FreeCAD instead: it is not our child and sits outside the job.
    try:
        _spawn_via_wmi(executable)
        logger.info("FreeCAD launch path: WMI (outside the session job)")
        return None
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not start FreeCAD outside the session; it may close with it")
    return subprocess.Popen(command, creationflags=flags, **kwargs)


def _spawn_via_wmi(executable: str) -> None:
    quoted = executable.replace("'", "''")
    script = (
        f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine = '\"{quoted}\"'}}; exit [int]$r.ReturnValue"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=30,
    )
    if result.returncode != 0:
        raise OSError(f"WMI could not create the FreeCAD process (code {result.returncode})")


def ensure_freecad_running(
    host: str,
    port: int = 9875,
    token: str | None = None,
    wait: float | None = None,
) -> LaunchResult:
    """Make sure FreeCAD's RPC server is answering, launching FreeCAD if needed.

    Raises FreeCADLaunchError when launching is disabled, impossible, or the
    RPC server did not come up within ``wait`` seconds (default
    FREECAD_MCP_START_TIMEOUT). Safe to call from concurrent tool threads: only
    one FreeCAD is ever spawned, later callers wait for the same one.
    """
    global _last_launch

    if rpc_reachable(host, port, token):
        return LaunchResult("already_running", "FreeCAD RPC server is already running.")

    if not is_local_host(host):
        raise FreeCADLaunchError(
            f"FreeCAD RPC at {host}:{port} is not reachable, and FreeCAD is only "
            "auto-started for a local host. Start it on that machine."
        )
    if not autostart_enabled():
        raise FreeCADLaunchError(
            "FreeCAD is not running and auto-start is disabled "
            "(FREECAD_MCP_AUTOSTART=0). Start FreeCAD with its RPC server."
        )

    budget = wait if wait is not None else default_timeout()
    with _launch_lock:
        # Another thread may have finished the launch while this one waited.
        if rpc_reachable(host, port, token):
            return LaunchResult("already_running", "FreeCAD RPC server is already running.")

        spawned_now = False
        # A launch is still loading if it began recently: keep waiting for it
        # instead of opening a second FreeCAD (the launching shell is gone, so
        # there is no process handle to ask).
        loading = (
            _last_launch is not None
            and time.monotonic() - _last_launch < RELAUNCH_GUARD
        )
        if not loading:
            executable = find_freecad_executable()
            if not executable:
                raise FreeCADLaunchError(
                    "FreeCAD is not running and its executable was not found. "
                    "Install FreeCAD or set FREECAD_EXE to the path of freecad.exe."
                )
            logger.info(f"Starting FreeCAD: {executable}")
            try:
                _spawn(executable)
            except OSError as exc:
                raise FreeCADLaunchError(f"Could not start FreeCAD ({exc}).") from exc
            _last_launch = time.monotonic()
            spawned_now = True

        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if rpc_reachable(host, port, token):
                time.sleep(1.0)  # let the GUI settle before the first command
                return LaunchResult(
                    "started" if spawned_now else "already_running",
                    "FreeCAD was started and its RPC server is ready.",
                )
            time.sleep(POLL_INTERVAL)

    raise FreeCADLaunchError(
        f"FreeCAD was launched but its RPC server did not answer within {budget:.0f}s. "
        "Check that the FreeCAD MCP addon has 'auto start RPC' enabled "
        "(FreeCAD MCP workbench > Toggle Auto Start), then retry."
    )
