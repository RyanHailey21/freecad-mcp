"""Build GUI-side scripts (library + JSON params + call) and parse their JSON reply.

Every tool that runs inside the FreeCAD GUI sends one self-contained script
through ``execute_code``: the library source, the caller's parameters as a JSON
*literal* (never interpolated as code) and one function call whose result is
printed between markers.  Helper libraries the main library imports (e.g. the
pure ASME rules and the layout engine for TechDraw) travel as ``modules``: their
source is a string literal installed into ``sys.modules`` before the library runs.
"""

from __future__ import annotations

import json
from typing import Any

BEGIN, END = "FCMCP_JSON_BEGIN", "FCMCP_JSON_END"

_RUNNER = r'''
import json as _json
import traceback as _tb
try:
    _res = {"ok": True, "result": %(fn)s(PARAMS)}
except Exception as _exc:
    _res = {"ok": False, "error": "%%s: %%s" %% (type(_exc).__name__, _exc), "traceback": _tb.format_exc()[-2500:]}
print("%(begin)s" + _json.dumps(_res, default=str) + "%(end)s")
'''


_MODULE = r'''
import sys as _sys
import types as _types


def _fcmcp_module(_name, _src):
    _m = _types.ModuleType(_name)
    _m.__file__ = "<fcmcp:%s>" % _name
    exec(compile(_src, "<fcmcp:%s>" % _name, "exec"), _m.__dict__)
    _sys.modules[_name] = _m
    return _m
'''


def build_script(library: str, fn: str, params: dict[str, Any], modules: dict[str, str] | None = None) -> str:
    if not fn.isidentifier():
        raise ValueError("bad function name")
    pre = ""
    if modules:
        if not all(name.isidentifier() for name in modules):
            raise ValueError("bad module name")
        pre = _MODULE + "".join("_fcmcp_module(%r, %r)\n" % (name, src) for name, src in modules.items())
    return ("import json\nPARAMS = json.loads(" + repr(json.dumps(params)) + ")\n" + pre + library + "\n"
            + _RUNNER % {"fn": fn, "begin": BEGIN, "end": END})


def parse_reply(message: str) -> dict[str, Any]:
    start, end = message.rfind(BEGIN), message.rfind(END)
    if start < 0 or end < start:
        return {"ok": False, "error": "no JSON reply from FreeCAD", "output": message[-2000:]}
    return json.loads(message[start + len(BEGIN):end])


def run_gui(freecad: Any, library: str, fn: str, params: dict[str, Any], timeout: float = 90,
            modules: dict[str, str] | None = None) -> Any:
    """Run ``fn(PARAMS)`` in the GUI; return its result or raise RuntimeError with the error."""
    res = freecad.execute_code(build_script(library, fn, params, modules), timeout)
    if not res.get("success"):
        raise RuntimeError(f"FreeCAD execution failed: {res.get('error')}")
    reply = parse_reply(res.get("message", ""))
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "unknown error") + ("\n" + reply["traceback"] if reply.get("traceback") else ""))
    return reply["result"]


def run_headless_lib(command: list[str] | None, library: str, fn: str, params: dict[str, Any],
                     timeout: float = 600, modules: dict[str, str] | None = None) -> Any:
    """Same as run_gui but in a fresh ``freecadcmd`` process."""
    from .headless import run_headless

    res = run_headless(build_script(library, fn, params, modules), timeout, command)
    reply = parse_reply(res.get("output", ""))
    if not reply.get("ok"):
        err = reply.get("error") or res.get("error") or "headless run failed"
        raise RuntimeError(err + ("\n" + reply.get("traceback", "") if reply.get("traceback") else "")
                           + ("\n" + res.get("output", "")[-1500:] if not reply.get("traceback") else ""))
    return reply["result"]
