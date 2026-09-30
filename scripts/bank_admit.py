#!/usr/bin/env python3
"""Admit finished task runs into the bank (self-evolution spec §6).

    uv run python scripts/bank_admit.py outputs/cpe_evolution/<batch>/<task> [...] \\
        [--success-rate 0.2] [--min-per-class 3] [--bank outputs/evolution/bank]

Only training tasks are admitted; the split file decides. Prints what was
admitted, what was skipped and why.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from orchestra.control.evolution.bank import AdmissionPolicy, Bank, entries_from_run
from orchestra.control.evolution.ledger import split_of


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+", type=Path, help="<batch>/<task> directories")
    ap.add_argument("--success-rate", type=float, default=0.2)
    ap.add_argument("--min-per-class", type=int, default=3)
    ap.add_argument("--bank", type=Path, default=None)
    args = ap.parse_args()
    bank = Bank(args.bank)
    policy = AdmissionPolicy(success_sample_rate=args.success_rate, min_per_class=args.min_per_class)
    for run in args.run_dirs:
        entries = entries_from_run(run)
        if not entries:
            print(f"{run}: no task_execution.json")
            continue
        split = split_of(entries[0].task_id)
        admitted = bank.admit(entries, policy)
        print(f"{run.name} ({split}): {len(admitted)}/{len(entries)} admitted "
              + ", ".join(f"{e.milestone_id}[{e.outcome}]" for e in admitted))
    print("bank classes:", dict(bank.class_counts()))


if __name__ == "__main__":
    main()
