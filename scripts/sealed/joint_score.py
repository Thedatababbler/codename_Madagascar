"""Score one first run for the joint iteration (sealed: the held-out part).

Returns the gate suite's per-case results and failure messages, the verification suite's per-case
results on cases the reference passes, and the relevant held-out cases' pass/fail (case ids only).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_results as hr  # noqa: E402
from author_eval import ENV_ROOT, run_suite  # noqa: E402
from heldout_matrix import per_case, reference_failures  # noqa: E402


def score_first_run(task: str, mid: str, ws: Path, gate_suite: Path, verifier: Path | None) -> dict:
    py = ENV_ROOT / task / "bin" / "python"
    msgs: dict = {}
    gate = run_suite(ws, gate_suite, py, messages=msgs)
    out = {"gate": gate, "gate_messages": msgs}
    if verifier is not None:
        invalid = set(reference_failures(task, verifier))
        vm: dict = {}
        v = run_suite(ws, verifier, py, messages=vm)
        out["verifier"] = {k: x for k, x in v.items() if k not in invalid}
        out["verifier_messages"] = {k: m for k, m in vm.items() if k not in invalid}
    scope = f"rel:{mid}"
    if hr.attributed_cases(task, scope):
        out["heldout"] = per_case(hr.results_for(task, scope, str(ws)))
    else:
        out["heldout"] = {}
    return out
