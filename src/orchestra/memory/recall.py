"""Recall (memory spec §3.1 step 2): program only, no model.

For the categories the judge selected, take the recallable entries (active or
trial) whose ``applies_when`` holds on the milestone, order them active first
then by ``evidence.fixes_observed`` (more first) then by id, and cut to the
configured caps.
"""

from __future__ import annotations

import operator
from collections.abc import Iterable, Mapping
from typing import Any

from orchestra.memory.store import RECALLABLE

_OPS = {"==": operator.eq, "!=": operator.ne, ">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}


def applies(entry: Mapping[str, Any], *, features: Mapping[str, Any], domain_tags: Iterable[str]) -> bool:
    aw = entry.get("applies_when") or {}
    tags = set(domain_tags)
    for key, want in aw.items():
        if key == "domain_tags":
            if want and not (set(want) & tags):
                return False
            continue
        have = features.get("position_group") if key == "position" else features.get(key)
        if isinstance(want, Mapping) and "op" in want:
            try:
                if not _OPS[str(want["op"])](have, want.get("value")):
                    return False
            except (TypeError, KeyError):
                return False
        elif isinstance(want, (list, tuple, set)):
            if want and have not in want:
                return False
        elif want is not None and have != want:
            return False
    return True


def _order(entry: Mapping[str, Any], id_field: str) -> tuple:
    fixes = int(((entry.get("evidence") or {}).get("fixes_observed")) or 0)
    return (0 if entry.get("state") == "active" else 1, -fixes, str(entry.get(id_field) or ""))


def recall(entries: Iterable[Mapping[str, Any]], *, categories: Iterable[str], features: Mapping[str, Any],
           domain_tags: Iterable[str], limit: int, id_field: str) -> list[dict[str, Any]]:
    cats = list(categories)
    tags = list(domain_tags)
    pool = [
        dict(e) for e in entries
        if e.get("state") in RECALLABLE and e.get("category_id") in cats and applies(e, features=features, domain_tags=tags)
    ]
    pool.sort(key=lambda e: _order(e, id_field))
    return pool[: max(0, int(limit))]


__all__ = ["applies", "recall"]
