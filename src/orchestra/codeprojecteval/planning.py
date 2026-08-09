"""Planning brief for CodeProjectEval repositories.

The dataset describes a project through a PRD, a pyreverse class diagram, an
architecture document and a directory tree, so the risk-first planner reads those
instead of RealBench's ``public_design/``.
"""

from __future__ import annotations

from orchestra.codeprojecteval.dataset import CpeTask
from orchestra.codeprojecteval.harness import expected_modules
from orchestra.realbench.milestone_planner import PlanningBrief


def cpe_brief(task: CpeTask) -> PlanningBrief:
    """Risk-first planning inputs for one CodeProjectEval repository."""
    documents: list[tuple[str, str]] = [
        ("PRD.md", task.read(task.prd_path, limit=12000)),
        ("architecture_design.md", task.read(task.architecture_path, limit=8000)),
        ("directory_tree.txt", task.directory_tree),
    ]
    for uml in task.uml_paths:
        documents.append((uml.rsplit("/", 1)[-1], task.read(uml, limit=8000)))

    return PlanningBrief(
        task_id=task.task_id,
        documents=documents,
        modules=expected_modules(task),
        exports={},
        acceptance_note=(
            "A visible `check_tests` suite ships with the repository and runs at "
            "every integration gate; the scoring suite is held out and you cannot "
            "see it. Acceptance may reference the design documents and the visible "
            "tests, never the held-out suite."
        ),
    )
