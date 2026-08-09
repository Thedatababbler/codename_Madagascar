"""CodeProjectEval integration: dataset adapter, milestone harness, planning brief."""

from orchestra.codeprojecteval.dataset import (
    CpeTask,
    available_tasks,
    build_agent_workspace,
    load_task,
)
from orchestra.codeprojecteval.harness import (
    CpeHarnessManifest,
    check_command,
    materialize_check_harness,
)
from orchestra.codeprojecteval.planning import cpe_brief

__all__ = [
    "CpeHarnessManifest",
    "CpeTask",
    "available_tasks",
    "build_agent_workspace",
    "check_command",
    "cpe_brief",
    "load_task",
    "materialize_check_harness",
]
