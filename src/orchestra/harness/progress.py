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
from typing import Any

from orchestra.schemas.artifacts import (
    HarnessStageResult,
    RepositoryHarnessResultArtifact,
)

MARKER = "ADAMAS_HARNESS_SCORE "


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
