"""Read a harness's graded progress out of its stdout.

A gate answers one question -- did this milestone pass -- and a tuning loop
cannot climb an answer like that. Two attempts that both failed are
indistinguishable to it, even when one produced a package that imports cleanly
and fails two tests and the other produced nothing importable at all.

Harnesses that know how far they got print a single machine-readable line; the
rest print nothing and are reported as having no score, which is different from
scoring zero.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from orchestra.schemas.artifacts import (
    HarnessStageResult,
    RepositoryHarnessResultArtifact,
)

MARKER = "ADAMAS_HARNESS_SCORE "

#: Stages that measure whether the code *works*, best first. The rest of a graded
#: score measures whether it exists and imports, which saturates: EXP-20260810-05
#: found every committed imapclient milestone at compile 1/1, imports 17/17,
#: contracts 18/18. Those stages therefore contribute a constant, and a constant
#: does not rank anything -- it just divides whatever signal remains by the weight
#: it occupies. Blending them in compressed a real 0.069 spread on the authored
#: suite down to 0.021 on the blended score, which is inside the 0.02 quality
#: epsilon, i.e. reported as a tie.
BEHAVIOUR_STAGES: tuple[str, ...] = ("spec_tests", "tests")

#: The authored suite's stage and directory. `tests` is deliberately absent: that
#: is the dataset's own visible suite, which an agent is meant to read and be
#: given feedback on.
HIDDEN_STAGES: tuple[str, ...] = ("spec_tests",)
HIDDEN_DIR = "spec_tests"


def redact_hidden_suite(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A harness report with every trace of the authored suite removed.

    The full report is what the selector ranks on and what a human debugs from,
    so it is stored intact. This is the copy an agent may read, and an agent may
    not read any of it: the suite is the yardstick it is ranked against.

    Three fields carry it, and each was found leaking by a different test. The
    score line names every failed test. The custody step's own output gives the
    absolute path it moved the suite to, which an agent with a shell can simply
    open. And the change artifact lists the suite's files by name.
    """
    redacted = dict(payload)
    for key in ("stdout_summary", "stderr_summary"):
        text = redacted.get(key)
        if isinstance(text, str) and text:
            kept = [
                line
                for line in text.splitlines()
                if HIDDEN_DIR not in line and not line.lstrip().startswith(MARKER)
            ]
            # Rewritten only when a line went, so a report that never mentioned
            # the suite comes back byte for byte.
            if len(kept) != len(text.splitlines()):
                redacted[key] = "\n".join(kept)
    stages = redacted.get("stages")
    if isinstance(stages, list):
        redacted["stages"] = [
            stage
            for stage in stages
            if not (
                isinstance(stage, Mapping) and str(stage.get("stage")) in HIDDEN_STAGES
            )
        ]
    if str(redacted.get("furthest_stage") or "") in HIDDEN_STAGES:
        redacted["furthest_stage"] = ""
    changed = redacted.get("changed_files")
    if isinstance(changed, list):
        redacted["changed_files"] = [
            path for path in changed if HIDDEN_DIR not in str(path)
        ]
    return redacted


def parse_progress(stdout: str) -> tuple[float | None, list[HarnessStageResult], str]:
    """``(score, stages, furthest_stage)`` from a harness's output.

    Returns ``(None, [], "")`` for a harness that does not report progress, such
    as a plain ``pytest`` command. Callers must not read that as zero.
    """
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith(MARKER):
            continue
        try:
            payload = json.loads(line[len(MARKER):])
        except json.JSONDecodeError:
            return None, [], ""
        stages = [
            HarnessStageResult(
                stage=str(entry.get("stage") or ""),
                passed_units=int(entry.get("passed_units") or 0),
                total_units=int(entry.get("total_units") or 0),
                weight=float(entry.get("weight") or 0.0),
                failed_tests=[str(t) for t in (entry.get("failed_tests") or [])],
            )
            for entry in (payload.get("stages") or [])
        ]
        score = payload.get("score")
        return (
            None if score is None else max(0.0, min(1.0, float(score))),
            stages,
            str(payload.get("furthest_stage") or ""),
        )
    return None, [], ""


