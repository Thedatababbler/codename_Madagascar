"""Configuration for the standard search procedure of the self-evolving loop.

Read from the experiment YAML's top-level ``evolution`` / ``probes`` /
``budget`` / ``acceptance`` blocks (adamas_milestone_self_evolution_prompt.md
§10). Everything defaults to *off*, in which case the controller behaves
exactly as before this module existed; a run that never sets
``evolution.enabled`` cannot be changed by it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Env override for the repair slot's trigger, read by the subgraph builder
#: (which is called from three places and would otherwise need the option
#: threaded through every one of them).
REPAIR_TRIGGER_ENV = "ADAMAS_REPAIR_TRIGGER"
REPAIR_TRIGGERS = ("gate", "failures")


def repair_trigger_from_env(default: str = "gate") -> str:
    value = (os.getenv(REPAIR_TRIGGER_ENV) or default or "gate").strip().lower()
    return value if value in REPAIR_TRIGGERS else "gate"


@dataclass(frozen=True)
class AuthorConfig:
    """``evolution.author`` (author-evolution spec §12). Everything off by default."""

    enabled: bool = False
    inventory: bool = False
    publish: bool = False
    require_human_approval: bool = True
    confirm_rerun: bool = True
    max_proposals: int = 2
    k: int = 2
    split_file: str = "configs/datasets/author_evolution_split.yaml"
    coverage_hard_min: float = 1.0
    coverage_max_rounds: int = 2
    rules_max_active: int = 15
    accept_miss_delta: float = 0.05
    accept_ref_fail_tol: float = 0.02
    accept_fp_tol: float = 0.02
    accept_disc_tol: float = 0.05
    accept_cost_tol: float = 0.3
    accept_min_milestones: int = 5
    confirm_n: int = 4
    confirm_tol: float = 5.0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> AuthorConfig:
        a = dict(payload or {})
        if not bool(a.get("enabled", False)):
            return cls()
        cov = dict(a.get("coverage") or {})
        rules = dict(a.get("rules") or {})
        acc = dict(a.get("accept") or {})
        conf = dict(a.get("confirm") or {})
        return cls(
            enabled=True,
            inventory=bool(a.get("inventory", False)),
            publish=bool(a.get("publish", False)),
            require_human_approval=bool(a.get("require_human_approval", True)),
            confirm_rerun=bool(a.get("confirm_rerun", True)),
            max_proposals=int(a.get("max_proposals", 2)),
            k=int(a.get("k", 2)),
            split_file=str(a.get("split_file") or cls.split_file),
            coverage_hard_min=float(cov.get("hard_min", 1.0)),
            coverage_max_rounds=int(cov.get("max_rounds", 2)),
            rules_max_active=int(rules.get("max_active", 15)),
            accept_miss_delta=float(acc.get("miss_delta", 0.05)),
            accept_ref_fail_tol=float(acc.get("ref_fail_tol", 0.02)),
            accept_fp_tol=float(acc.get("fp_tol", 0.02)),
            accept_disc_tol=float(acc.get("disc_tol", 0.05)),
            accept_cost_tol=float(acc.get("cost_tol", 0.3)),
            accept_min_milestones=int(acc.get("min_milestones", 5)),
            confirm_n=int(conf.get("n", 4)),
            confirm_tol=float(conf.get("tol", 5.0)),
        )


@dataclass(frozen=True)
class EvolutionConfig:
    enabled: bool = False
    #: Probes: how many resamples of the incumbent design, whether the count
    #: adapts (1 when the first probe's failure set equals the incumbent's,
    #: 3 when the flaky set reaches ``flaky_extra_threshold``), and whether
    #: they are budgeted apart from the candidate slots.
    probes_default: int = 2
    probes_adaptive: bool = True
    flaky_extra_threshold: int = 3
    probes_separate: bool = True
    #: Row slots after R0: mode A = one active row + one trial slot; mode B =
    #: one slot, given to a trial row with probability ``trial_prob``.
    budget_mode: str = "B"
    trial_prob: float = 0.3
    #: Unified per-case acceptance instead of the Pareto selector.
    unified_acceptance: bool = True
    #: Diagnostic re-run of every committed predecessor's frozen suite on each
    #: candidate, for the zero-new-regressions condition.
    prior_suite_check: bool = True
    #: Flaky-only searches: how many agent-free re-runs of the frozen suite a
    #: candidate must pass to be committed as a ``flaky_resolution`` (0 = never).
    flaky_reverify_runs: int = 2
    #: The repair slot runs when the early gate reports failures ("failures")
    #: or only when it fails outright ("gate", the historical behaviour).
    repair_trigger: str = "failures"
    #: The `thr` block: first-pass triggers and row preconditions (§10).
    thresholds: tuple[tuple[str, float], ...] = ()
    #: The author's evolvable layer (``evolution.author``), off by default.
    author: AuthorConfig = AuthorConfig()

    @property
    def thr(self) -> dict[str, float]:
        return dict(self.thresholds)

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> EvolutionConfig:
        cfg = dict(config or {})
        evo = dict(cfg.get("evolution") or {})
        probes = dict(cfg.get("probes") or {})
        budget = dict(cfg.get("budget") or {})
        acceptance = dict(cfg.get("acceptance") or {})
        enabled = bool(evo.get("enabled", False))
        if not enabled:
            return cls()
        mode = str(budget.get("mode", "B")).strip().upper()
        if mode not in ("A", "B"):
            raise ValueError(f"budget.mode must be A or B, got {mode!r}")
        trigger = str(evo.get("repair_trigger", "failures")).strip().lower()
        if trigger not in REPAIR_TRIGGERS:
            raise ValueError(f"evolution.repair_trigger must be one of {REPAIR_TRIGGERS}, got {trigger!r}")
        default = int(probes.get("default", 2))
        if default < 1:
            raise ValueError("probes.default must be at least 1")
        return cls(
            enabled=True,
            probes_default=default,
            probes_adaptive=bool(probes.get("adaptive", True)),
            flaky_extra_threshold=int(probes.get("flaky_extra_threshold", 3)),
            probes_separate=bool(budget.get("probes_separate", True)),
            budget_mode=mode,
            trial_prob=float(cfg.get("trial", {}).get("prob", 0.3) if isinstance(cfg.get("trial"), Mapping) else 0.3),
            unified_acceptance=bool(acceptance.get("unified", True)),
            prior_suite_check=bool(acceptance.get("prior_suite_check", True)),
            flaky_reverify_runs=int(acceptance.get("flaky_reverify_runs", 2)),
            repair_trigger=trigger,
            thresholds=tuple((str(k), float(v)) for k, v in dict(cfg.get("thr") or {}).items()),
            author=AuthorConfig.from_mapping(evo.get("author")),
        )

    @property
    def row_slots(self) -> int:
        """Candidate slots after R0 (R0 itself is always run)."""
        return 2 if self.budget_mode == "A" else 1

    @property
    def max_probes(self) -> int:
        return max(self.probes_default, 3) if self.probes_adaptive else self.probes_default


__all__ = ["AuthorConfig", "EvolutionConfig", "REPAIR_TRIGGERS", "REPAIR_TRIGGER_ENV", "repair_trigger_from_env"]
