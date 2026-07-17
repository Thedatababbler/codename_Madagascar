"""Manual M4 smoke (workflow_dispatch / local). Does not run in ordinary CI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchestra.settings import load_env_file


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="M4 Fast Loop smoke (trusted fixture)")
    parser.add_argument(
        "--backend",
        choices=("codex_sdk", "smolagents_code"),
        required=True,
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config + print planned path without calling real APIs",
    )
    args = parser.parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"config not found: {config_path}")

    # Real API smokes reuse the M3.5 Codex / CodeAgent runners when credentials
    # exist. Ordinary CI must not call this module.
    print(
        json.dumps(
            {
                "milestone": "M4",
                "backend": args.backend,
                "config": str(config_path),
                "dry_run": args.dry_run,
                "note": (
                    "M4 controller is covered by deterministic integration tests. "
                    "For live backend execution smoke, use run_codex_smoke / "
                    "run_bbeh with a tiny trusted fixture and small budget. "
                    "Do not fabricate retry success for paper results."
                ),
                "docs": "docs/m4_fast_local_adaptation.md",
            },
            indent=2,
        )
    )
    if args.dry_run:
        return 0

    if args.backend == "codex_sdk":
        # Delegate to existing Codex smoke entrypoint when present.
        import sys

        from orchestra.cli import run_codex_smoke

        sys.argv = ["run_codex_smoke", "--config", str(config_path)]
        return int(run_codex_smoke.main())

    print(
        "smolagents_code live M4 smoke: run deterministic controller integration "
        "tests plus optional BBEH CodeAgent smoke; full fail→retry live demos "
        "are not required for M4 acceptance."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
