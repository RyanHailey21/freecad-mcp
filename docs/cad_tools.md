# CAD workflow tools

[Back to README](../README.md) · [Tools](tools.md) · [Code execution](execution.md)

Reusable tools for the slow, repetitive parts of a CAD project: clash and
manufacturability checks, TechDraw drawings, simulation renders and assembly
health checks. They are built for speed:

* **Headless + parallel** – checks run in `freecadcmd` worker processes
  (default `cpu - 2`, max 8), never on the GUI thread.
* **Cached** – results are keyed by geometry fingerprints (and, for clashes,
  the pair's relative placement), so unchanged parts are never recomputed.
  If none of the shape source's files changed, the source is not even
  re-evaluated (no FreeCAD launch).
* **Chunked GUI work** – drawing exports and frame captures are one GUI call
  per page / chunk, so nothing hits the 90 s GUI budget.
* **Background jobs** – anything slow takes `background=true` and returns a
  `job_id`; poll with `job_status(job_id, wait_s=60)`, stop with `job_cancel`.
* **Compact replies** – summaries and worst cases; full detail goes to
  `report_path` JSON files when asked for.

Caches live in `~/.cache/freecad-mcp` (override with `FREECAD_MCP_CACHE`);
`clear_check_cache` empties them.

## Tools

| Tool | Runs | Purpose |
| --- | --- | --- |
| `interference_check` | headless | Pairwise clash (overlap volume) and optional clearance check over many poses. |
| `hole_edge_check` | headless | Hole edge distance (FAIL/WARN) and broken-out holes per part. |
| `clear_check_cache` | server | Drop cached clash / hole results or exported BREPs. |
| `job_status` / `job_cancel` | server | Progress, ETA and result of background jobs. |
| `techdraw_list_templates` | GUI | Bundled TechDraw templates (ASME, ISO, ...). |
| `techdraw_create_page` / `techdraw_set_fields` | GUI | Page from template, fill editable fields (title / tolerance block). |
| `techdraw_add_views` | GUI | Part, projection-group, section and detail views with correct scale/position. |
| `techdraw_view_geometry` | GUI | Compact circles / lines / extreme vertices of a view, to pick references. |
| `techdraw_annotate` | GUI | Dimensions (incl. basic), datum symbols, FCFs, hole callouts, cosmetic geometry, notes. |
| `techdraw_export` | GUI | PDF/SVG (+ PNG review image) for many pages, one GUI call per page. |
| `techdraw_part_drawing` | GUI | One-call reduced-dimension part drawing(s) from a BREP/STEP or object: ASME rules, overlap-free layout, exported. |
| `techdraw_lint` | GUI | Read-only sheet check: label / leader / dimension-line conflicts, datums, metric text. |
| `techdraw_verify` | headless | Read-only model-vs-callout check of a saved sheet (counts, depths, FAR SIDE, csk angle, uncalled holes). |
| `render_frames` | GUI | Assembly-simulation and/or camera-path frames in chunks; optional MP4. |
| `encode_video` | server | PNG sequence → H.264 MP4 (ffmpeg / imageio-ffmpeg / Blender). |
| `assembly_attach_view_providers` | GUI | Re-attach joint/simulation view providers in headless-built assemblies. |
| `assembly_solve_check` | GUI or headless | Solve and report part drift from stored placements. |
| `assembly_simulation_check` | GUI | Generate a simulation, step frames, flag solver jumps. |

## Shape sources (checks)

`interference_check` and `hole_edge_check` take a `source`:

```json
{"fcstd": "C:/proj/asm.FCStd", "include": "Bracket|Plate", "exclude": "^Ref_", "group_by": "container"}
{"files": ["a.step", "b.brep"]}
{"python": "C:/proj/cad/provider.py", "function": "shapes", "poses_function": "poses", "args": {"which": "chk"}}
```

* **fcstd** – leaf solids of a saved document (features inside a Body or
  consumed by a boolean are represented by the result), global placements.
  Poses: `{"placements": {"Obj": [x, y, z, ax, ay, az, deg]}}`.
* **files** – BREP/STEP/IGES; multi-solid files are split into `name#i`.
* **python** – a provider module for parametric / scripted models:

```python
def poses():                       # optional: default pose list
    return ["STOW", "REACH"]

def shapes(pose, **args):          # body-frame shape + body placement => best cache reuse
    return [{"name": "Link1", "shape": link1_shape, "placement": body_placement, "group": "UPPER"}, ...]
```

Return each shape in its rigid-body frame plus the body placement: the cache
then recognises pairs whose *relative* pose did not change (all pairs on one
rigid body, static parts) and computes them once for every pose. Edits to the
provider or any module it imports (or files listed in `source["deps"]`)
invalidate the prepared shapes automatically.

### Typical calls

```json
// full motion sweep, everything against everything (cached afterwards)
{"source": {...}, "skip_same_group": false}
// after editing the belt drive: only belt parts against everything
{"source": {...}, "only": "Belt|Pulley"}
// intended press fits ignored, 1 mm clearance reported
{"source": {...}, "ignore": [["Bearing", "Housing"]], "clearance_mm": 1.0}
// manufacturing check on the real (holed) shapes
{"source": {"python": "...", "args": {"which": "vis"}}, "fail_wall_mm": 1.0, "warn_wall_mm": 1.5}
```

## TechDraw

Coordinates: view `x`/`y` and note positions are page mm; balloon
`origin`/`at`, `leader_to` and cosmetic geometry are model mm from the view
centre (Y up); dimension `at` is a page-mm offset from the view centre.
`page.addView()` resets a view's X/Y/Scale, so the tools set them afterwards
with `ScaleType="Custom"`. Pages are opened in the GUI before PDF export
(otherwise the template is not drawn). Review the PDF/PNG, not the SVG (third
party SVG rasterisers shift balloon text).

GD&T uses native objects only: datum feature symbols and feature control
frames are `DrawViewBalloon`s with a Rectangle bubble (`|` separates FCF
compartments, `KinkLength = 0`), hole callouts are balloons with no bubble and
an arrow, basic dimensions use `TheoreticalExact`. Text tokens such as
`{dia}`, `{mmc}`, `{cbore}`, `{depth}`, `{pm}`, `{deg}` and every symbol name
(`{position}`, `{flatness}`, ...) expand to their Unicode symbols.

References for dimensions can be names (`"Edge3"`) or geometric queries:
`{"circle": 6.0, "center": [x, y]}`, `{"vertex": [x, y]}`,
`{"line": [[x0, y0], [x1, y1]]}`, `{"extreme": "xmin"}`.

```json
{"drawings": [{"doc": "DWG_101", "brep": "C:/proj/fab/brep/hub.brep", "stem": "ARC-101_Hub",
  "fields": {"DrawingTitle1": "J1 OUTPUT HUB", "drawing_number": "ARC-101"},
  "density_g_cm3": 2.7, "position_tolerance": "0.05", "profile_tolerance": "0.1",
  "tapped": {"1.25": "M3x0.5-6H"}, "notes": ["MATERIAL: 6061-T6 ALUMINUM."]}],
 "out_dir": "C:/proj/drawings/out", "png_dpi": 110, "background": true}
```

`process` (built-ins `machined`, `plate`, `printed`, `shaft`, `shim`, or one
defined in `config.processes`) picks default tolerances, notes and whether MMC,
ISO 286 fits, hidden lines, shaft and flatness rules apply. Project specifics
travel in `config` / `config_path`: processes, `extra_notes`, `plain_names`,
`hole_overrides`, `projected_heights` (`{"M3": 6}` or `{"<part>|M3": 6}`,
default 6 mm with a warning) and `bearings`. Feature recognition is the
headless module `fcworkers/fcmcp_features.py` (fixture tests in
`tests/test_features.py`), sent to the GUI with each call.

### Rules and layout engine

`guiside/asme_rules.py` (pure functions, unit-tested) and `guiside/layout.py`
(placement on TechDraw's own Qt scene) are ported from the light_weight_arm
drawing generator and sent with every TechDraw call as the modules
`fcmcp_asme_rules` / `fcmcp_layout`. The rules follow ASME Y14.5-2018 §4.3.1
(metric values), §4.4.4 / §4.4.4.2 (leaders, radial to circles), Fig 6-30
(FCF below the hole callout), App. B-4 and §10.4.1 (fixed fastener with a
projected zone). ISO 286 H7/k6 values cover 3-50 mm and are not verified
against the standard itself; Y14.41, Y14.100 and Y14.2 are not claimed.

The layout reads object labels: `Datum_*` and leaderless `FCF_g<n>` carry no
free leader, items whose labels end in `_g<n>` move as one unit, balloons with
`Anchors` / `AnchorR` (techdraw_annotate `anchors` / `anchor_r`) get their
leader re-attached radially to the best pattern instance. `techdraw_annotate`
with `layout=true`, and `techdraw_part_drawing` (default), snap datum symbols
onto their FCFs, try several placement orders, and report the remaining
`conflicts` exactly as `techdraw_lint` does. The engine is tuned on the ANSI B
sheet; other sizes are scaled, or pass `frame` / `keepout` boxes (page mm,
Y up). A clean lint is necessary, not sufficient: look at the PNG too.
Workflow guidance: `skills/techdraw-drawings/SKILL.md`.

PNG review images need PyMuPDF (`pip install freecad-mcp[drawings]`, or any
Python on PATH that has it; `FREECAD_MCP_PDF_PYTHON` picks one).

## Renders and video

`render_frames(doc_name, out_dir, simulation="Simulation", frame_step=5,
camera={"keys": [...]} | {"orbit": {...}}, encode_fps=30)` generates the
Assembly simulation once, then captures frames in chunks with
`updateForFrame` + `view.saveImage`. Joint glyphs are hidden while capturing
(`hide_joints`), and the camera is re-framed with `fitAll()` unless `fit:
false` or an orthographic `height` is given. MP4 encoding uses
`FREECAD_MCP_FFMPEG`, `imageio-ffmpeg` (`pip install freecad-mcp[video]`),
`ffmpeg` on PATH, or Blender's bundled FFmpeg (`FREECAD_MCP_BLENDER`).

Assembly simulations need the GUI: headless FreeCAD cannot restore the
Python simulation/motion objects (their module imports Qt).

## Measured speed-ups (light_weight_arm, 297 shapes, 14 poses, 12-core PC)

| Task | Ad hoc script | New tool |
| --- | --- | --- |
| Clash check, STOW all pairs + 14 task poses | `check_interference.py` §1-2: 110 s (3641 booleans, 1 process) | cold 47.5 s (booleans 7.7 s on 8 workers; 35 s is the model build) · unchanged model 1.5 s |
| Re-check changed parts (`only="Belt\|Pulley\|Rocker"`) | `quick_pairs.py`: 47 s | 0.3 s (cached) to 13 s (new booleans) without a model change; model rebuild + changed pairs after an edit |
| Hole edge check, 44 parts | `check_holes.py`: 49 s | cold 58 s (check phase 11 s on 8 workers) · unchanged 0.1 s · after an edit only changed parts are re-checked |
| Part drawing with GD&T + PDF + PNG | `batch_drawings.py`: ~12 s/part plus hand-written driver calls | `techdraw_part_drawing`: 8.3 s/part, one call |
| Assembly solve drift, 343 parts | ad hoc GUI script | `assembly_solve_check(path=...)`: 4.8 s headless |

Cold runs are dominated by evaluating the model itself; a busy machine
(another FreeCAD session) made the same cold runs 3-4x slower. Booleans are
deduplicated across poses, so the full sweep needs 2336 unique booleans for
all pairs at all 14 poses.
