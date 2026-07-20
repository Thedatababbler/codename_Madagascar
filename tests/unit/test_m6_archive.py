"""M6 Pareto archive unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.control.pareto.archive import ParetoArchive
from orchestra.control.pareto.persistence import ParetoPersistence
from orchestra.control.pareto.schemas import (
    ObjectiveDirection,
    ObjectiveSource,
    ObjectiveValue,
    ParetoConfig,
    ParetoEvaluationKind,
    ParetoObjectiveVector,
    ParetoOrchestraCandidate,
)


def _cand(cid: str, ctx: str = "ctx", **kwargs) -> ParetoOrchestraCandidate:
    values = {
        name: ObjectiveValue(
            value=float(v),
            source=ObjectiveSource.ESTIMATED,
            available=True,
        )
        for name, v in kwargs.items()
    }
    return ParetoOrchestraCandidate(
        candidate_id=cid,
        content_hash=cid,
        edit_signature=cid,
        context_id=ctx,
        global_candidate=None,
        objectives=ParetoObjectiveVector(
            values=values, evaluation_kind=ParetoEvaluationKind.ESTIMATED
        ),
    )


def test_archive_deduplicates_content_hash():
    archive = ParetoArchive()
    c = _cand("a", quality=1.0, cost=1.0, latency=1.0, risk=1.0)
    assert archive.insert(c) is True
    assert archive.insert(c) is False
    assert len(archive.frontier("ctx")) == 1


def test_archive_is_context_local():
    archive = ParetoArchive()
    archive.insert(_cand("a", "c1", quality=1.0, cost=1.0, latency=1.0, risk=0.0))
    archive.insert(_cand("b", "c2", quality=0.1, cost=9.0, latency=9.0, risk=9.0))
    assert len(archive.by_context("c1")) == 1
    assert len(archive.by_context("c2")) == 1


def test_archive_removes_dominated_points():
    archive = ParetoArchive()
    archive.insert(
        _cand(
            "dom",
            quality=1.0,
            cost=1.0,
            latency=1.0,
            risk=0.0,
            communication_overhead=0.0,
        )
    )
    archive.insert(
        _cand(
            "weak",
            quality=0.1,
            cost=2.0,
            latency=2.0,
            risk=1.0,
            communication_overhead=1.0,
        )
    )
    hashes = {c.content_hash for c in archive.frontier("ctx")}
    assert "dom" in hashes
    assert "weak" not in hashes


def test_archive_preserves_extreme_points():
    cfg = ParetoConfig(
        max_estimated_archive_size=2,
        objectives={
            "quality": ObjectiveDirection.MAXIMIZE,
            "cost": ObjectiveDirection.MINIMIZE,
        },
    )
    archive = ParetoArchive(cfg)
    archive.insert(_cand("hq", quality=1.0, cost=10.0))
    archive.insert(_cand("lc", quality=0.1, cost=1.0))
    archive.insert(_cand("mid", quality=0.5, cost=5.0))
    hashes = {c.content_hash for c in archive.frontier("ctx")}
    assert "hq" in hashes
    assert "lc" in hashes


def test_archive_pruning_is_deterministic():
    cfg = ParetoConfig(max_estimated_archive_size=2)
    a1 = ParetoArchive(cfg)
    a2 = ParetoArchive(cfg)
    for cid, q, c in [("z", 0.9, 3.0), ("a", 0.8, 2.0), ("m", 0.7, 2.5)]:
        a1.insert(_cand(cid, quality=q, cost=c, latency=1.0, risk=0.0))
        a2.insert(_cand(cid, quality=q, cost=c, latency=1.0, risk=0.0))
    assert [c.content_hash for c in a1.frontier("ctx")] == [
        c.content_hash for c in a2.frontier("ctx")
    ]


def test_archive_checkpoint_resume(tmp_path: Path):
    archive = ParetoArchive()
    archive.insert(_cand("a", quality=1.0, cost=1.0, latency=1.0, risk=0.0))
    pers = ParetoPersistence(tmp_path)
    pers.save_estimated_archive(archive)
    loaded = pers.load_estimated_archive(ParetoConfig())
    assert len(loaded.frontier("ctx")) == 1


def test_archive_atomic_snapshot_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import os

    archive = ParetoArchive()
    archive.insert(_cand("a", quality=1.0, cost=1.0, latency=1.0, risk=0.0))
    pers = ParetoPersistence(tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        pers.save_estimated_archive(archive)
