"""Fast, reusable CAD-workflow tools: checks, drawings, renders, assembly helpers, job queue.

Registered on the main server by ``register(tool, state, get_connection)``.
Headless tools run in parallel ``freecadcmd`` workers and never touch the GUI;
GUI tools send one self-contained script per call (each well inside the GUI
time budget) and loop in the server for long batches. Anything slow accepts
``background=True`` and returns a job id for ``job_status``.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Callable

from mcp.types import ImageContent, TextContent

from .gui_lib import run_gui, run_headless_lib
from .jobs import JOBS, run_or_queue

_GUISIDE = Path(__file__).with_name("guiside")


def _lib(name: str) -> str:
    return (_GUISIDE / f"{name}.py").read_text(encoding="utf-8")


# helper modules the TechDraw library imports inside FreeCAD (pure ASME rules, Qt-scene layout engine)
TECHDRAW_MODULES = {"fcmcp_asme_rules": "asme_rules", "fcmcp_layout": "layout"}
_FCWORKERS = Path(__file__).with_name("fcworkers")


def techdraw_modules() -> dict[str, str]:
    mods = {mod: _lib(src) for mod, src in TECHDRAW_MODULES.items()}
    # feature recognition is pure Part: the same module runs headless (tests, workers) and in the GUI
    mods["fcmcp_features"] = (_FCWORKERS / "fcmcp_features.py").read_text(encoding="utf-8")
    return mods


def _text(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=1, default=str))]


def _err(exc: Exception) -> list[TextContent]:
    return _text({"status": "error", "error": f"{type(exc).__name__}: {exc}"})


def _image(path: str) -> ImageContent:
    return ImageContent(type="image", data=base64.b64encode(Path(path).read_bytes()).decode(), mimeType="image/png")


def register(tool: Callable, state: Any, get_connection: Callable[[], Any]) -> None:  # noqa: C901
    # ================================================================== job queue
    @tool
    def job_status(job_id: str = "", wait_s: float = 0) -> list[TextContent]:
        """Progress / result of background jobs started with ``background=True``.

        Args:
            job_id: Job id; empty lists all jobs (newest first, results omitted).
            wait_s: Block up to this many seconds (max 60) for the job to finish before answering,
                so polling needs few calls.
        """
        if not job_id:
            return _text([j.summary(with_result=False) for j in JOBS.list()])
        job = JOBS.wait(job_id, min(60.0, max(0.0, wait_s)))
        if job is None:
            return _text({"status": "error", "error": f"unknown job {job_id}"})
        return _text(job.summary())

    @tool
    def job_cancel(job_id: str) -> list[TextContent]:
        """Cancel a running background job (kills its headless workers; GUI chunks stop after the current one)."""
        job = JOBS.get(job_id)
        if job is None:
            return _text({"status": "error", "error": f"unknown job {job_id}"})
        job.cancel.set()
        return _text({"job_id": job_id, "state": "cancelling" if job.state == "running" else job.state})

    # ================================================================== headless checks
    @tool
    def interference_check(
        source: dict[str, Any],
        poses: list[Any] | None = None,
        only: str | None = None,
        against: str | None = None,
        skip_same_group: bool = False,
        ignore: list[Any] | None = None,
        tolerance_mm3: float = 0.5,
        clearance_mm: float = 0.0,
        workers: int = 0,
        use_cache: bool = True,
        tight_bbox: bool = False,
        report_path: str | None = None,
        max_results: int = 50,
        background: bool = False,
        timeout: float = 3600,
    ) -> list[TextContent]:
        """Pairwise clash check of shapes at one or many poses, headless, cached and parallel.

        Pipeline: one freecadcmd evaluates ``source`` at every pose and exports unseen shapes to a
        content-addressed BREP store; pairs are filtered by name, group and world bounding box; each
        remaining pair is keyed by both body-frame geometry fingerprints + their relative placement and
        looked up in a persistent cache (~/.cache/freecad-mcp/interference_cache.json); only new keys are
        booleaned, spread over parallel freecadcmd workers. Pairs that keep their relative pose across
        poses (same rigid body, static parts) are computed once. The GUI is never touched.

        Args:
            source: Where shapes come from (one of):
                {"fcstd": "C:/p/asm.FCStd", "include": regex, "exclude": regex,
                 "group_by": "object"|"container", "visible_only": false}  - leaf solids, global placements;
                {"files": ["a.step", "b.brep"]};
                {"python": "provider.py", "function": "shapes", "poses_function": "poses", "args": {...}}
                  where shapes(pose, **args) returns [{"name", "shape", "placement", "group"}] with the
                  shape in its body frame (best cache reuse) - see docs/cad_tools.md.
            poses: Pose values passed to the provider (null = poses_function() or one as-saved pose). For
                FCStd: {"placements": {obj: [x,y,z, ax,ay,az, deg]}} or {"simulation": name, "frame": i}.
            only: Regex; check only pairs where at least one part name matches (re-check changed parts
                against everything).
            against: Regex the other part must match.
            skip_same_group: Skip pairs within one group (rigid body / container).
            ignore: Known/intended overlaps: regex strings (any pair involving a match) or [regexA, regexB].
            tolerance_mm3: Overlap volume above which a pair is reported (face contact reports 0).
            clearance_mm: >0 also reports non-overlapping pairs closer than this (slower: distToShape).
            workers: Parallel freecadcmd processes (0 = auto, cpu-2 capped at 8).
            use_cache: False recomputes everything (results are still written back).
            tight_bbox: Use optimal (tight) bounding boxes in the prefilter (slower prepare, fewer booleans).
            report_path: Also write every checked pair to this JSON file.
            max_results: Cap on listed overlaps.
            background: Return a job id at once; poll job_status.
        Returns: status clear/clash, summary (pairs, bbox hits, unique booleans, cache hits, computed,
            workers), worst overlap per pair across poses, timings.
        """
        from .interference import interference_check as run

        def work(job: Any) -> Any:
            return run(source, poses, only, against, skip_same_group, ignore, tolerance_mm3, clearance_mm,
                       workers, use_cache, None, tight_bbox, report_path, max_results, state.freecadcmd,
                       timeout, job)

        try:
            return _text(run_or_queue("interference_check", work, background))
        except Exception as exc:
            return _err(exc)

    @tool
    def hole_edge_check(
        source: dict[str, Any],
        pose: Any = None,
        only: str | None = None,
        fail_wall_mm: float = 1.0,
        warn_wall_mm: float = 1.5,
        warn_ratio: float = 0.5,
        min_diameter: float = 2.0,
        max_diameter: float = 12.0,
        tangent_deg: float = 3.0,
        workers: int = 0,
        use_cache: bool = True,
        report_path: str | None = None,
        max_rows: int = 80,
        background: bool = False,
        timeout: float = 3600,
    ) -> list[TextContent]:
        """Manufacturability check: hole edge distance and broken-out holes, per part, headless and parallel.

        A hole is a set of concave cylindrical faces sharing axis and radius. BROKEN: a partial hole
        meeting a neighbour at a sharp edge (breaks through an edge/pocket; tangent slot ends and
        fillets are ignored). FAIL: wall to the nearest side-bounding face < fail_wall_mm. WARN: wall <
        max(warn_wall_mm, warn_ratio * d). Results are cached per part geometry + thresholds, so after
        an edit only changed parts are re-checked.

        Args:
            source: Same forms as interference_check (use the shapes with real holes, e.g. provider
                args {"which": "vis"}).
            pose: Pose to evaluate (geometry is checked in each part's own frame).
            only: Regex on part names.
            fail_wall_mm / warn_wall_mm / warn_ratio: Thresholds.
            min_diameter / max_diameter: Hole size range checked (mm).
            tangent_deg: Edge angle below which a partial hole counts as blending (slot end, fillet).
            background: Return a job id at once; poll job_status.
        Returns: status pass/warn/fail, counts, rows [{status, part, d, at, wall_mm|rim_deg}], timings.
        """
        from .holes import hole_edge_check as run

        def work(job: Any) -> Any:
            return run(source, pose, only, fail_wall_mm, warn_wall_mm, warn_ratio, min_diameter, max_diameter,
                       tangent_deg, workers, use_cache, report_path, max_rows, state.freecadcmd, timeout, job)

        try:
            return _text(run_or_queue("hole_edge_check", work, background))
        except Exception as exc:
            return _err(exc)

    @tool
    def clear_check_cache(which: str = "all") -> list[TextContent]:
        """Delete cached check results: "interference", "holes", "breps" (exported shapes) or "all"."""
        from .parallel import cache_root
        import shutil

        root = cache_root()
        removed = {}
        targets = {"interference": [root / "interference_cache.json"], "holes": [root / "holes_cache.json"],
                   "breps": [root / "breps"]}
        for key, paths in targets.items():
            if which not in ("all", key):
                continue
            for p in paths:
                if p.is_dir():
                    removed[key] = len(list(p.glob("*.brep")))
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    removed[key] = len(json.loads(p.read_text(encoding="utf-8") or "{}"))
                    p.unlink()
        return _text({"removed": removed, "cache_dir": str(root)})

    # ================================================================== TechDraw (GUI)
    td = lambda: _lib("techdraw_lib")  # noqa: E731

    def run_td(fn: str, params: dict[str, Any], timeout: float = 90) -> Any:
        return run_gui(get_connection(), td(), fn, params, timeout, modules=techdraw_modules())

    @tool
    def techdraw_list_templates(filter: str = "") -> list[TextContent]:
        """List FreeCAD's bundled TechDraw templates (relative to Mod/TechDraw/Templates), optionally filtered."""
        try:
            return _text(run_td("list_templates", {"filter": filter}))
        except Exception as exc:
            return _err(exc)

    @tool
    def techdraw_create_page(
        doc_name: str,
        template: str = "ANSIB_Landscape",
        fields: dict[str, str] | None = None,
        page_name: str = "Page",
        new_document: bool = True,
        replace: bool = False,
    ) -> list[TextContent]:
        """Create a TechDraw page from a template and fill its editable fields.

        Args:
            doc_name: Document to create (new_document=True) or an open one to add the page to.
            template: Bundled template name ("ANSIB_Landscape", "ISO/A3_Landscape_blank", ...) or an .svg path.
            fields: EditableTexts values, e.g. {"DrawingTitle1": "HUB", "drawing_number": "ARC-101"}. Tokens
                such as {dia}, {pm}, {deg}, {position} expand to symbols. Unknown names are reported.
            replace: Close and recreate an open document of that name (only with new_document).
        Returns: doc/page names, template path, sheet size and all field names.
        """
        try:
            return _text(run_td("create_page", {
                "doc": doc_name, "template": template, "fields": fields or {}, "page": page_name,
                "new_document": new_document, "replace": replace}))
        except Exception as exc:
            return _err(exc)

    @tool
    def techdraw_set_fields(doc_name: str, fields: dict[str, str], page_name: str = "Page") -> list[TextContent]:
        """Update a page template's editable text fields (title block, tolerance block ...)."""
        try:
            return _text(run_td("set_fields",
                                 {"doc": doc_name, "page": page_name, "fields": fields}))
        except Exception as exc:
            return _err(exc)

    @tool
    def techdraw_add_views(doc_name: str, views: list[dict[str, Any]], page_name: str = "Page",
                           wait_s: float = 30) -> list[TextContent]:
        """Add views to a page with correct placement/scale (set after addView, ScaleType Custom).

        Each view dict: {"type": "part"|"projgroup"|"section"|"detail", "name", "x", "y" (page mm), "scale",
        "source": [object names] or "brep": path (hidden helper Part::Feature is created), "direction",
        "x_direction", "hidden": hidden lines}.
        projgroup: "projections": ["Right", "Top", ...], "projection_type": "Third angle"|"First angle",
        direction/x_direction set the Front anchor. section: "base_view", "normal", "origin", "symbol",
        "section_direction" ("Down"...). detail: "base_view", "anchor" [x, y], "radius", "reference".
        Waits until view geometry exists. Returns each (sub)view's name, position, scale and edge count.
        """
        try:
            return _text(run_td("add_views",
                                 {"doc": doc_name, "page": page_name, "views": views, "wait": wait_s},
                                 timeout=max(90, wait_s + 30)))
        except Exception as exc:
            return _err(exc)

    @tool
    def techdraw_view_geometry(doc_name: str, view_name: str, limit: int = 60) -> list[TextContent]:
        """Compact geometry of a view for picking references: circles [edge, r, cx, cy], arcs, longest lines
        [edge, x0, y0, x1, y1, len], extreme vertices and circle radius counts (model mm from view centre)."""
        try:
            return _text(run_td("view_geometry",
                                 {"doc": doc_name, "view": view_name, "limit": limit}))
        except Exception as exc:
            return _err(exc)

    @tool
    def techdraw_annotate(doc_name: str, items: list[dict[str, Any]], page_name: str = "Page",
                          layout: bool = False) -> list[TextContent]:
        """Add native dimensions and GD&T to a page in one call.

        Item kinds ("view" = view name; origin/at/leader_to/center/p0/p1 in model mm from the view centre;
        dimension "at" and note x/y in page mm):
          datum: {"letter", "origin", "at"} - datum feature symbol (Rectangle balloon, filled triangle).
          fcf: {"cells": ["position", "{dia}0.1{mmc}", "A", "B"]} or {"symbol", "tolerance", "diameter",
               "modifier": "mmc"|"lmc", "datums"}, "at", optional "leader_to" - feature control frame
               (Rectangle balloon, '|' compartments, KinkLength 0, no leader unless leader_to).
          callout: {"text": "6X {dia}3.3 THRU", "origin", "at"} - hole callout (no frame, arrow leader).
          text: {"text", "at"} - leaderless line (e.g. 2nd callout line); balloon: generic balloon.
          dimension: {"refs": [ref, ...], "type": "Distance"|"DistanceX"|"DistanceY"|"Radius"|"Diameter"|
               "Angle", "format": "{dia}%.1f", "at", "basic": true (boxed), "over_tolerance", "under_tolerance"}.
               ref = "Edge3"/"Vertex2" or {"circle": r, "center": [x, y]} | {"vertex": [x, y]} |
               {"line": [[x0, y0], [x1, y1]]} | {"extreme": "xmin|xmax|ymin|ymax"}.
          cosmetic_circle: {"center", "r"}; cosmetic_line: {"p0", "p1"}; centerline: {"refs", "mode"}.
          note: {"lines": [...], "x", "y", "width", "size_pt", "frame"} or {"html": ...}.
        Layout keys (any balloon/dimension item): "label" (layout reads labels), "group": n (items sharing n move
          as one unit; datum -> Datum_<L>_g<n>, leaderless fcf -> FCF_g<n>), "anchors": [[x, y], ...] + "anchor_r"
          (every instance of the hole pattern: the layout re-attaches the leader radially to the best one),
          "font_size" (default 3.5 mm). Dimensions without "format" get the Y14.5 metric format (no trailing zeros).
        layout: afterwards snap datum symbols onto their FCFs and run the overlap-free placement (opens the page);
          the reply then has "conflicts" (labels / leaders / dimension_lines) as techdraw_lint reports them.
        Text tokens: {dia} {mmc} {lmc} {cbore} {csk} {depth} {deg} {pm} {sq} and every GD&T symbol name
        ({position}, {flatness}, {perpendicularity}, {profile_surface}, ...).
        Returns created object names/labels, per-item problems (e.g. unresolved references) and "text_lint"
        (Y14.5 4.3.1 metric value issues such as trailing zeros).
        """
        try:
            return _text(run_td("annotate",
                                 {"doc": doc_name, "page": page_name, "items": items, "layout": layout},
                                 timeout=300 if layout else 120))
        except Exception as exc:
            return _err(exc)

    def _raster(res: dict[str, Any], dpi: int) -> list[str]:
        from .pdf_raster import rasterize

        if dpi and res.get("pdf"):
            try:
                res["png"] = rasterize(res["pdf"], dpi)
                return res["png"]
            except Exception as exc:
                res["png_error"] = str(exc)
        return []

    @tool
    def techdraw_export(
        pages: list[dict[str, Any]],
        out_dir: str,
        pdf: bool = True,
        svg: bool = False,
        png_dpi: int = 0,
        save_fcstd: bool = False,
        close_view: bool = False,
        close_document: bool = False,
        return_image: bool = False,
        background: bool = False,
    ) -> list[TextContent | ImageContent]:
        """Export TechDraw pages to PDF (and SVG/PNG) - one GUI call per page, so batches never time out.

        Each page is opened in the GUI first (TechDraw only draws the template on an open page).
        Args:
            pages: [{"doc": name, "page": "Page", "stem": file stem (default doc name)}, ...].
            png_dpi: >0 rasterises each PDF with PyMuPDF for review (e.g. 110).
            save_fcstd: Also save each drawing document as <stem>.FCStd in out_dir.
            close_view / close_document: Tidy up after each export.
            return_image: Attach the first PNG to the reply (needs png_dpi).
            background: Run as a job (recommended for many pages).
        """

        def work(job: Any) -> Any:
            out = []
            for i, pg in enumerate(pages):
                if job.cancel.is_set():
                    break
                job.progress(i, len(pages), pg.get("doc", ""))
                t = time.time()
                try:
                    res = run_td("export_page", {
                        "doc": pg["doc"], "page": pg.get("page", "Page"), "stem": pg.get("stem"), "out_dir": out_dir,
                        "pdf": pdf or bool(png_dpi), "svg": svg, "save_fcstd": save_fcstd, "close_view": close_view,
                        "close_document": close_document}, timeout=120)
                    _raster(res, png_dpi)
                    res["s"] = round(time.time() - t, 1)
                except Exception as exc:
                    res = {"doc": pg.get("doc"), "error": str(exc)[:500]}
                out.append(res)
            job.progress(len(pages), len(pages))
            return {"exported": sum(1 for r in out if "error" not in r), "failed": sum(1 for r in out if "error" in r),
                    "pages": out}

        try:
            res = run_or_queue("techdraw_export", work, background)
            reply: list[TextContent | ImageContent] = list(_text(res))
            if return_image and not background:
                pngs = [p for r in res["pages"] for p in r.get("png", [])]
                if pngs:
                    reply.append(_image(pngs[0]))
            return reply
        except Exception as exc:
            return _err(exc)

    @tool
    def techdraw_part_drawing(
        drawings: list[dict[str, Any]],
        out_dir: str,
        png_dpi: int = 0,
        return_image: bool = False,
        background: bool = False,
    ) -> list[TextContent | ImageContent]:
        """One-call reduced-dimension part drawings (batch): sheet, views, dims, GD&T, notes, layout, PDF.

        For each spec a new drawing document is built in the GUI and exported (one GUI call per drawing), with
        the ASME Y14.5-2018 rules and overlap-free layout proven on the light_weight_arm drawings:
        - orientation: Z = the axis most holes run along (else the thinnest extent), X = longest;
        - third-angle Front + Right [+ Top unless plate-like] and an isometric view, largest standard scale that
          keeps every view inside the frame (re-checked on the rendered page);
        - overall dimensions only on the side where extension lines stay short, otherwise an envelope REF note;
          round outlines get one diameter; datum A on the side view (or the diameter's axis for turned parts);
        - holes: full circles from merged split arcs (hidden-only circles ignored), counterbored columns open at
          both ends are THRU, grouped Y14.5 callouts with the position FCF directly below (Fig 6-30) and the
          datum symbol attached to the FCF; DRF A|B(|C) from the pattern (R.drf); floating fasteners T = H - F at
          MMC, tapped holes (fixed fastener) T = (H - F)/2 with a projected zone (App. B-4, 10.4.1);
          machined bearing seats / dowel holes get ISO 286 H7, bearing journals k6;
        - leaders attach radially to whichever pattern instance gives the shortest uncrossed leader, then the
          layout engine snaps datums and moves labels until no label/leader/dimension-line conflict remains;
        - a warning for every hole group that gets no callout (hidden in the front view or over max_callouts).

        Project specifics go in "config" (dict) or "config_path" (JSON file), not in the tool:
          "processes": {name: {title2, title3, pos, prof, tol {template fields}, notes [...], mmc, fits
            ("h7" | "ream" | null), tapped {radius: thread}, projected, journals, two_view, hidden, shaft,
            flatness, insert_d, density, base (built-in profile to extend)}} - built-ins: machined, plate,
            printed, shaft, shim; note tokens @THREADS @MASK @INSERT @SHAFT @SHIMMAT @THK {step} {dxf};
          "extra_notes": {part-name prefix: note}; "plain_names": regex (spacers/sleeves: no fits, no threads);
          "hole_overrides": [{"names": regex, "diameter", "tol", "text" ({t}, {tap_depth})}];
          "projected_heights", "bearings", "template", "note_hex" (true).
        Spec keys: "doc" (drawing document name, required), "brep" (path) or "source_doc" + "object",
        "stem" (file stem), "part_name" (matched by config name rules), "instances" (names sharing this sheet:
        QTY / mirror-image note), "qty_per" ("PER ROBOT"), "template" (default ANSIB_Landscape), "fields",
        "process" (a built-in or config process: default tolerances, MMC/fits/hidden-line/shaft rules),
        "density_g_cm3" (fills Weight + mass note), "scale" (else auto), "orient" (true), "projections",
        "hidden", "iso", "overall_dims", "hole_callouts", "datums", "max_callouts" (7),
        "position_tolerance" (floor / RFS value, default per process), "profile_tolerance" (default per
        process; false = none), "tapped" ({"1.25": "M3x0.5-6H"} hole radius -> thread), "fits" (bool),
        "bearings" ({"6700ZZ": [10, 15]} extra d x D sizes), "projected_heights" ({"M3": 6} or
        {"<part>|M3": 6} mm; default 6 with a warning), "model_ref" (model file cited in note 1),
        "model_note" (true), "notes" (list of strings), "extra_items" (techdraw_annotate items),
        "layout" (true), "layout_budget_s" (240: no new layout attempt after it), "frame" / "keepout" (layout
        boxes, page mm, Y up; default scaled from ANSI B), "dxf_ref",
        "save_fcstd" (true), "close" (true).
        Reply per drawing: "conflicts" (should be count 0), "warnings", "text_lint", "problems", pdf/png paths.
        Check the PNG visually as well (png_dpi=110); run techdraw_lint on the saved FCStd after edits.
        Tip: export part BREPs headless first so the GUI does no modelling.
        """

        def work(job: Any) -> Any:
            out = []
            for i, spec in enumerate(drawings):
                if job.cancel.is_set():
                    break
                job.progress(i, len(drawings), spec.get("doc", ""))
                t = time.time()
                try:
                    res = run_td("part_drawing", dict(spec, out_dir=out_dir), timeout=600)
                    _raster(res, png_dpi)
                    res["s"] = round(time.time() - t, 1)
                except Exception as exc:
                    res = {"doc": spec.get("doc"), "error": str(exc)[:800]}
                out.append(res)
            job.progress(len(drawings), len(drawings))
            return {"made": sum(1 for r in out if "error" not in r), "failed": sum(1 for r in out if "error" in r),
                    "drawings": out}

        try:
            res = run_or_queue("techdraw_part_drawing", work, background)
            reply: list[TextContent | ImageContent] = list(_text(res))
            if return_image and not background:
                pngs = [p for r in res["drawings"] for p in r.get("png", [])]
                if pngs:
                    reply.append(_image(pngs[0]))
            return reply
        except Exception as exc:
            return _err(exc)

    @tool
    def techdraw_lint(doc_name: str = "", path: str = "", page_name: str = "", frame: list[float] | None = None,
                      keepout: list[list[float]] | None = None) -> list[TextContent]:
        """Read-only drawing check on an open page (doc_name) or a saved .FCStd (path; opened, then closed).

        Measures the rendered TechDraw scene and runs the layout engine's collision rules: labels on labels,
        views or the title/tolerance block or outside the frame; leaders through labels or crossing other
        leaders; leaders crossing dimension/extension lines. Also lists undefined or self-referenced datums,
        Y14.5 4.3.1 text issues (trailing/missing zeros) and views outside the frame. Nothing is moved or saved.
        frame / keepout: [x0, y0, x1, y1] page-mm boxes (Y up) if the template is not ANSI B-like.
        A clean lint is necessary, not sufficient: also look at the exported PNG.
        """
        if not doc_name and not path:
            return _err(ValueError("pass doc_name (open document) or path (.FCStd)"))
        try:
            params: dict[str, Any] = {"doc": doc_name, "path": path, "page": page_name}
            if frame:
                params["frame"] = frame
            if keepout is not None:
                params["keepout"] = keepout
            return _text(run_td("lint", params, timeout=180))
        except Exception as exc:
            return _err(exc)

    # ================================================================== frames / video
    @tool
    def render_frames(
        doc_name: str,
        out_dir: str,
        simulation: str | None = None,
        assembly: str | None = None,
        frame_start: int = 0,
        frame_end: int | None = None,
        frame_step: int = 1,
        n_frames: int | None = None,
        camera: dict[str, Any] | None = None,
        width: int = 1280,
        height: int = 720,
        background_color: str = "Current",
        pattern: str = "frame_%05d.png",
        chunk_size: int = 20,
        encode_fps: int = 0,
        video_path: str | None = None,
        background: bool = True,
    ) -> list[TextContent]:
        """Capture PNG frames of an assembly simulation and/or a camera path, in GUI chunks; optionally MP4.

        Frames come from Assembly simulation playback (generateSimulation once, updateForFrame per frame,
        view.saveImage) when ``simulation`` is given; otherwise ``n_frames`` frames of a pure camera move.
        Work is split into chunks of ``chunk_size`` frames (one GUI call each), so the 90 s GUI budget is
        never hit; runs as a background job by default (poll job_status).

        Args:
            simulation: Simulation object name/label inside an Assembly.
            frame_start / frame_end / frame_step: Simulation frame range (end default = last frame).
            n_frames: Frame count for camera-only sequences.
            camera: {"keys": [{"t": 0..1, "position": [x,y,z], "target": [x,y,z], "up": [0,0,1]}, ...],
                "ease": true} or {"orbit": {"center": [..], "radius", "height", "start_deg", "end_deg"}}.
                Omit to keep the current view.
            background_color: saveImage background ("Current", "White", "Transparent", "#RRGGBB").
            encode_fps: >0 encodes the frames to MP4 afterwards (see encode_video).
            video_path: MP4 path (default <out_dir>/../<dirname>.mp4).
        """
        lib = _lib("render_lib")

        def work(job: Any) -> Any:
            t0 = time.time()
            conn = get_connection()
            if simulation:
                info = run_gui(conn, lib, "sim_frame_count", {"doc": doc_name, "simulation": simulation}, timeout=300)
                last = info["frames"] - 1 if frame_end is None else min(frame_end, info["frames"] - 1)
                frames = list(range(frame_start, last + 1, max(1, frame_step)))
            else:
                if not n_frames:
                    raise ValueError("give simulation or n_frames")
                frames = list(range(n_frames))
            total = len(frames)
            done = 0
            for c in range(0, total, max(1, chunk_size)):
                if job.cancel.is_set():
                    break
                chunk = frames[c:c + chunk_size]
                run_gui(conn, lib, "render_chunk", {
                    "doc": doc_name, "simulation": simulation, "assembly": assembly, "frames": chunk,
                    "index0": c, "total": total, "out_dir": out_dir, "pattern": pattern, "width": width,
                    "height": height, "background": background_color, "camera": camera}, timeout=300)
                done += len(chunk)
                job.progress(done, total, "frames")
            res: dict[str, Any] = {"frames": done, "out_dir": out_dir, "pattern": pattern,
                                   "render_s": round(time.time() - t0, 1),
                                   "s_per_frame": round((time.time() - t0) / max(1, done), 2)}
            if encode_fps and done:
                from .video import encode_video as enc

                vp = video_path or str(Path(out_dir).parent / (Path(out_dir).name + ".mp4"))
                res["video"] = enc(out_dir, vp, encode_fps, pattern)
            return res

        try:
            return _text(run_or_queue("render_frames", work, background))
        except Exception as exc:
            return _err(exc)

    @tool
    def encode_video(frames_dir: str, out_path: str, fps: int = 30, pattern: str = "frame_%05d.png",
                     crf: int = 18, encoder: str = "auto") -> list[TextContent]:
        """Encode numbered PNG frames to H.264 MP4 (no FreeCAD needed).

        Encoder: FREECAD_MCP_FFMPEG, imageio-ffmpeg's bundled ffmpeg, ffmpeg on PATH, else Blender's bundled
        FFmpeg via its Video Sequencer (FREECAD_MCP_BLENDER / blender on PATH). encoder: auto|ffmpeg|blender.
        """
        from .video import encode_video as enc

        try:
            return _text(enc(frames_dir, out_path, fps, pattern, crf, encoder))
        except Exception as exc:
            return _err(exc)

    # ================================================================== assembly helpers
    asm = lambda: _lib("assembly_lib")  # noqa: E731

    def _asm_run(fn: str, params: dict[str, Any], path: str | None, timeout: float) -> Any:
        if path:
            return run_headless_lib(state.freecadcmd, asm(), fn, dict(params, path=path), timeout)
        return run_gui(get_connection(), asm(), fn, params, timeout=timeout)

    @tool
    def assembly_attach_view_providers(doc_name: str) -> list[TextContent]:
        """Re-attach Python view providers (joints, grounded joints, simulations, motions, ...) in an assembly
        built headless (their ViewObject.Proxy is None, which breaks dragging, solving UI and playback).
        Uses ViewProvider<ProxyClass> from each feature's own module. GUI document only."""
        try:
            return _text(run_gui(get_connection(), asm(), "attach_view_providers", {"doc": doc_name}))
        except Exception as exc:
            return _err(exc)

    @tool
    def assembly_solve_check(doc_name: str = "", path: str | None = None, assembly: str | None = None,
                             restore: bool = True, tol_mm: float = 0.01, tol_deg: float = 0.01,
                             top: int = 10, timeout: float = 300) -> list[TextContent]:
        """Solve an Assembly and report drift: how far each part moved from its stored placement.

        Large drift means the saved placements did not satisfy the joints (or the solver flipped a
        configuration). ``restore`` puts the original placements back afterwards. With ``path`` the saved
        file is checked headless (never touches the GUI or saves); otherwise the open GUI document."""
        try:
            return _text(_asm_run("solve_check", {"doc": doc_name, "assembly": assembly, "restore": restore,
                                                  "tol_mm": tol_mm, "tol_deg": tol_deg, "top": top}, path, timeout))
        except Exception as exc:
            return _err(exc)

    @tool
    def assembly_simulation_check(doc_name: str = "", path: str | None = None, simulation: str | None = None,
                                  assembly: str | None = None, every: int = 1, jump_factor: float = 20.0,
                                  jump_floor_mm: float = 1.0, timeout: float = 600) -> list[TextContent]:
        """Generate an Assembly simulation and validate it: frame count, per-frame part motion and solver jumps
        (frames where a part moves > max(jump_floor_mm, jump_factor x median step)). Placements are restored.
        With ``path`` it runs headless on the saved file; otherwise in the open GUI document."""
        try:
            return _text(_asm_run("simulation_check", {"doc": doc_name, "simulation": simulation, "assembly": assembly,
                                                       "every": every, "jump_factor": jump_factor,
                                                       "jump_floor_mm": jump_floor_mm}, path, timeout))
        except Exception as exc:
            return _err(exc)
