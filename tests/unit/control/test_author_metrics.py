"""§6 metrics on hand-built milestones (author-evolution spec stage B4's metric test, written
with the metric module in B2)."""

from __future__ import annotations

from orchestra.control.author.metrics import (
    HeldoutCase,
    MilestoneInputs,
    SuiteCase,
    aggregate,
    milestone_metrics,
    stability,
)

F = frozenset


def _m(**kw) -> MilestoneInputs:
    base = dict(
        task="t", milestone="m", final="final",
        suite=[SuiteCase("s1", "hard", F({"Table.insert"})), SuiteCase("s2", "hard", F({"Table.search"})),
               SuiteCase("s3", "soft", F({"Table.get"})), SuiteCase("s4", "hard", F({"Query"}))],
        reference_failed=F({"s4"}),
        suite_results={"final": {"s1": "fail", "s2": "pass", "s3": "fail", "s4": "fail"},
                       "cand_a": {"s1": "pass", "s2": "pass", "s3": "pass", "s4": "fail"},
                       "cand_b": {"s1": "fail", "s2": "fail", "s3": "fail", "s4": "fail"}},
        heldout_cases=[HeldoutCase("h1", "documented", F({"Table.insert"}), inventory_miss=False),
                       HeldoutCase("h2", "documented", F({"Table.search"}), inventory_miss=True),
                       HeldoutCase("h3", "undocumented", F({"Table.__repr__"})),
                       HeldoutCase("h4", "documented", F({"Table.count"}))],
        heldout_results={"final": {"h1": "fail", "h2": "fail", "h3": "fail", "h4": "pass"},
                         "cand_a": {"h1": "pass", "h2": "fail", "h3": "fail", "h4": "pass"},
                         "cand_b": {"h1": "fail", "h2": "fail", "h3": "fail", "h4": "fail"}},
        inventory_hard_total=10, inventory_hard_covered=8, cost={"cases": 4, "rounds": 1}, audit_violations=0,
    )
    base.update(kw)
    return MilestoneInputs(**base)


def test_symbol_coverage_and_true_miss_use_hard_reference_passing_failures_only() -> None:
    r = milestone_metrics(_m())
    # F_doc on final = h1 (Table.insert), h2 (Table.search); S' failing on final = s1 (insert); s3 soft, s4 ref-failed
    assert r["f_doc"] == 2 and r["f_undoc"] == 1
    assert r["symbol_coverage"] == 0.5 and r["true_miss"] == 0.5
    assert r["uncovered_symbols"] == ["Table.search"]


def test_ceiling_and_reference_fail_rates() -> None:
    r = milestone_metrics(_m())
    assert r["ceiling_share"] == round(1 / 3, 4)
    assert r["ref_fail_rate_hard"] == round(1 / 3, 4)  # s4 of s1, s2, s4
    assert r["ref_fail_rate_soft"] == 0.0


def test_false_positive_when_every_heldout_case_on_the_symbol_passes() -> None:
    m = _m(heldout_results={"final": {"h1": "pass", "h2": "fail", "h3": "fail", "h4": "pass"}})
    r = milestone_metrics(m)
    assert r["false_positive_rate"] == 1.0 and r["false_positive_judged"] == 1
    assert milestone_metrics(_m())["false_positive_rate"] == 0.0


def test_discrimination_counts_sign_agreement_and_ties_as_half() -> None:
    r = milestone_metrics(_m())
    # held-out passes: final 1, cand_a 2, cand_b 0; S' passes (s1, s2): final 1, cand_a 2, cand_b 0
    assert r["discrimination"] == 1.0 and r["discrimination_pairs"] == 3
    tie = _m(suite_results={"final": {"s1": "pass", "s2": "pass"}, "cand_a": {"s1": "pass", "s2": "pass"},
                            "cand_b": {"s1": "pass", "s2": "pass"}})
    assert milestone_metrics(tie)["discrimination"] == 0.5


def test_inventory_metrics_and_undefined_values() -> None:
    r = milestone_metrics(_m())
    assert r["inventory_coverage_hard"] == 0.8
    assert r["inventory_gap_rate"] == 0.5  # h2 missed, h1 not
    none = milestone_metrics(_m(heldout_results={"final": {"h1": "pass", "h2": "pass", "h3": "pass", "h4": "pass"}},
                                inventory_hard_total=None))
    assert none["true_miss"] is None and none["ceiling_share"] is None and none["inventory_coverage_hard"] is None


def test_aggregate_with_and_without_a_task_and_stability() -> None:
    rows = [milestone_metrics(_m()), {**milestone_metrics(_m()), "task": "tinydb", "true_miss": 0.0}]
    both = aggregate(rows)
    without = aggregate(rows, exclude_tasks=["tinydb"])
    assert both["milestones"] == 2 and both["true_miss"] == 0.25
    assert without["milestones"] == 1 and without["true_miss"] == 0.5
    assert both["cost_cases"] == 8.0
    assert stability([0.5, 0.3]) == 0.1 and stability([0.5]) is None


def test_sentence_classes_split_failures_by_why_the_suite_missed_them() -> None:
    from orchestra.control.author.metrics import sentence_classes

    paras = ("insert returns the id of the new document", "remove deletes matching documents",
             "count returns the number of matching documents", "update sets the given fields")
    m = _m(
        suite=[SuiteCase("s1", "hard", F({"Table.insert"}), ("insert returns the id of the new document",)),
               SuiteCase("s2", "hard", F({"Table.remove"}), ("remove deletes matching documents",)),
               SuiteCase("s3", "hard", F({"Table.update"}), ("update sets the given fields",))],
        reference_failed=F(),
        suite_results={"final": {"s1": "fail", "s2": "pass", "s3": "pass"}},
        heldout_cases=[
            HeldoutCase("caught", "documented", F({"Table.insert"}), depth="detail_specified", paragraphs=(0,)),
            HeldoutCase("depth", "documented", F({"Table.remove"}), depth="detail_specified", paragraphs=(1,)),
            HeldoutCase("detail", "undocumented", F({"Table.update"}), depth="behaviour_only", paragraphs=(3,)),
            HeldoutCase("breadth", "documented", F({"Table.count"}), depth="detail_specified", paragraphs=(2,)),
            HeldoutCase("ceiling", "undocumented", F({"Table.__repr__"}), depth="not_specified"),
            HeldoutCase("unknown", "unknown", F(), depth="unknown"),
            HeldoutCase("passing", "documented", F({"Table.insert"}), depth="detail_specified", paragraphs=(0,)),
        ],
        heldout_results={"final": {"caught": "fail", "depth": "fail", "detail": "fail", "breadth": "fail",
                                   "ceiling": "fail", "unknown": "fail", "passing": "pass"}},
        doc_paragraphs=paras,
    )
    sc = sentence_classes(m)
    assert {k: v for k, v in sc.items()} == {"caught": ["caught"], "depth": ["depth"], "detail_ceiling": ["detail"],
                                              "breadth": ["breadth"], "ceiling": ["ceiling"], "unattributable": ["unknown"]}
    row = milestone_metrics(m)
    assert row["sentence_classes"]["depth"] == 1
    agg = aggregate([row, row])
    assert agg["sentence_classes"]["caught"] == 2 and agg["sentence_shares"]["breadth"] == round(1 / 6, 4)
