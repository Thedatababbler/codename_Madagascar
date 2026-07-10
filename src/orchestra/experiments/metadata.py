"""Run metadata helpers for Stage 1 experiments."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph


def git_commit_hash(repo_root: str | Path | None = None) -> str | None:
    root = Path(repo_root or Path.cwd())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def prompt_hash(contracts_dir: str | Path) -> str:
    contracts = load_contracts(contracts_dir)
    digest = hashlib.sha256(
        "".join(contracts[key].model_dump_json() for key in sorted(contracts)).encode()
    ).hexdigest()
    return digest


def contract_hash(contracts_dir: str | Path) -> str:
    return prompt_hash(contracts_dir)


def build_run_metadata(
    *,
    config,
    manifest,
    graph,
    contracts_dir: str,
    started_at: datetime | None = None,
    repo_root: str | Path | None = None,
) -> dict:
    contracts = load_contracts(contracts_dir)
    graph_obj = load_graph(graph) if isinstance(graph, (str, Path)) else graph
    return {
        "started_at": (started_at or datetime.now(UTC)).isoformat(),
        "git_commit": git_commit_hash(repo_root),
        "livecodebench_release": config.benchmark.release_version,
        "livecodebench_evaluator_commit": manifest.livecodebench_commit,
        "manifest_hash": manifest.manifest_sha256,
        "graph_hash": graph_obj.content_hash if hasattr(graph_obj, "content_hash") else None,
        "contract_hash": contract_hash(contracts_dir),
        "prompt_hash": prompt_hash(contracts_dir),
        "models": {
            contract.contract_id: contract.model for contract in contracts.values()
        },
        "temperatures": {
            contract.contract_id: contract.temperature for contract in contracts.values()
        },
        "max_tokens": {
            contract.contract_id: contract.max_tokens for contract in contracts.values()
        },
        "sandbox": config.sandbox.model_dump(),
        "runtime": config.runtime.model_dump(),
        "repair_limit": config.evaluation.get("max_repair_attempts"),
    }


def finalize_run_metadata(run_dir: str | Path, *, completed_at: datetime | None = None) -> dict:
    root = Path(run_dir)
    manifest_path = root / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = payload.setdefault("metadata", {})
    metadata["completed_at"] = (completed_at or datetime.now(UTC)).isoformat()
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return metadata
