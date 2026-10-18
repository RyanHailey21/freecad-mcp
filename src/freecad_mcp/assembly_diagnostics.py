"""Extension for the existing FreeCAD MCP package; no separate server."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import uuid


def diagnose(path, output_dir, command=None, timeout=60, required_modules=None):
    from .headless import run_headless, detect_freecadcmd
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError('timeout must be in (0,60]')
    if command is None:
        command = detect_freecadcmd()
    source = Path(path).resolve(strict=True)
    if source.suffix.lower() not in ('.fcstd', '.step', '.stp'):
        raise ValueError('Supply a saved FCStd or STEP file')
    root = Path(output_dir).resolve() / ('diagnostic-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    saved = root / ('input' + source.suffix)
    shutil.copyfile(source, saved)
    def sha(p):
        with p.open('rb') as f:
            return hashlib.file_digest(f, 'sha256').hexdigest()
    original_hash = sha(source)
    if sha(saved) != original_hash:
        raise ValueError('Source changed during snapshot')
    result_path = root / 'report.json'
    # All caller strings are Python literals, never executable interpolation.
    code = '''import FreeCAD as App
import Part, Import, importlib, json, traceback
report = {"status":"pass", "version":App.Version(), "modules":{}, "components":[],
          "solver_status":"not_run", "scope":"Headless import diagnostics; GUI workbench activation and assembly solving are not verified"}
doc = None
try:
    for name in MODULES:
        try:
            importlib.import_module(name)
            report["modules"][name] = {"available":True}
        except Exception as exc:
            report["modules"][name] = {"available":False,"error":str(exc)}
            report["status"] = "fail"
    if INPUT.lower().endswith('.fcstd'):
        doc = App.openDocument(INPUT)
    else:
        doc = App.newDocument('ImportDiagnostic')
        Import.insert(INPUT, doc.Name)
    doc.recompute()
    for obj in doc.Objects:
        if not hasattr(obj, 'Shape') or obj.Shape.isNull():
            continue
        shape = obj.Shape
        b = shape.BoundBox
        valid = shape.isValid()
        report['components'].append({'id':obj.Name, 'label':obj.Label,
            'type':obj.TypeId, 'valid':valid, 'solids':len(shape.Solids),
            'volume_mm3':shape.Volume, 'placement':list(obj.Placement.toMatrix().A),
            'bounds':[b.XMin,b.YMin,b.ZMin,b.XMax,b.YMax,b.ZMax]})
        if not valid:
            report['status'] = 'fail'
    if not report['components']:
        report.update(status='fail', error='No shaped components imported')
except Exception as exc:
    report.update(status='error',error=str(exc),traceback=traceback.format_exc())
finally:
    if doc:
        App.closeDocument(doc.Name)
    with open(OUTPUT,'w',encoding='utf-8') as f:
        json.dump(report,f,indent=2)
'''
    prefix = f'INPUT={str(saved)!r}\nOUTPUT={str(result_path)!r}\nMODULES={required_modules or ["Part", "Assembly"]!r}\n'
    process = run_headless(prefix + code, timeout, command) if command else {'success': False, 'unsupported': True, 'error': 'Configure FREECAD_MCP_EXECUTABLE or --freecadcmd'}
    report = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {}
    if not process.get('success') or not report:
        report.update(status='unsupported' if process.get('unsupported') else 'timeout' if process.get('timed_out') else 'error')
    report.update(process=process, input_path=str(source), input_sha256=original_hash,
                  report_path=str(result_path))
    if sha(source) != original_hash:
        report.update(status='error', error='Source changed during diagnostic')
    from .evidence import envelope
    report['evidence'] = envelope(report, 'freecad-mcp', 'diagnose_saved_assembly', root,
        [{'id': 'input', 'path': str(saved), 'sha256': original_hash}], [], __file__)
    report['evidence']['versions']['FreeCAD'] = report.get('version', 'unknown')
    temp = root / 'report.tmp'
    temp.write_text(json.dumps(report, indent=2), encoding='utf-8')
    os.replace(temp, result_path)
    return report


def register(mcp, state):
    @mcp.tool()
    def cad_capabilities() -> dict:
        """Discover headless availability without launching FreeCAD or touching the GUI."""
        from .headless import detect_freecadcmd
        from .evidence import versions
        command = state.freecadcmd or detect_freecadcmd()
        return {'schema': 'cad-capabilities/1', 'producer': 'freecad-mcp',
                'versions': versions('freecad-mcp'), 'evidence_schema': 'cad-evidence/1',
                'operations': {'diagnose_saved_assembly': 'available' if command else 'unsupported'},
                'command': command, 'limits': {'isolated_timeout_seconds': 60},
                'scope': 'Executable discovery only; module imports are checked by diagnostics'}

    @mcp.tool()
    def diagnose_saved_assembly(path: str, output_dir: str, timeout: float = 60,
                                required_modules: list[str] | None = None) -> dict:
        """Diagnose a saved STEP/FCStd in headless FreeCAD without touching GUI documents.

        Reports required-module imports (default Part, Assembly), component IDs,
        placements, validity, bounds, volumes and process failures. Does not run
        the assembly solver or claim GUI workbench compatibility. Writes a durable
        report and input snapshot. Use a representative subassembly first.
        """
        return diagnose(path, output_dir, state.freecadcmd, timeout, required_modules)
