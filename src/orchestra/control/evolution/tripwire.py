"""The sealed held-out tripwire (self-evolution spec §5.4 and §9.6).

One file, ``outputs/evolution/sealed/heldout_tripwire.jsonl``, written by
an independent process on training tasks only and read by exactly one
function, ``check_tripwire`` below. Nothing in the search, the evolver, the
selection or the commit logic imports this module's reader; a unit test
greps the source tree to keep it that way.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SEALED_ROOT = Path("outputs") / "evolution" / "sealed"  # historical default; see sealed_file()
SEALED_FILE = SEALED_ROOT / "heldout_tripwire.jsonl"


def sealed_file() -> Path:
    from orchestra.control.evolution.ledger import evolution_root

    return evolution_root() / "sealed" / "heldout_tripwire.jsonl"


def record_tripwire(records: Iterable[dict[str, Any]], path: Path | None = None) -> Path:
    """Append ``{record_id, heldout_attributed_subset_pass_rate}`` rows. Writer side only."""
    p = Path(path) if path else sealed_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as h:
        for r in records:
            h.write(json.dumps({"record_id": str(r["record_id"]),
                                "heldout_attributed_subset_pass_rate": float(r["heldout_attributed_subset_pass_rate"])}) + "\n")
    return p


def _read(path: Path | None) -> dict[str, float]:
    p = Path(path) if path else sealed_file()
    out: dict[str, float] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            out[str(d["record_id"])] = float(d["heldout_attributed_subset_pass_rate"])
        except (ValueError, KeyError, TypeError):
            continue
    return out


def check_tripwire(
    *,
    promoted_record_ids: Iterable[str],
    baseline_record_ids: Iterable[str],
    gate_net_fix: float,
    tolerance: float = 2.0,
    path: Path | None = None,
) -> tuple[bool, str]:
    """§9.6: fire when the gate says better (net fix > 0) but held-out says worse by more than ``tolerance`` points.

    Returns ``(fired, note)``. Missing held-out rows never fire: the check can
    only veto, not approve.
    """
    rates = _read(path)
    promoted = [rates[r] for r in promoted_record_ids if r in rates]
    baseline = [rates[r] for r in baseline_record_ids if r in rates]
    if not promoted or not baseline:
        return False, "no sealed held-out rows for these records"
    mean_p = sum(promoted) / len(promoted)
    mean_b = sum(baseline) / len(baseline)
    drop = (mean_b - mean_p) * 100.0
    if gate_net_fix > 0 and drop > tolerance:
        return True, f"gate net fix {gate_net_fix:+.2f} but held-out subset {drop:.1f} points lower ({len(promoted)} vs {len(baseline)} rows)"
    return False, f"held-out subset {-drop:+.1f} points ({len(promoted)} vs {len(baseline)} rows)"


__all__ = ["SEALED_FILE", "SEALED_ROOT", "check_tripwire", "record_tripwire"]