async def best_harness_progress(
    result: Any, artifact_store: Any
) -> tuple[float | None, list[HarnessStageResult], str]:
    """The furthest a graph execution's acceptance harnesses reported getting.

    A milestone can run more than one: ``gate_then_repair`` probes midway as
    well as gating at the end, and what the milestone is worth is how far it
    eventually got, not what the first probe saw.

    Returns the same shape as ``parse_progress``. The per-stage breakdown is
    what makes a score readable -- 0.62 says little, "contracts 12/19" says
    where to look -- so it travels with the number rather than being dropped
    at the first hop.
    """
    best: float | None = None
    stages: list[HarnessStageResult] = []
    stage = ""
    for outputs in getattr(result.state, "node_outputs", {}).values():
        for artifact_id in outputs.values():
            try:
                artifact = await artifact_store.get(artifact_id)
            except KeyError:
                continue
            if artifact.artifact_type != "RepositoryHarnessResultArtifact":
                continue
            payload = RepositoryHarnessResultArtifact.model_validate(artifact.payload)
            if payload.score is None:
                continue
            if best is None or payload.score > best:
                best = payload.score
                stages = list(payload.stages or [])
                stage = payload.furthest_stage
    return best, stages, stage


def behaviour_score(stages: Any) -> float | None:
    """The fraction of the behavioural stage that passed, or ``None``.

    This is the part of a graded score that can still move once a milestone is
    committed, and it is what a quality comparison should read. ``None`` when the
    harness reported no behavioural stage at all -- which is different from
    scoring zero, and callers must not conflate them: a milestone whose gate is a
    plain ``pytest`` invocation has no stage breakdown, and treating that as 0.0
    would rank it below every milestone that merely failed to import.

    A single stage is used rather than an average of both, since ``spec_tests``
    and ``tests`` measure the same repository against two suites of very different
    size and authorship; ``spec_tests`` wins because it was written for this
    milestone's scope, while the dataset's visible suite spans the whole project
    and saturates early.
    """
    stage = _behavioural_stage(stages)
    if stage is None:
        return None
    _passed, total, _failed = stage
    # A stage that collected nothing measured nothing. Reporting 0/0 as 0.0 would
    # make an empty suite the worst possible design.
    if total <= 0:
        return None
    return max(0.0, min(1.0, _passed / total))


def behaviour_failures(stages: Any) -> list[str]:
    """Which tests the behavioural stage named as failing.

    Read off the same stage `behaviour_score` scored, so the two describe one
    measurement. Empty is not "everything passed": a stage that reports no
    identities and one where nothing failed are indistinguishable here, and callers
    that need to tell them apart must check the counts.
    """
    stage = _behavioural_stage(stages)
    return list(stage[2]) if stage is not None else []


def behaviour_total(stages: Any) -> int | None:
    """How many tests the behavioural stage ran, or ``None`` if there was none."""
    stage = _behavioural_stage(stages)
    return stage[1] if stage is not None else None


def _behavioural_stage(stages: Any) -> tuple[int, int, list[str]] | None:
    """``(passed, total, failed_ids)`` for the stage that measures behaviour."""
    by_stage: dict[str, tuple[int, int, list[str]]] = {}
    for entry in stages or []:
        if isinstance(entry, Mapping):
            name = str(entry.get("stage") or "")
            passed = int(entry.get("passed_units") or 0)
            total = int(entry.get("total_units") or 0)
            failed = [str(t) for t in (entry.get("failed_tests") or [])]
        else:
            name = str(getattr(entry, "stage", "") or "")
            passed = int(getattr(entry, "passed_units", 0) or 0)
            total = int(getattr(entry, "total_units", 0) or 0)
            failed = [str(t) for t in (getattr(entry, "failed_tests", None) or [])]
        if name:
            by_stage[name] = (passed, total, failed)
    for name in BEHAVIOUR_STAGES:
        if name in by_stage:
            return by_stage[name]
    return None
