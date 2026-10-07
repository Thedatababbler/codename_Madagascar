"""The joint experiment's foundation definition: early in the chain, types later milestones name."""

from orchestra.control.first_pass.features import foundation_strict

PLAN = [
    {"milestone_id": "a", "depends_on": [], "objective": "errors", "acceptance": {"criteria": []}},
    {"milestone_id": "b", "depends_on": ["a"], "objective": "core", "acceptance": {"criteria": ["uses Store"]}},
    {"milestone_id": "c", "depends_on": ["b"], "objective": "Raises StoreError and builds on Store and Query",
     "acceptance": {"criteria": ["MAX_SIZE honoured"]}},
    {"milestone_id": "d", "depends_on": ["c"], "objective": "integration uses Store, Query", "acceptance": {"criteria": []}},
]


def test_early_milestone_whose_types_later_milestones_name_is_a_foundation() -> None:
    ok, names = foundation_strict(PLAN, 0, owned_symbols={"Store", "StoreError", "MAX_SIZE", "helper"})
    assert ok and names == ["Store", "StoreError"]


def test_constants_do_not_count_and_one_reference_is_not_enough() -> None:
    assert foundation_strict(PLAN, 1, owned_symbols={"Query", "MAX_SIZE"}) == (False, ["Query"])


def test_third_in_the_chain_is_never_a_foundation() -> None:
    assert foundation_strict(PLAN, 2, owned_symbols={"Store", "Query"}) == (False, [])
