"""The run CLI turns evolution.author into the environment (author-evolution spec §12)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from orchestra.cli.run_codeprojecteval_decomp import configure_author_layer
from orchestra.control.author import assemble as A
from orchestra.control.fast_loop.evolution_config import AuthorConfig

KEYS = (A.RULES_DOC_ENV, A.INVENTORY_DIR_ENV, A.DOCS_DIR_ENV, A.PACKAGES_ENV, A.TASK_ENV, A.HARD_MIN_ENV,
        A.MAX_ROUNDS_ENV, A.PLAN_FILE_ENV)


def test_off_sets_nothing_and_on_sets_strings_with_the_full_plan(monkeypatch, tmp_path) -> None:
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    task = SimpleNamespace(task_id="tinydb", repo_root=tmp_path, source_dir="tinydb")
    configure_author_layer(AuthorConfig(), task=task, run_dir=tmp_path, plan_file=Path("x.json"))
    assert not any(k in os.environ for k in KEYS)
    on = AuthorConfig.from_mapping({"enabled": True, "inventory": True})
    configure_author_layer(on, task=task, run_dir=tmp_path / "run", plan_file=Path("one_milestone.plan.json"))
    assert all(isinstance(os.environ[k], str) for k in KEYS)
    assert os.environ[A.PLAN_FILE_ENV].endswith("cpe_feature_plans/tinydb.plan.json")
    assert os.environ[A.INVENTORY_DIR_ENV] == str(tmp_path / "run" / "author_inventory")
