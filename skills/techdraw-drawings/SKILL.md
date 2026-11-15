---
name: techdraw-drawings
description: Make ASME Y14.5 manufacturing drawings of FreeCAD parts with the freecad-mcp TechDraw tools (techdraw_part_drawing, techdraw_annotate, techdraw_lint, techdraw_verify, techdraw_export). Use when asked for part drawings, GD&T, hole callouts, datum frames, a drawing package for fabrication, or to fix overlapping or crossing leaders and labels on a TechDraw sheet.
---

# TechDraw manufacturing drawings (freecad-mcp)

Deterministic work (geometry queries, ASME rules, placement, collision checks) lives in the MCP
tools. This skill is the process and the judgement around them.

## Hard rules

- **Native TechDraw objects only.** Views are `DrawProjGroup` / `DrawViewPart` / section / detail;
  dimensions are `DrawViewDimension`; datum symbols, FCFs and hole callouts are `DrawViewBalloon`s;
  notes are `DrawRichAnno`; sheets are bundled (or project) SVG *templates* with editable fields.
  **Never hand-draw SVG**, never post-edit an exported SVG/PDF, never fake a symbol with lines.
- **Prebuilt first.** Use the bundled templates (`techdraw_list_templates`) and the tools' rules
  before writing custom drawing code.
- **Done = four checks, the last one independent.** A sheet is done only when `conflicts.count == 0`
  (or `techdraw_lint` is `clean`), `techdraw_verify` has no errors, you have looked at the rasterised
  sheet yourself, **and an independent reviewer** (a fresh subagent that sees the sheet and the model,
  not your reasoning) found nothing. On the arm drawings the independent reviews found what every
  automated check missed: wrong FAR SIDE, wrong hole counts, impossible processes.
- **Project specifics belong in `config`,** not in the tool or in ad hoc code: process table, extra
  notes, name rules (spacers, standoffs), projected heights, bearings.

## Which tool when

| Situation | Tool |
| --- | --- |
| Standard part drawing (one part or a batch) | `techdraw_part_drawing` (does views, dims, GD&T, notes, layout, PDF/PNG) |
| Add or change a few items on an existing sheet | `techdraw_view_geometry` (pick references) → `techdraw_annotate` with `layout=true` |
| Custom sheet (sections, details, assembly views) | `techdraw_create_page` → `techdraw_add_views` → `techdraw_annotate(layout=true)` |
| Title / tolerance block text | `techdraw_set_fields` (or `fields` in the spec) |
| Re-export after edits, many pages | `techdraw_export` (`background=true` for batches, `png_dpi=110`) |
| Layout check of any sheet (open doc or saved `.FCStd`) | `techdraw_lint` (read-only, GUI) |
| Many sheets in parallel (private GUIs, shared FreeCAD untouched) | `gui_batch` (per-item time budget, skips unchanged sheets) |
| Callouts vs the model (saved `.FCStd`) | `techdraw_verify` (read-only, headless: safe while the GUI is busy) |

Export part shapes headless first (BREP or STEP path in `brep`) so the GUI does no modelling.

## Workflow

1. **Spec the part.** Write the project `config` once (or a JSON file for `config_path`):
   `processes` (extend a built-in: `machined`, `plate`, `printed`, `shaft`, `shim` - title lines,
   tolerance-block fields, position/profile values, notes with `@THREADS @MASK @INSERT @SHAFT
   @SHIMMAT` tokens, `tapped` radius → thread map, fits `h7`/`ream`), `extra_notes` by part-name
   prefix, `plain_names` (spacers/sleeves: no fits, no threads), `hole_overrides` (e.g. standoffs
   tapped from both ends), `projected_heights` measured from the assembly (thickest mating stack on
   the screw-head side; the 6 mm default raises a warning), `bearings`. Per sheet: `process`,
   `part_name`, `instances` (sheet shared by several parts / mirror images), `fields`, `notes`.
2. **Run it** with `png_dpi=110`, `return_image=true` for a single part (`background=true` and
   `job_status` for batches).
3. **Read the reply, not just the status:**
   - `conflicts.count` must be 0. `labels` = label on label / view / title block / outside frame,
     `leaders` = leader through a label or crossing another leader, `dimension_lines` = leader
     across a dimension or extension line.
   - `warnings`: every hole group that got **no callout** (hidden in the front view, or beyond
     `max_callouts`) is listed and goes into a "HOLES WITHOUT CALLOUT" note. Decide whether that is
     acceptable or the part needs another view / a section / an `extra_items` callout.
   - `text_lint`: Y14.5 metric value issues (trailing zeros, missing leading zero).
   - `envelope_note: true` means overall dimensions were replaced by an envelope REF note because
     their extension lines would have run across most of the part. That is intentional.
