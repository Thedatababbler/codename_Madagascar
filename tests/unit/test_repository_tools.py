"""Unit tests for bounded smolagents repository-editing tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.tools.base import ToolBuildContext
from orchestra.tools.registry import get_tool_registry, reset_tool_registry_for_tests
from orchestra.tools.repository_tools import (
    REPOSITORY_TOOL_IDS,
    WorkspacePathError,
    resolve_authorized_path,
)


def _ctx(ws: Path, *, level: str = "discovery") -> ToolBuildContext:
    return ToolBuildContext(
        run_id="r",
        task_id="t",
        node_id="n",
        workspace_ref=str(ws),
        metadata={"public_harness_level": level},
    )


def _tools(ws: Path, *, level: str = "discovery"):
    reset_tool_registry_for_tests()
    reg = get_tool_registry()
    built = {
        tid: reg.build([tid], _ctx(ws, level=level))[0]
        for tid in REPOSITORY_TOOL_IDS
        if tid != "final_answer"
    }
    return built


def test_resolve_rejects_absolute_and_traversal(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(WorkspacePathError, match="absolute"):
        resolve_authorized_path(ws, "/etc/passwd")
    with pytest.raises(WorkspacePathError, match="traversal"):
        resolve_authorized_path(ws, "../escape.txt")
    with pytest.raises(WorkspacePathError, match="\\.git"):
        resolve_authorized_path(ws, ".git/config")


def test_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = ws / "leak"
    link.symlink_to(outside)
    with pytest.raises(WorkspacePathError, match="symlink escapes"):
        resolve_authorized_path(ws, "leak")


def test_write_read_list_and_protected_paths(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".adamas_trusted_harness").write_text("trusted\n", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_adamas_workspace_ok.py").write_text("def test_ok():\n    assert True\n")
    tools = _tools(ws)

    assert "OK wrote" in tools["write_workspace_file"]("pkg/mod.py", "x = 1\n")
    assert tools["read_workspace_file"]("pkg/mod.py") == "x = 1\n"
    listing = tools["list_workspace_files"](".")
    assert "pkg/mod.py" in listing
    assert ".adamas_trusted_harness" not in listing
    assert "test_adamas_workspace_ok.py" not in listing

    assert "forbidden" in tools["write_workspace_file"](
        ".adamas_trusted_harness", "hack"
    )
    assert "forbidden" in tools["read_workspace_file"](
        "tests/test_adamas_workspace_ok.py"
    )
    assert "absolute" in tools["read_workspace_file"]("/etc/passwd")
    assert "traversal" in tools["read_workspace_file"]("../x")


def test_apply_workspace_patch_and_public_check_server_controlled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestra.realbench.public_harness import materialize_public_harness

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "demo.py").write_text("print(1)\n", encoding="utf-8")
    manifest = materialize_public_harness(ws, harness_dir=tmp_path / "harness")
    monkeypatch.setenv("ADAMAS_PUBLIC_CHECK_SCRIPT", manifest.script_path)
    monkeypatch.setenv("ADAMAS_PUBLIC_CHECK_MANIFEST", manifest.manifest_path)
    tools = _tools(ws, level="discovery")
    assert "OK wrote" in tools["apply_workspace_patch"]("demo.py", "print(2)\n")
    assert (ws / "demo.py").read_text(encoding="utf-8") == "print(2)\n"
    out = tools["run_public_check"]()
    assert "exit=" in out
    assert "level=discovery" in out
    # The harness the agent can trigger is not reachable from the workspace.
    assert not (ws / "scripts").exists()
    monkeypatch.delenv("ADAMAS_PUBLIC_CHECK_SCRIPT")
    assert "not configured" in tools["run_public_check"]()


def test_workspace_ref_required() -> None:
    reset_tool_registry_for_tests()
    reg = get_tool_registry()
    with pytest.raises(WorkspacePathError):
        reg.build(
            ["list_workspace_files"],
            ToolBuildContext(workspace_ref=None),
        )


def test_bounded_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(10):
        (ws / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "orchestra.tools.repository_tools._MAX_LIST_ENTRIES", 3
    )
    tools = _tools(ws)
    listing = tools["list_workspace_files"](".")
    assert "truncated" in listing


def test_public_check_does_not_accept_model_command(tmp_path: Path) -> None:
    """run_public_check takes no command argument from the model."""
    ws = tmp_path / "ws"
    ws.mkdir()
    from orchestra.realbench.public_harness import materialize_public_harness

    materialize_public_harness(ws, harness_dir=tmp_path / "harness")
    tools = _tools(ws)
    fn = tools["run_public_check"]
    # Smolagents tools expose a forward callable without user command params.
    assert "command" not in getattr(fn, "inputs", {}) and "command" not in str(
        getattr(fn, "__signature__", "")
    )
