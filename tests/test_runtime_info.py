from freecad_mcp import runtime_info


def test_runtime_marks_stale_process(monkeypatch):
    monkeypatch.setattr(runtime_info, "LOADED_REVISION", "old")
    monkeypatch.setattr(runtime_info, "revision", lambda: "new")
    monkeypatch.setattr(runtime_info, "source_hash", lambda: "changed")
    result=runtime_info.toolchain_status("new")
    assert result["producer"]=="freecad-mcp"
    assert result["restart_required"]
    assert not result["expected_revision_matches"]
    assert result["freecad_rpc"]=="not_probed"

