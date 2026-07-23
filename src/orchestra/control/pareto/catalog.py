"""Configuration-defined safe future orchestra candidate catalog (M6.2)."""

from __future__ import annotations

from pathlib import Path

from orchestra.cli.validate_graph import build_compiler
from orchestra.control.pareto.schemas import ObjectiveSource, ObjectiveValue
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    GlobalEdit,
    PendingBackendAssignmentEdit,
    PendingGraphTemplateEdit,
    SchedulingConcurrencyEdit,
    SerializationGroupEdit,
)
from orchestra.experiments.control_plane import CandidateCatalogSection
from orchestra.ir.graph import load_graph


class SafeCandidateCatalog:
    """Allowlisted future-only edit sources for Pareto generation."""

    def __init__(
        self,
        section: CandidateCatalogSection | None = None,
        *,
        contracts_dir: str = "configs/contracts",
        repo_root: str | Path | None = None,
    ) -> None:
        self.section = section or CandidateCatalogSection()
        self.contracts_dir = contracts_dir
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self._compiler = build_compiler(contracts_dir)
        self._allowlisted_graphs = {
            item.graph_path: item for item in self.section.graph_templates
        }

    def graph_template_edits(
        self, *, eligible: set[str], role_to_subtask: dict[str, str] | None = None
    ) -> list[tuple[list[GlobalEdit], dict[str, ObjectiveValue | None]]]:
        """Return (edits, optional_declared_objectives) for allowlisted templates.

        Declared cost/latency may be attached as evidence; when absent the caller
        must leave those objectives unavailable rather than inventing zeros.
        """
        role_map = role_to_subtask or {}
        out: list[tuple[list[GlobalEdit], dict[str, ObjectiveValue | None]]] = []
        for item in self.section.graph_templates:
            targets = self._resolve_targets(item.target_roles, eligible, role_map)
            if not targets:
                continue
            if not self.compile_graph_path(item.graph_path):
                continue
            for sid in targets:
                edits: list[GlobalEdit] = [
                    PendingGraphTemplateEdit(
                        subtask_id=sid, graph_template_id=item.graph_path
                    )
                ]
                declared: dict[str, ObjectiveValue | None] = {
                    "cost": None,
                    "latency": None,
                }
                if item.declared_cost_usd is not None:
                    declared["cost"] = ObjectiveValue(
                        value=float(item.declared_cost_usd),
                        available=True,
                        source=ObjectiveSource.DECLARED_BUDGET,
                        detail=f"catalog:{item.template_id}",
                    )
                if item.declared_latency_seconds is not None:
                    declared["latency"] = ObjectiveValue(
                        value=float(item.declared_latency_seconds),
                        available=True,
                        source=ObjectiveSource.DECLARED_BUDGET,
                        detail=f"catalog:{item.template_id}",
                    )
                out.append((edits, declared))
        return out

    def scheduling_edits(self, *, current_concurrency: int) -> list[list[GlobalEdit]]:
        edits: list[list[GlobalEdit]] = []
        for concurrency in sorted(set(self.section.concurrency_alternatives)):
            if concurrency != current_concurrency and concurrency >= 1:
                edits.append(
                    [SchedulingConcurrencyEdit(max_concurrent_subtasks=concurrency)]
                )
        return edits

    def serialization_edits(self, *, eligible: set[str]) -> list[list[GlobalEdit]]:
        edits: list[list[GlobalEdit]] = []
        for group in self.section.serialization_groups:
            members = [sid for sid in group if sid in eligible]
            if len(members) >= 2:
                edits.append([SerializationGroupEdit(subtask_ids=sorted(members))])
        return edits

    def context_budget_edits(
        self, *, eligible: set[str], current_budgets: dict[str, int]
    ) -> list[list[GlobalEdit]]:
        edits: list[list[GlobalEdit]] = []
        for sid, alternatives in sorted(self.section.context_budget_alternatives.items()):
            if sid not in eligible:
                continue
            current = current_budgets.get(sid)
            for tokens in sorted(set(alternatives)):
                if tokens >= 64 and tokens != current:
                    edits.append(
                        [ContextBudgetEdit(target_subtask_id=sid, max_tokens=tokens)]
                    )
        return edits

    def backend_assignment_edits(
        self,
        *,
        eligible: set[str],
        allowed: dict[str, list[str]],
        resolve_node,
    ) -> list[list[GlobalEdit]]:
        """Build allowlisted backend reassignment edits via resolver callback."""
        edits: list[list[GlobalEdit]] = []
        flat_allowed = sorted({b for values in allowed.values() for b in values})
        for sid in sorted(eligible)[:2]:
            resolution = resolve_node(sid)
            if not getattr(resolution, "eligible", False):
                continue
            node_id = getattr(resolution, "node_id", None)
            current = getattr(resolution, "current_backend_id", None)
            if not node_id:
                continue
            for backend_id in flat_allowed:
                if backend_id == current:
                    continue
                edits.append(
                    [
                        PendingBackendAssignmentEdit(
                            subtask_id=sid,
                            node_id=node_id,
                            backend_id=backend_id,
                        )
                    ]
                )
                break
        return edits

    def is_allowlisted_graph(self, graph_path: str) -> bool:
        if not self._allowlisted_graphs:
            # Empty catalog means graph-template candidates are disabled.
            return False
        return graph_path in self._allowlisted_graphs

    def compile_graph_path(self, graph_path: str) -> bool:
        path = self.repo_root / graph_path
        if not path.exists():
            path = Path(graph_path)
        if not path.exists():
            return False
        normalized = str(path).replace("\\", "/")
        if "/.staging-" in normalized or ".." in Path(graph_path).parts:
            return False
        try:
            graph = load_graph(path)
            self._compiler.compile(graph)
        except Exception:
            return False
        return True

    @staticmethod
    def _resolve_targets(
        roles: list[str],
        eligible: set[str],
        role_map: dict[str, str],
    ) -> list[str]:
        if not roles:
            return sorted(eligible)[:1]
        targets: list[str] = []
        for role in roles:
            if role in eligible:
                targets.append(role)
            elif role in role_map and role_map[role] in eligible:
                targets.append(role_map[role])
        return sorted(set(targets))
