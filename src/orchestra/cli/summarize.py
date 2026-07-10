import argparse
import json

from orchestra.telemetry.summary import summarize_run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    print(json.dumps(summarize_run(args.run_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
