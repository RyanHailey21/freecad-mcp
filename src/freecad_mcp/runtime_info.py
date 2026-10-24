"""Read-only process provenance. Capture loaded revision once, not on every status call."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def revision():
    if not (ROOT / ".git").exists():
        return None
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def source_hash():
    h = hashlib.sha256()
    for p in sorted(Path(__file__).parent.rglob("*.py")):
        h.update(str(p.relative_to(Path(__file__).parent)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def source_dirty():
    if not (ROOT / ".git").exists():
        return None
    try:
        return bool(subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--", "src"],
            text=True, stderr=subprocess.DEVNULL, timeout=2).strip())
    except (OSError, subprocess.SubprocessError):
        return None


LOADED_REVISION = revision()
LOADED_DIRTY = source_dirty()
LOADED_HASH = source_hash()


def toolchain_status(expected_revision: str = ""):
    installed = revision()
    changed = source_hash() != LOADED_HASH
    result = {
        "schema": "cad-toolchain/1",
        "producer": "freecad-mcp",
        "pid": os.getpid(),
        "python": sys.executable,
        "source_root": str(ROOT),
        "running_revision": LOADED_REVISION,
        "running_source_dirty": LOADED_DIRTY,
        "installed_source_dirty": source_dirty(),
        "installed_revision": installed,
        "running_source_sha256": LOADED_HASH,
        "source_changed_since_start": changed,
        "expected_revision": expected_revision or None,
        "restart_required": changed or installed != LOADED_REVISION,
        "expected_revision_matches": None
        if not expected_revision
        else LOADED_REVISION == expected_revision and LOADED_DIRTY is False,
        "capabilities": ["execute_code", "diagnose_saved_assembly"],
        "freecad_rpc": "not_probed",
    }
    return result
