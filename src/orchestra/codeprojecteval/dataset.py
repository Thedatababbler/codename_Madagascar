"""CodeProjectEval task loading and agent-workspace construction.

The dataset ships each repository as PRD + UML + architecture design + a visible
``check_tests`` suite + a held-out ``unit_tests`` suite + the reference
implementation. An agent workspace therefore contains the design documents and
the visible tests only: the reference implementation and the held-out suite are
withheld, and everything AdaMAS invents stays runner-side as on RealBench.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# CPE_DATASET_ROOT / CPE_ENV_ROOT let another dataset in the same on-disk
# shape (e.g. NL2Repo-Bench tasks converted by scripts/nl2repo_to_cpe.py) run
# through the same loader, runner and harness without touching the originals.
DEFAULT_DATASET_ROOT = Path(os.environ.get("CPE_DATASET_ROOT") or 
    "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset"
)
DEFAULT_ENV_ROOT = Path(os.environ.get("CPE_ENV_ROOT") or "/root/codex-benchmarks/cpe_envs")

CHECK_TESTS_DIRNAME = "check_tests"
UNIT_TESTS_DIRNAME = "unit_tests"
# Dataset repositories carry build residue (voluptuous ships a broken venv).
_WORKSPACE_SKIP = {
    ".git",
    ".venv",
    "venv",
    "bin",
    "lib",
    "lib64",
    "include",
    "share",
    "pyvenv.cfg",
    "__pycache__",
    ".pytest_cache",
    ".idea",
}


@dataclass(frozen=True)
class CpeTask:
    """One CodeProjectEval repository, resolved from its ``config.json``."""

    task_id: str
    repo_root: Path
    language: str
    source_dir: str
    prd_path: str
    uml_paths: list[str]
    architecture_path: str
    directory_tree_path: str
    requirements_path: str
    check_tests: str
    unit_tests: str
    required_files: list[str] = field(default_factory=list)
    raw_config: dict[str, Any] = field(default_factory=dict)

    @property
    def env_python(self) -> Path:
        return DEFAULT_ENV_ROOT / self.task_id / "bin" / "python"

    def read(self, relative: str, *, limit: int = 20000) -> str:
        path = self.repo_root / relative
        if not relative or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:limit]

    @property
    def directory_tree(self) -> str:
        return self.read(self.directory_tree_path, limit=8000)


def load_task(task_id: str, *, dataset_root: Path = DEFAULT_DATASET_ROOT) -> CpeTask:
    """Resolve one repository's declared layout from its ``config.json``."""
    repo_root = Path(dataset_root) / task_id
    config_path = repo_root / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"missing CodeProjectEval config: {config_path}")
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    uml = cfg.get("UML") or []
    if isinstance(uml, str):
        uml = [uml]
    return CpeTask(
        task_id=task_id,
        repo_root=repo_root,
        language=str(cfg.get("language") or "python"),
        source_dir=str(cfg.get("source_code") or ""),
        prd_path=str(cfg.get("PRD") or ""),
        uml_paths=[str(p) for p in uml],
        architecture_path=str(cfg.get("architecture_design") or ""),
        directory_tree_path="docs/directory_tree.txt",
        requirements_path=str(cfg.get("dependencies") or "requirements.txt"),
        check_tests=str(cfg.get("check_tests") or CHECK_TESTS_DIRNAME),
        unit_tests=str(cfg.get("unit_tests") or UNIT_TESTS_DIRNAME),
        required_files=[str(p) for p in (cfg.get("required_files") or [])],
        raw_config=cfg,
    )


def available_tasks(dataset_root: Path = DEFAULT_DATASET_ROOT) -> list[str]:
    return sorted(p.name for p in Path(dataset_root).iterdir() if p.is_dir())


def _copy(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns(*_WORKSPACE_SKIP),
            ignore_dangling_symlinks=True,
            dirs_exist_ok=True,
        )
    else:
        shutil.copy2(src, dst)


def build_agent_workspace(task: CpeTask, destination: Path) -> Path:
    """Materialize the workspace an agent starts from.

    Includes the design documents, the dependency manifest and the visible
    ``check_tests``; excludes the reference implementation and the held-out
    ``unit_tests``, which decide the score offline.
    """
    dest = Path(destination)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    for relative in [
        task.prd_path,
        *task.uml_paths,
        task.architecture_path,
        task.directory_tree_path,
        task.requirements_path,
        *task.required_files,
        "README.md",
        "README.rst",
    ]:
        if relative:
            _copy(task.repo_root / relative, dest / relative)
    _copy(task.repo_root / task.check_tests, dest / task.check_tests)

    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    from orchestra.control.fast_loop.workspace import write_cache_exclude

    write_cache_exclude(dest)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=adamas@local",
            "-c",
            "user.name=AdaMAS",
            "commit",
            "-q",
            "-m",
            "dataset inputs only",
        ],
        cwd=dest,
        check=True,
    )
    return dest
