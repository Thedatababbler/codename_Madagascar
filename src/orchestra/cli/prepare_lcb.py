import argparse
import json
import os
from pathlib import Path

from orchestra.adapters.livecodebench.loader import LiveCodeBenchLoader
from orchestra.adapters.livecodebench.mapper import (
    load_manifest,
    sample_manifest,
    save_manifest,
)
from orchestra.settings import load_env_file

PINNED_COMMIT = "28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24"


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-version", default="release_v6")
    parser.add_argument("--output", "--manifest", dest="output", required=True)
    parser.add_argument("--data-dir", default=os.getenv("LCB_DATA_DIR"))
    parser.add_argument("--num-easy", type=int, default=5)
    parser.add_argument("--num-medium", type=int, default=5)
    parser.add_argument("--num-hard", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--exclusions", default="configs/manifests/excluded_ids.json"
    )
    parser.add_argument(
        "--exclude-manifest",
        action="append",
        default=[],
        help="Exclude IDs from an existing manifest; may be repeated.",
    )
    args = parser.parse_args()
    if not args.data_dir:
        parser.error("--data-dir or LCB_DATA_DIR is required")
    excluded_path = Path(args.exclusions)
    excluded = (
        set(json.loads(excluded_path.read_text()).get("question_ids", []))
        if excluded_path.exists()
        else set()
    )
    for manifest_path in args.exclude_manifest:
        excluded.update(
            entry.question_id for entry in load_manifest(manifest_path).entries
        )
    loader = LiveCodeBenchLoader(args.data_dir, args.release_version)
    tasks = loader.load()
    manifest = sample_manifest(
        tasks,
        release_version=args.release_version,
        seed=args.seed,
        counts={
            "easy": args.num_easy,
            "medium": args.num_medium,
            "hard": args.num_hard,
        },
        excluded_ids=excluded,
        livecodebench_commit=PINNED_COMMIT,
    )
    save_manifest(manifest, args.output)
    print(f"Wrote {len(manifest.entries)} tasks to {args.output}")
    print(f"manifest_sha256={manifest.manifest_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
