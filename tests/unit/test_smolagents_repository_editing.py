"""Unit tests for smolagents git-derived RepositoryChangeArtifact path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestra.backends.base import (
    AgentRequest,
    AgentRunStatus,
    BackendExecutionContext,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.catalog import capabilities_for
from orchestra.backends.smolagents_code import SmolagentsCodeBackend
from orchestra.schemas.artifacts import RepositoryChangeArtifact


def _git_init(ws: Path) -> None:
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(ws), "config", "user.email", "t@local"], check=True
    )
    subprocess.run(
        ["git", "-C", str(ws), "config", "user.name", "t"], check=True
    )
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "commit", "-m", "base", "--allow-empty"],
        check=True,
        capture_output=True,
    )


def _repo_request(**overrides) -> AgentRequest:
    base = {
        "request_id": "r1",
        "task_id": "t1",
        "node_id": "smolagents_implementer",
        "role": "coder",
        "instruction": "edit",
        "rendered_context": "edit workspace",
        "model": ModelSpec(provider="fixture", name="scripted"),
        "tools": [
            "list_workspace_files",
            "write_workspace_file",
            "final_answer",
        ],
        "max_steps": 4,
        "timeout_seconds": 60.0,
        "output_contract": OutputContract(
            parser_id="repository_change",
            output_schema="RepositoryChangeArtifact",
        ),
        "backend_config": {
            "type": "smolagents_code",
            "max_steps": 4,
            "executor_type": "local",
            "use_structured_outputs_internally": True,
            "require_git_diff": True,
            "public_harness_level": "discovery",
            "additional_authorized_imports": [],
            "fixture_responses": [],
        },
    }
    base.update(overrides)
    return AgentRequest(**base)


def _ctx(tmp_path: Path, ws: Path) -> BackendExecutionContext:
    return BackendExecutionContext(
        run_id="run",
        task_id="t1",
        node_id="smolagents_implementer",
        subtask_id="s1",
        workspace_ref=str(ws),
        trace_dir=str(tmp_path / "traces"),
    )


def test_capability_declares_repository_editing() -> None:
    caps = capabilities_for("smolagents_code")
    assert caps is not None
    assert caps.repository_editing is True
    assert SmolagentsCodeBackend().capabilities.repository_editing is True


@pytest.mark.asyncio
async def test_workspace_ref_propagation_into_worker_payload(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _git_init(ws)
    seen: dict = {}

    def runner(payload: dict) -> dict:
        seen.update(payload)
        (Path(payload["workspace_ref"]) / "a.py").write_text("x=1\n", encoding="utf-8")
        return {
            "ok": True,
            "status": "success",
            "error": None,
            "final_output": "MODEL CLAIMS PATCH: fake",
            "trace_events": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            "step_count": 1,
            "backend_metadata": {},
        }

    backend = SmolagentsCodeBackend(worker_runner=runner)
    result = await backend.run(_repo_request(), _ctx(tmp_path, ws))
    assert result.status is AgentRunStatus.SUCCESS
    assert seen["workspace_ref"] == str(ws)
    assert seen["subtask_id"] == "s1"
    art = RepositoryChangeArtifact.model_validate(result.output_artifacts[0].payload)
    assert "a.py" in art.changed_files
    assert "MODEL CLAIMS PATCH" not in art.patch
    assert "a.py" in art.patch or art.changed_files == ["a.py"]
    assert result.backend_metadata.get("model_patch_ignored") is True
    assert result.backend_metadata.get("change_evidence") == "git_workspace_snapshot"
    assert result.backend_metadata["usage_provenance"]["prompt_tokens"] == "provider"


@pytest.mark.asyncio
async def test_empty_real_diff_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _git_init(ws)

    def runner(_payload: dict) -> dict:
        return {
            "ok": True,
            "status": "success",
            "error": None,
            "final_output": json.dumps(
                {
                    "changed_files": ["invented.py"],
                    "patch": "diff --git a/invented.py b/invented.py\n+fake\n",
                }
            ),
            "trace_events": [],
            "usage": {},
            "step_count": 1,
            "backend_metadata": {},
        }

    backend = SmolagentsCodeBackend(worker_runner=runner)
    result = await backend.run(_repo_request(), _ctx(tmp_path, ws))
    assert result.status is AgentRunStatus.OUTPUT_CONTRACT_FAILURE
    assert "empty" in (result.error.message if result.error else "").lower()


@pytest.mark.asyncio
async def test_missing_workspace_ref_rejected(tmp_path: Path) -> None:
    backend = SmolagentsCodeBackend(
        worker_runner=lambda _p: {"status": "success", "final_output": "x"}
    )
    ctx = BackendExecutionContext(
        run_id="run",
        task_id="t1",
        node_id="n",
        workspace_ref=None,
        trace_dir=str(tmp_path / "traces"),
    )
    result = await backend.run(_repo_request(), ctx)
    assert result.status is AgentRunStatus.INVALID_REQUEST


@pytest.mark.asyncio
async def test_tracked_untracked_delete_rename_and_ordering(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "old_name.py").write_text("v1\n", encoding="utf-8")
    (ws / "keep.py").write_text("keep\n", encoding="utf-8")
    (ws / "gone.py").write_text("bye\n", encoding="utf-8")
    _git_init(ws)

    def runner(payload: dict) -> dict:
        root = Path(payload["workspace_ref"])
        (root / "keep.py").write_text("keep2\n", encoding="utf-8")
        (root / "new_file.py").write_text("new\n", encoding="utf-8")
        (root / "gone.py").unlink()
        (root / "old_name.py").rename(root / "renamed.py")
        return {
            "ok": True,
            "status": "success",
            "error": None,
            "final_output": "done",
            "trace_events": [],
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "step_count": 2,
            "backend_metadata": {},
        }

    backend = SmolagentsCodeBackend(worker_runner=runner)
    result = await backend.run(_repo_request(), _ctx(tmp_path, ws))
    assert result.status is AgentRunStatus.SUCCESS
    art = RepositoryChangeArtifact.model_validate(result.output_artifacts[0].payload)
    assert art.changed_files == sorted(art.changed_files)
    assert "new_file.py" in art.changed_files
    assert "keep.py" in art.changed_files
    assert "gone.py" in art.changed_files or "renamed.py" in art.changed_files
    prov = result.backend_metadata["usage_provenance"]
    assert prov["prompt_tokens"] == "provider"
    assert prov["total_tokens"] in {"provider", "derived_sum"}


@pytest.mark.asyncio
async def test_harness_scaffolding_excluded_from_agent_diff(tmp_path: Path) -> None:
    from orchestra.realbench.public_harness import materialize_public_harness

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".adamas_trusted_harness").write_text("trusted\n", encoding="utf-8")
    materialize_public_harness(ws)
    _git_init(ws)

    def runner(payload: dict) -> dict:
        Path(payload["workspace_ref"], "agent_only.py").write_text(
            "ok=1\n", encoding="utf-8"
        )
        return {
            "ok": True,
            "status": "success",
            "final_output": "ok",
            "trace_events": [],
            "usage": {},
            "step_count": 1,
            "backend_metadata": {},
        }

    backend = SmolagentsCodeBackend(worker_runner=runner)
    result = await backend.run(_repo_request(), _ctx(tmp_path, ws))
    art = RepositoryChangeArtifact.model_validate(result.output_artifacts[0].payload)
    assert art.changed_files == ["agent_only.py"]
    assert ".adamas_trusted_harness" not in art.changed_files
    assert "adamas_public_check.py" not in " ".join(art.changed_files)


@pytest.mark.asyncio
async def test_usage_provenance_unavailable_when_missing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _git_init(ws)

    def runner(payload: dict) -> dict:
        Path(payload["workspace_ref"], "x.py").write_text("1\n", encoding="utf-8")
        return {
            "ok": True,
            "status": "success",
            "final_output": "ok",
            "trace_events": [],
            "usage": {},
            "step_count": 1,
            "backend_metadata": {},
        }

    result = await SmolagentsCodeBackend(worker_runner=runner).run(
        _repo_request(), _ctx(tmp_path, ws)
    )
    prov = result.backend_metadata["usage_provenance"]
    assert prov["prompt_tokens"] == "unavailable"
    assert prov["completion_tokens"] == "unavailable"
