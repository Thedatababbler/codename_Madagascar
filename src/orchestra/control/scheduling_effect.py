"""Effective concurrency and no-op scheduling-candidate rejection."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from orchestra.control.slow_loop.schemas import SchedulingConcurrencyEdit, TaskSchedulingPolicy
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan


class SchedulingNoOpReason(StrEnum):
    NO_EFFECTIVE_RUNTIME_CHANGE = "NO_EFFECTIVE_RUNTIME_CHANGE"
    NO_FUTURE_PARALLEL_WAVE = "NO_FUTURE_PARALLEL_WAVE"
    RUNTIME_CAP_DOMINATED = "RUNTIME_CAP_DOMINATED"


TERMINAL_STATUSES = frozenset(
    {
        SubtaskStatus.COMMITTED,
        SubtaskStatus.FAILED,
        SubtaskStatus.HARNESS_FAILED,
        SubtaskStatus.SKIPPED,
    }
)


def effective_concurrency(
    *,
    runtime_concurrency_cap: int,
    policy_concurrency: int,
) -> int:
    """effective = min(runtime_cap, active_or_proposed_policy_concurrency)."""
    return max(1, min(int(runtime_concurrency_cap), int(policy_concurrency)))


def policy_concurrency(policy: TaskSchedulingPolicy | None) -> int:
    if policy is None:
        return 1
    return max(1, int(policy.max_concurrent_subtasks))


def remaining_subtasks(state: TaskExecutionState) -> list[str]:
    out: list[str] = []
    for sid, sub in state.subtasks.items():
        if sub.status in TERMINAL_STATUSES:
            continue
        if sub.lease_status == "leased":
            continue
        out.append(sid)
    return sorted(out)


def dependency_map(plan: TaskPlan) -> dict[str, set[str]]:
    return {s.subtask_id: set(s.dependencies) for s in plan.subtasks}


def future_ready_waves(
    *,
    plan: TaskPlan,
    state: TaskExecutionState,
) -> list[list[str]]:
    """Deterministic ready waves over remaining unleased unfinished subtasks.

    A wave is the set of remaining subtasks whose remaining dependencies are
    all outside the remaining set (already committed/terminal or absent).
    After emitting a wave, those IDs are treated as completed for subsequent
    waves so fork/join DAGs produce a genuine parallel future wave.
    """
    remaining = set(remaining_subtasks(state))
    if not remaining:
        return []
    deps = dependency_map(plan)
    committed = {
        sid
        for sid, sub in state.subtasks.items()
        if sub.status is SubtaskStatus.COMMITTED
    }
    waves: list[list[str]] = []
    pending = set(remaining)
    safety = 0
    while pending and safety < len(remaining) + 2:
        safety += 1
        done = committed | (remaining - pending)
        wave = sorted(
            sid
            for sid in pending
            if deps.get(sid, set()).issubset(done)
        )
        if not wave:
            # Cycle / blocked — stop rather than inventing parallelism.
            break
        waves.append(wave)
        pending -= set(wave)
    return waves


def max_future_wave_width(
    *,
    plan: TaskPlan,
    state: TaskExecutionState,
) -> int:
    waves = future_ready_waves(plan=plan, state=state)
    if not waves:
        return 1
    return max(len(w) for w in waves)


class SchedulingEffectAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason: SchedulingNoOpReason | None = None
    detail: str = ""
    current_effective: int = 1
    proposed_effective: int = 1
    runtime_cap: int = 1
    max_future_wave_width: int = 1


def assess_concurrency_edit(
    *,
    plan: TaskPlan,
    state: TaskExecutionState,
    current_policy: TaskSchedulingPolicy | None,
    proposed_concurrency: int,
    runtime_concurrency_cap: int,
) -> SchedulingEffectAssessment:
    """Reject concurrency edits that cannot change future execution."""
    cap = max(1, int(runtime_concurrency_cap))
    current_pol = policy_concurrency(current_policy)
    proposed_pol = max(1, int(proposed_concurrency))
    current_eff = effective_concurrency(
        runtime_concurrency_cap=cap, policy_concurrency=current_pol
    )
    proposed_eff = effective_concurrency(
        runtime_concurrency_cap=cap, policy_concurrency=proposed_pol
    )
    width = max_future_wave_width(plan=plan, state=state)

    if proposed_eff == current_eff:
        if proposed_pol > cap and current_eff == cap:
            return SchedulingEffectAssessment(
                ok=False,
                reason=SchedulingNoOpReason.RUNTIME_CAP_DOMINATED,
                detail=(
                    f"proposed policy={proposed_pol} but runtime_cap={cap} "
                    f"keeps effective={current_eff}"
                ),
                current_effective=current_eff,
                proposed_effective=proposed_eff,
                runtime_cap=cap,
                max_future_wave_width=width,
            )
        return SchedulingEffectAssessment(
            ok=False,
            reason=SchedulingNoOpReason.NO_EFFECTIVE_RUNTIME_CHANGE,
            detail=(
                f"effective concurrency unchanged at {current_eff} "
                f"(policy {current_pol}->{proposed_pol}, cap={cap})"
            ),
            current_effective=current_eff,
            proposed_effective=proposed_eff,
            runtime_cap=cap,
            max_future_wave_width=width,
        )

    # Increasing effective concurrency only helps if a future wave can use it.
    if proposed_eff > current_eff and width <= current_eff:
        return SchedulingEffectAssessment(
            ok=False,
            reason=SchedulingNoOpReason.NO_FUTURE_PARALLEL_WAVE,
            detail=(
                f"max future wave width={width} cannot benefit from "
                f"effective {current_eff}->{proposed_eff}"
            ),
            current_effective=current_eff,
            proposed_effective=proposed_eff,
            runtime_cap=cap,
            max_future_wave_width=width,
        )

    return SchedulingEffectAssessment(
        ok=True,
        current_effective=current_eff,
        proposed_effective=proposed_eff,
        runtime_cap=cap,
        max_future_wave_width=width,
    )


def concurrency_edit_from_candidate(edits: list) -> SchedulingConcurrencyEdit | None:
    for edit in edits:
        if isinstance(edit, SchedulingConcurrencyEdit):
            return edit
    return None


def concurrency_manifest_fields(
    *,
    runtime_concurrency_cap: int,
    policy: TaskSchedulingPolicy | None,
) -> dict[str, int]:
    pol = policy_concurrency(policy)
    return {
        "runtime_concurrency_cap": max(1, int(runtime_concurrency_cap)),
        "policy_concurrency": pol,
        "effective_concurrency": effective_concurrency(
            runtime_concurrency_cap=runtime_concurrency_cap,
            policy_concurrency=pol,
        ),
    }
