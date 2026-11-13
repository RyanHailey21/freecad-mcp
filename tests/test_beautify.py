import json

import pytest

from freecad_mcp import beautify as bt


def test_generated_script_is_valid_python():
    code = bt.build_beautify_code("Doc")
    compile(code, "<beautify>", "exec")
    assert "PARAMS = json.loads(" in code
    assert bt.BEGIN in code and bt.END in code


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"doc_name": ""}, "doc_name"),
        ({"doc_name": "D", "fillet_radius": -1}, "fillet_radius"),
        ({"doc_name": "D", "fillet_radius": 50}, "fillet_radius"),
        ({"doc_name": "D", "fillet_radius": True}, "fillet_radius"),
        ({"doc_name": "D", "palette": "neon"}, "palette"),
        ({"doc_name": "D", "min_edge_length": 0}, "min_edge_length"),
        ({"doc_name": "D", "max_volume_change_pct": 0}, "max_volume_change_pct"),
        ({"doc_name": "D", "max_volume_change_pct": 99}, "max_volume_change_pct"),
        ({"doc_name": "D", "protect": [""]}, "protect"),
        ({"doc_name": "D", "protect": ["x"] * 51}, "protect"),
    ],
)
def test_invalid_arguments_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        bt.build_beautify_code(**kwargs)


def test_document_name_is_embedded_safely():
    nasty = "A\"'); import os; os.system('x') #"
    code = bt.build_beautify_code(nasty)
    compile(code, "<beautify>", "exec")
    params_line = code.splitlines()[1]
    ns = {"json": json}
    exec(params_line, ns)
    assert ns["PARAMS"]["doc_name"] == nasty  # carried as data, never as code


def test_reference_objects_are_protected_by_default():
    code = bt.build_beautify_code("D")
    ns = {"json": json}
    exec(code.splitlines()[1], ns)
    assert ns["PARAMS"]["protect"] == ["Ref_*"]


def test_parse_report_round_trip():
    payload = {"summary": {"objects": 2}}
    message = f"noise\n{bt.BEGIN}{json.dumps(payload)}{bt.END}\nmore"
    assert bt.parse_report(message) == payload
    assert bt.parse_report("nothing here") is None


class FakeConnection:
    def __init__(self, result):
        self.result = result
        self.code = None
        self.timeout = None
        self.screenshots = 0

    def execute_code(self, code, timeout=None):
        self.code, self.timeout = code, timeout
        return self.result

    def get_active_screenshot(self, view_name="Isometric"):
        self.screenshots += 1
        return "aGVsbG8="


def _ok(payload):
    return {"success": True, "message": f"out {bt.BEGIN}{json.dumps(payload)}{bt.END}"}


def test_operation_returns_report_and_screenshot():
    conn = FakeConnection(_ok({"summary": {"filleted": 3}}))
    out = bt.beautify_operation(conn, False, "Doc", fillet_radius=1.5, palette="carbon")
    assert json.loads(out[0].text)["summary"]["filleted"] == 3
    assert out[-1].type == "image" and conn.screenshots == 1
    assert conn.timeout == 600 and '"palette": "carbon"' in conn.code


def test_dry_run_and_text_only_skip_the_screenshot():
    conn = FakeConnection(_ok({"summary": {}}))
    assert len(bt.beautify_operation(conn, False, "Doc", dry_run=True)) == 1
    assert len(bt.beautify_operation(conn, True, "Doc")) == 1
    assert conn.screenshots == 0


def test_freecad_error_and_missing_report_are_reported():
    bad = bt.beautify_operation(FakeConnection({"success": False, "error": "no doc"}), False, "Doc")
    assert "Beautify failed: no doc" in bad[0].text
    silent = bt.beautify_operation(FakeConnection({"success": True, "message": "hi"}), False, "Doc")
    assert "no report" in silent[0].text


def test_invalid_arguments_never_reach_freecad():
    conn = FakeConnection(_ok({}))
    out = bt.beautify_operation(conn, False, "Doc", palette="neon")
    assert "Invalid beautify arguments" in out[0].text and conn.code is None