4. **Look at the sheet.** Open the PNG. Then zoom into every callout block, every leader and the
   area around the views (rasterise the PDF at 200–300 dpi with a clip). Lint cannot see
   everything; look for: leaders converging on the same point, a leader landing on an ambiguous
   edge (profile leader next to a hole), text touching a frame, an unreadably small iso view,
   callouts far from their feature, anything that reads wrong to a machinist.
5. **Verify against the model:** `techdraw_verify(path=...FCStd)`. Every error is a wrong sheet
   (count, THRU/depth, FAR SIDE, counterbore/countersink, angle, uncalled holes, undefined datums).
   Fix the cause (config, model, generator), not the text.
6. **Fix with native objects**, then re-run the layout and re-check:
   `techdraw_annotate(items, layout=true)` → `techdraw_export` → `techdraw_lint(path=...FCStd)` →
   `techdraw_verify` → look at the PNG again. Do not nudge coordinates by hand without re-linting.
7. **Independent review (required, last).** Hand each sheet's PNG/PDF and the model (STEP) to a
   fresh reviewer subagent with a narrow brief: "act as the machinist and the inspector: list every
   DEFECT (wrong or missing information, ambiguity, a process the shop cannot do) with the callout
   it concerns; probe the STEP to confirm". Do not give it your conclusions. Fix, regenerate, and
   review again until a round finds nothing new; re-run lint and verify after every fix (fixes
   regress other sheets: regenerate the whole batch, not only the sheet you touched).
8. **Batch:** after all sheets are made, lint and verify each saved `.FCStd`, inspect each PNG and
   review them; report any sheet you could not get clean instead of shipping it. The layout has a
   time budget (`layout_budget_s`, 240 s); a sheet that hits it is reported with its conflicts.

## Annotating so the layout engine can help

`techdraw_annotate` items take layout keys the engine reads from object labels:

- `group: n` — items that belong together (callout + FCF + datum symbol) move as one unit.
  A datum gets label `Datum_<L>_g<n>`, a leaderless FCF `FCF_g<n>`; `snap_datums` puts the datum
  triangle on that FCF's left edge.
- `anchors: [[x, y], ...]`, `anchor_r: r` — every instance of a hole pattern; the engine attaches
  the leader radially (Y14.5 §4.4.4.2) to the instance giving the shortest uncrossed leader.
- Put the FCF directly **below** its hole callout (Y14.5 Fig 6-30).
- Leave `format` off dimensions to get the metric format without trailing zeros.

The engine is tuned on the ANSI B landscape sheet and scales to other sizes; for a template with a
different title block pass `frame` / `keepout` boxes (page mm, Y up) to the tools.

## Standards status — say this accurately

Verified against the licensed ASME Y14.5-2018 copy:

- §4.3.1 — millimetre values: no trailing zeros, leading zero below 1.
- §4.4.4 — leaders: inclined line plus a short horizontal shoulder at the text mid-height.
- §4.4.4.2 — a leader to a circle or arc is radial.
- Fig 6-30 — feature control frame placed below the hole callout.
- App. B-4 — floating fastener T = H − F; fixed fastener T = (H − F)/2 with a projected zone.
- §10.4.1 — projected tolerance zone (Ⓟ plus height) on tapped holes.

Also applied, taken from the light_weight_arm generator's reviewed rules and not re-checked in
this repository: §5.2.1 (fit shown as the tolerance symbol, `⌀4 H7`), §4.5.12 (countersink diameter
and included angle in the callout), §11.3.1.4 / §11.3.1.5(b) (profile ALL OVER, its exception for
directly toleranced features).

**Not verified** (no copy checked): ASME Y14.41, Y14.100, Y14.2, and ISO 286 (the H7/k6 values,
3–50 mm, were cross-checked only against the ISO 286-1 formulas and published examples). Do not
cite these on drawings or claim compliance with them. Drawings cite Y14.5 only. Quote the
standard minimally and never copy it into a repository.

## Gotchas

- TechDraw builds its scene (and draws the template) only while the page is open; the tools open
  it. Review the PDF/PNG export, not the SVG (other rasterisers shift balloon text).
- The GUI-side libraries (`guiside/*.py`) are re-read on every call; a change to a tool's
  signature needs an MCP server restart, which kills in-flight FreeCAD jobs. Coordinate first.
- Datum letters: every letter used in an FCF must be defined on the sheet and no FCF may
  reference its own datum feature; `techdraw_lint` reports both.
- Never run TechDraw from `execute_code_async`: it is refused outside `commit()` because it can
  wedge FreeCAD. The GUI runs one job at a time and is often shared: ask before long GUI runs;
  `techdraw_verify` and the feature tests are headless.
- Feature recognition (`fcworkers/fcmcp_features.py`) is pure Part and fixture-tested
  (`tests/test_features.py`): add a fixture before changing it.
