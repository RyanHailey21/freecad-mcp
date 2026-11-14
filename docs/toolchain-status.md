# Process readiness

Call `toolchain_status(expected_revision="...", probe_freecad=false)` to compare the running
startup revision/source hash with the files currently installed. A changed installation is reported
as needing restart; editing client configuration alone is not proof of a running update.

The optional FreeCAD addon ping uses a three-second socket timeout. No document is opened,
closed, saved or reloaded, and no geometry check is run. Unknown Git revisions remain null.
Use this once at project startup or after deployment, not as a repeated verification gate.
