"""Encode numbered PNG frames to an H.264 MP4.

Encoder search order: ``FREECAD_MCP_FFMPEG``; ``imageio-ffmpeg``'s bundled
binary (``pip install freecad-mcp[video]``); ``ffmpeg`` on PATH; Blender's
bundled FFmpeg through its Video Sequencer (``FREECAD_MCP_BLENDER`` or
``blender`` on PATH / standard install folders).
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_BLENDER_SCRIPT = r'''
import os, sys, bpy
a = sys.argv[sys.argv.index("--") + 1:]
src, out, fps, crf = a[0], a[1], int(a[2]), a[3]
files = sorted(f for f in os.listdir(src) if f.lower().endswith(".png") and f.startswith(a[4]))
sc = bpy.context.scene
sc.sequence_editor_create()
seqs = sc.sequence_editor.strips if hasattr(sc.sequence_editor, "strips") else sc.sequence_editor.sequences
strip = seqs.new_image(name="frames", filepath=os.path.join(src, files[0]), channel=1, frame_start=1)
for f in files[1:]:
    strip.elements.append(f)
sc.frame_start, sc.frame_end = 1, len(files)
sc.render.fps = fps
img = bpy.data.images.load(os.path.join(src, files[0]))
sc.render.resolution_x, sc.render.resolution_y = img.size[0] // 2 * 2, img.size[1] // 2 * 2
sc.render.resolution_percentage = 100
try:
    sc.render.image_settings.media_type = "VIDEO"
except Exception:
    pass
sc.render.image_settings.file_format = "FFMPEG"
sc.render.ffmpeg.format = "MPEG4"
sc.render.ffmpeg.codec = "H264"
sc.render.ffmpeg.constant_rate_factor = crf
sc.render.filepath = out
bpy.ops.render.render(animation=True)
'''


def find_ffmpeg() -> str | None:
    env = os.environ.get("FREECAD_MCP_FFMPEG")
    if env and Path(env).is_file():
        return env
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


def find_blender() -> str | None:
    env = os.environ.get("FREECAD_MCP_BLENDER")
    if env and Path(env).is_file():
        return env
    exe = shutil.which("blender")
    if exe:
        return exe
    pats = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
            pats.append(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe"))
            pats.append(os.path.join(base, "Programs", "Blender*", "blender.exe"))
    else:
        pats += ["/Applications/Blender.app/Contents/MacOS/Blender", "/usr/bin/blender", "/snap/bin/blender"]
    hits = sorted(h for p in pats for h in glob.glob(p))
    return hits[-1] if hits else None


def _pattern_info(frames_dir: Path, pattern: str) -> tuple[str, int, int]:
    """printf pattern 'frame_%05d.png' -> (prefix, first number, count)."""
    m = re.fullmatch(r"(.*)%0?(\d*)d(.*)", pattern)
    if not m:
        raise ValueError("pattern must contain a %d / %05d field, e.g. frame_%05d.png")
    prefix, _w, suffix = m.groups()
    nums = sorted(int(f.name[len(prefix):len(f.name) - len(suffix)]) for f in frames_dir.iterdir()
                  if f.name.startswith(prefix) and f.name.endswith(suffix)
                  and f.name[len(prefix):len(f.name) - len(suffix)].isdigit())
    if not nums:
        raise FileNotFoundError(f"no frames matching {pattern} in {frames_dir}")
    return prefix, nums[0], len(nums)


def encode_video(frames_dir: str, out_path: str, fps: int = 30, pattern: str = "frame_%05d.png",
                 crf: int = 18, encoder: str = "auto", timeout: float = 1800) -> dict[str, Any]:
    src = Path(frames_dir)
    prefix, first, count = _pattern_info(src, pattern)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg() if encoder in ("auto", "ffmpeg") else None
    if ffmpeg:
        argv = [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps), "-start_number", str(first),
                "-i", str(src / pattern), "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-crf", str(crf), "-movflags", "+faststart", str(out)]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr[-1500:]}")
        used = "ffmpeg:" + ffmpeg
    else:
        blender = find_blender() if encoder in ("auto", "blender") else None
        if not blender:
            raise RuntimeError("no encoder: install imageio-ffmpeg (pip install freecad-mcp[video]), put ffmpeg on "
                               "PATH, or set FREECAD_MCP_FFMPEG / FREECAD_MCP_BLENDER")
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(_BLENDER_SCRIPT)
            script = f.name
        try:
            crf_name = "HIGH" if crf <= 20 else ("MEDIUM" if crf <= 26 else "LOW")
            proc = subprocess.run([blender, "-b", "-P", script, "--", str(src), str(out), str(fps), crf_name, prefix],
                                  capture_output=True, text=True, timeout=timeout)
        finally:
            os.unlink(script)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"blender encode failed: {(proc.stdout + proc.stderr)[-1500:]}")
        used = "blender:" + blender
    return {"video": str(out), "frames": count, "fps": fps, "duration_s": round(count / fps, 2),
            "size_kb": round(out.stat().st_size / 1024, 1), "encoder": used}
