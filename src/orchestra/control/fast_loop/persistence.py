"""Persistent-failure diagnosis: what fails every time, not what failed once.

A single attempt's failure list mixes two populations. Some tests fail because
the code is wrong; some fail because this sample was unlucky and would pass on
a re-run. Best-of-n resampling harvests the second kind and cannot touch the
first (2026-08-31 anchor arm: four independent samples, five tests failed in
all four, two flipped). A candidate told to fix the whole list spends effort
on tests a re-roll would have handed it for free, and the evidence a role is
chosen on is contaminated by luck.

The intersection over several samples of the *same* design separates them.
This module computes it, applies the deterministic role floor that needs no
model, and scores what a diagnosis-driven candidate actually did about the
persistent set -- the ledger that decides whether the diagnoser keeps being
listened to.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    FailureDiagnosis,
)
from orchestra.roles.pool import RolePool

#: The fallback editing role when neither the rules nor the diagnoser name
#: one. It writes from the design documents, which is what a semantic misread
#: needs, and it is the pool's most general writer.
DEFAULT_EDITING_ROLE = "implementer"

#: Samples whose failure list describes a repository that passed its gate.
_SCORED = {CandidateStatus.VALID, CandidateStatus.COMMITTED, CandidateStatus.DISCARDED}


def failure_key(name: str) -> str:
    """``file.py::Class::test`` -- the identity of a test across workspaces.

    Harness names carry the path the suite was run from, which differs per
    candidate workspace; two samples naming the same test must intersect.
    """
    text = str(name or "").strip()
    return text.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class PersistenceSummary:
    samples: int
    #: Tests that failed in every scored sample, in the incumbent's spelling.
    persistent: list[str] = field(default_factory=list)
    #: Tests that failed in some samples and passed in others.
    flaky: list[str] = field(default_factory=list)
    sample_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "sample_ids": list(self.sample_ids),
            "persistent": list(self.persistent),
            "flaky": list(self.flaky),
        }


def persistent_failures(records: Iterable[CandidateRecord]) -> PersistenceSummary:
    """Intersect the failure lists of every scored sample.

    Only samples whose gate passed count: a candidate that broke the build
    reports failures about a repository nobody would ship, and intersecting
    with it would erase real defects behind that noise.
    """
    scored = [
        r
        for r in records
        if r.status in _SCORED and r.behaviour_score is not None
    ]
    if not scored:
        return PersistenceSummary(samples=0)
    by_key: list[dict[str, str]] = [
        {failure_key(n): n for n in (r.behaviour_failures or [])} for r in scored
    ]
    keys = [set(m) for m in by_key]
    inter = set.intersection(*keys)
    union = set.union(*keys)

    def spelling(key: str) -> str:
        for mapping in by_key:
            if key in mapping:
                return mapping[key]
        return key

    return PersistenceSummary(
        samples=len(scored),
        persistent=sorted(spelling(k) for k in inter),
        flaky=sorted(spelling(k) for k in union - inter),
        sample_ids=[r.candidate_id for r in scored],
    )


_SURFACE_TOKENS = (
    "import",
    "export",
    "reexport",
    "re_export",
    "public_surface",
    "module_file",
    "is_importable",
    "package_root",
)
_EDGE_TOKENS = (
    "empty",
    "boundary",
    "malformed",
    "invalid",
    "none_",
    "_none",
    "null",
    "overflow",
    "exhaust",
    "concurrent",
    "timeout",
    "truncat",
)


def rule_based_roles(
    persistent: list[str], furthest_stage: str
) -> tuple[str, str, str] | None:
    """``(editing_role, reviewer_role, reason)`` when the evidence is unambiguous.

    Conservative on purpose: a rule fires only when *every* persistent failure
    matches one category, so the model is consulted for exactly the residue no
    rule can name. Nothing here needs a prompt, so nothing here can hallucinate.
    """
    stage = str(furthest_stage or "").lower()
    if stage in {"compile", "imports"}:
        return ("dependency_resolver", "", "stage:imports")
    if not persistent:
        return None
    # Match on the test *function* name alone. The file and class ride along in
    # failure_key, and a suite file named test_..._public_surface.py made every
    # test it holds -- two config/oauth semantics tests included -- match the
    # surface rule at once (EXP-20260902-01). A token in the file name says
    # what the file is about, not what any one test needs.
    keys = [failure_key(n).lower().rsplit("::", 1)[-1] for n in persistent]
    if all(any(tok in k for tok in _SURFACE_TOKENS) for k in keys):
        return ("integrator", "contract_critic", "names:public_surface")
    if all(any(tok in k for tok in _EDGE_TOKENS) for k in keys):
        return ("edge_case_hardener", "", "names:edge_cases")
    return None


def apply_role_floor(diagnosis: FailureDiagnosis, pool: RolePool) -> FailureDiagnosis:
    """Fill ``recommended_role`` from the rules when they fire; else leave it."""
    if diagnosis.recommended_role:
        return diagnosis
    hit = rule_based_roles(list(diagnosis.behaviour_failures), diagnosis.furthest_stage)
    if hit is None:
        return diagnosis
    role, reviewer, reason = hit
    if pool.get(role) is None:
        return diagnosis
    return diagnosis.model_copy(
        update={
            "recommended_role": role,
            "recommended_reviewer": reviewer if pool.get(reviewer) else "",
            "role_source": f"rule:{reason}",
        }
    )


#: The default pair when nothing chose one, by what the search is for. A
#: quality search starts from work that passed its gate and scored poorly:
#: the residue no rule names is almost always a misread of the documented
#: behaviour, so the writer that works from the design documents, checked by
#: the reader that compares them to the code. A failure search has a failing
#: gate report as its evidence, and the pool has a role written for exactly
#: that report, checked by the reader of behavioural evidence.
DEFAULT_PAIRS: dict[str, tuple[str, str]] = {
    "quality": ("implementer", "spec_auditor"),
    "failure": ("gate_repairer", "behaviour_critic"),
}


def default_role(
    diagnosis: FailureDiagnosis, pool: RolePool, search_reason: str = "quality"
) -> FailureDiagnosis:
    """The last resort: a named pair, recorded as such, never silence.

    Only what is still empty is filled: a rule or the model may have named the
    writer and not the reviewer, and the pairing then completes it.
    """
    role, reviewer = DEFAULT_PAIRS.get(str(search_reason), DEFAULT_PAIRS["quality"])
    update: dict[str, str] = {}
    if not diagnosis.recommended_role and pool.get(role) is not None:
        update["recommended_role"] = role
        update["role_source"] = "default"
    if not diagnosis.recommended_reviewer and pool.get(reviewer) is not None:
        update["recommended_reviewer"] = reviewer
        update.setdefault("role_source", diagnosis.role_source or "default")
    return diagnosis.model_copy(update=update) if update else diagnosis


def persistence_ledger(record: CandidateRecord, persistent: list[str]) -> dict[str, object]:
    """What a diagnosis-driven candidate did about the persistent set.

    The diagnosis was a prediction -- "role R will move these" -- and this is
    its score. Aggregated per (class, role) over runs it decides which
    recommendations keep their slot; a role whose hit rate does not beat the
    anchor's on persistent failures is demoted, whatever the rationale said.
    """
    remaining = {failure_key(n) for n in (record.behaviour_failures or [])}
    fixed = [n for n in persistent if failure_key(n) not in remaining]
    return {
        "persistent_total": len(persistent),
        "persistent_fixed": fixed,
        "persistent_fixed_count": len(fixed),
        "scored": record.behaviour_score is not None,
    }


__all__ = [
    "DEFAULT_EDITING_ROLE",
    "DEFAULT_PAIRS",
    "PersistenceSummary",
    "apply_role_floor",
    "default_role",
    "failure_key",
    "persistence_ledger",
    "persistent_failures",
    "rule_based_roles",
]
