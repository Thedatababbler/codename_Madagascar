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

from orchestra.schemas.artifacts import HarnessStageResult

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
