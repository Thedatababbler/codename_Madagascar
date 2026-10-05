"""The author node's review step (author-evolution spec §2.4-2.6), called by the agent
executor right after a ``test_author`` call when the inventory feature is on.

Off -- ``ADAMAS_AUTHOR_INVENTORY_DIR`` unset, or no contract record for this contract --
``author_review_dir`` returns None and the executor does exactly what it did before.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestra.control.author.assemble import (
    DOCS_DIR_ENV,
    HARD_MIN_ENV,
    INVENTORY_DIR_ENV,
    MAX_ROUNDS_ENV,
    PACKAGES_ENV,
    PLAN_FILE_ENV,
)

FIX_HEADER = (
    "Your suite under `spec_tests/` is not finished. Edit only `spec_tests/`. Fix exactly the items below and "
    "keep every other test as it is.\n\n"
)


def author_review_dir(contract_id: str | None) -> Path | None:
    inv_dir = (os.environ.get(INVENTORY_DIR_ENV) or "").strip()
    if not inv_dir or not contract_id:
        return None
    rec = Path(inv_dir) / "contracts" / f"{contract_id}.json"
    return Path(inv_dir) if rec.is_file() else None


def lenient_symbols(docs: dict[str, str], plan_file: str, milestone_id: str, focus: list[str]) -> set[str] | None:
    """Own + every earlier milestone in plan order + unattributed (the user's audit set)."""
    from orchestra.codeprojecteval.public_symbols import derive_public_symbols

    inv = derive_public_symbols(docs)
    pred: list[str] = []
    if plan_file and Path(plan_file).is_file():
        try:
            plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
            order = [m["milestone_id"] for m in plan.get("milestones") or []]
            idx = order.index(milestone_id) if milestone_id in order else len(order)
            by = {m["milestone_id"]: m for m in plan["milestones"]}
            pred = [fp for mid in order[:idx] for fp in (by[mid].get("focus_paths") or [])]
        except (ValueError, KeyError, json.JSONDecodeError):
            pred = []
    return inv.lenient(focus, pred)


def _add_usage(total: Any, extra: Any) -> Any:
    try:
        return total.model_copy(update={
            "prompt_tokens": total.prompt_tokens + extra.prompt_tokens,
            "completion_tokens": total.completion_tokens + extra.completion_tokens,
            "cached_tokens": total.cached_tokens + extra.cached_tokens,
            "total_tokens": total.total_tokens + extra.total_tokens,
            "estimated_cost_usd": (None if total.estimated_cost_usd is None and extra.estimated_cost_usd is None
                                   else (total.estimated_cost_usd or 0.0) + (extra.estimated_cost_usd or 0.0)),
        })
    except AttributeError:
        return total


async def run_author_review(*, review_dir: Path, request: Any, backend: Any, backend_context: Any, first: Any,
                            semaphore: Any) -> tuple[Any, dict]:
    from orchestra.backends.base import AgentRunStatus
    from orchestra.codeprojecteval.behaviour_inventory import load_inventory
    from orchestra.codeprojecteval.public_symbols import load_docs
    from orchestra.control.author.review import review_and_fix

    meta = json.loads((review_dir / "contracts" / f"{request.contract_id}.json").read_text(encoding="utf-8"))
    mid = meta["milestone_id"]
    inv_path = review_dir / f"{mid}.inventory.json"
    if not inv_path.is_file():
        return first, {"skipped": "no inventory", "milestone_id": mid}
    inventory = load_inventory(inv_path)
    docs = load_docs(os.environ.get(DOCS_DIR_ENV) or "")
    packages = [p for p in (os.environ.get(PACKAGES_ENV) or "").split(",") if p]
    allowed = lenient_symbols(docs, os.environ.get(PLAN_FILE_ENV) or "", mid, list(meta.get("focus_paths") or []))
    hard_min = float(os.environ.get(HARD_MIN_ENV) or 1.0)
    max_rounds = int(os.environ.get(MAX_ROUNDS_ENV) or 2)
    state = {"result": first, "usage": first.usage, "fix_usage": []}
    tag = request.request_id[:8]

    async def run_fix(text: str) -> bool:
        fix_request = request.model_copy(update={
            "request_id": str(uuid4()),
            "messages": list(request.messages or []) + [{"role": "user", "content": FIX_HEADER + text}],
            "rendered_context": FIX_HEADER + text,
        })
        (review_dir / mid).mkdir(parents=True, exist_ok=True)
        (review_dir / mid / f"{tag}.fix{len(state['fix_usage']) + 1}.prompt.txt").write_text(FIX_HEADER + text, encoding="utf-8")
        async with semaphore:
            res = await backend.run(fix_request, backend_context)
        state["fix_usage"].append(res.usage.model_dump() if hasattr(res.usage, "model_dump") else {})
        state["usage"] = _add_usage(state["usage"], res.usage)
        if res.status is AgentRunStatus.SUCCESS and res.output_artifacts:
            state["result"] = res
            return True
        return False

    rec = await review_and_fix(
        workspace=backend_context.workspace_ref, milestone_id=mid, docs=docs, packages=packages, allowed_symbols=allowed,
        inventory=inventory, hard_min=hard_min, max_rounds=max_rounds, run_fix=run_fix,
        soft_dest=review_dir / mid / f"{tag}.spec_tests_soft", record_path=review_dir / mid / f"{tag}.review.json",
    )
    result = state["result"]
    if state["fix_usage"]:
        result = result.model_copy(update={"usage": state["usage"]})
    summary = {"milestone_id": mid, "met": rec.met, "fix_calls": rec.fix_calls, "hard_covered": rec.hard_covered,
               "hard_total": rec.hard_total, "soft_cases": len(rec.soft_cases), "soft_dest": rec.soft_dest,
               "violations_final": len(rec.violations_final), "record": str(review_dir / mid / f"{tag}.review.json")}
    return result, summary


__all__ = ["FIX_HEADER", "author_review_dir", "lenient_symbols", "run_author_review"]
