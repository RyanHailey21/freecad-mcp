# Portable CAD workflow integration

Project facts, acceptance thresholds and check scheduling belong in project files,
not this server. Use https://github.com/RyanHailey21/mechanical-workflow-template
for a small project-local starter. No companion MCP server is needed.

Install uv and the Python version required by pyproject.toml. Clone this fork,
check out the exact tested commit, then run `uv sync --frozen`. Configure your
agent to launch `uv --project /absolute/checkout run --frozen freecad-mcp`.
Set the server working directory to the project workspace; --project selects the
installed environment without changing that working directory. This matters for
build123d allowed file roots. Call `cad_capabilities` first. It reports installed features and discovery only;
it is not a successful geometry check. Existing tools and result fields remain
compatible. Restart an already-running host after updating its checkout.

Saved-file operations now add an `evidence` object with schema `cad-evidence/1`:
producer, operation, status, reason, scope, versions, implementation SHA-256,
inputs and artifacts. Artifact paths are relative to the saved report directory,
so copy the whole job directory to move it. Original legacy absolute paths are
retained as audit context. Each input/output has an explicit ID and SHA-256.
Statuses are pass, fail, unknown or unsupported. Timeouts, crashes and incomplete
reports are unknown, never pass. A failed requirement is fail. Retain the raw
reason and partial results. Do not retry a whole session automatically.

The saved-geometry evidence starts at the STEP input. It does not prove that a
part came from a particular source script. Save the execute_file response and
source hash, then export in that same fresh session without intervening edits.
Project orchestration records that chain. Import names/labels must be mapped
explicitly to project instance IDs, never guessed from list order. Compare shape
validity, solid count, volume and bounds; declare transform conventions before
comparing placements. None of these checks proves continuous safe motion.

## FreeCAD installation

Install FreeCAD separately; uv installs the MCP host, not the CAD application.
Set FREECAD_MCP_EXECUTABLE to the literal executable path (no embedded quotes),
or use the existing --freecadcmd argument for a command with arguments. Discovery
checks that environment variable first, then PATH, unambiguous standard Windows/
macOS locations, then Flatpak. Multiple installations require an explicit choice.
A bad explicit environment path does not silently fall back to another version.
Headless diagnose_saved_assembly needs no GUI RPC connection. It imports requested
modules and reports availability; it does not activate a GUI workbench or solve
an assembly. Required modules default to Part and Assembly; request Part alone
for plain geometry import. Missing executables produce durable unsupported reports.

Headless child processes receive closed stdin (DEVNULL). They must not inherit
the MCP protocol pipe: that can stall FreeCAD startup under a stdio host.
