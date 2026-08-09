#!/usr/bin/env python3
"""Provision and probe per-repository environments for CodeProjectEval.

A milestone gate is only worth building on tests that pass against the dataset's
own reference implementation. Anything failing here is an environment gap, not a
signal about generated code, so this script provisions one virtualenv per
repository and records which repositories are usable.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

DATASET = Path(
    "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset"
)
ENV_ROOT = Path("/root/codex-benchmarks/cpe_envs")
# Repositories ship stray build artefacts (a broken venv in voluptuous/bin).
IGNORED = shutil.ignore_patterns(
    "bin", ".git", ".venv", "venv", "*.egg-info", "__pycache__", "lib", "lib64",
    "pyvenv.cfg", "include", "share",
)


def _copy_repo(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=IGNORED, ignore_dangling_symlinks=True)


def _provision(repo_src: Path, env_dir: Path, *, timeout: int) -> dict:
    """Create a venv holding the repository's own third-party requirements."""
    if env_dir.exists():
        shutil.rmtree(env_dir)
    steps = [
        ["uv", "venv", "--python", "3.11", str(env_dir)],
    ]
    for cmd in steps:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return {"status": "venv_failed", "detail": (proc.stderr or "")[-500:]}

    python = env_dir / "bin" / "python"
    install = ["uv", "pip", "install", "--python", str(python), "pytest"]
    req = repo_src / "requirements.txt"
    if req.is_file():
        install += ["-r", str(req)]
    try:
        proc = subprocess.run(
            install, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return {"status": "install_timeout"}
    if proc.returncode != 0:
        # Requirements pinned for an older interpreter still leave a usable env
        # for repositories whose imports are satisfied by the base install.
        fallback = subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "pytest"],
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "status": "install_partial" if fallback.returncode == 0 else "install_failed",
            "detail": (proc.stderr or "")[-500:],
        }
    return {"status": "ok"}


def _run_pytest(python: Path, repo: Path, target: str, timeout: int) -> dict:
    if not (repo / target).exists():
        return {"status": "absent"}
    try:
        proc = subprocess.run(
            [
                str(python), "-m", "pytest", target, "-q", "--no-header",
                "-p", "no:cacheprovider",
                # Repositories bolt coverage thresholds, mypy and pycodestyle
                # onto pytest; those judge style, not whether the code works.
                "-o", "addopts=",
            ],
            cwd=repo,
            env={
                "PYTHONPATH": str(repo),
                "PATH": f"{python.parent}:/usr/bin:/bin:/usr/local/bin",
                "HOME": str(repo),
            },
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    tail = (proc.stdout or "")[-6000:]
    counts = {}
    for token in ("passed", "failed", "error"):
        match = re.search(rf"(\d+) {token}", tail)
        counts[token] = int(match.group(1)) if match else 0
    return {
        "status": "ok" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errors": counts["error"],
        "tail": tail[-800:] if proc.returncode != 0 else "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--env-root", type=Path, default=ENV_ROOT)
    ap.add_argument("--work-root", type=Path, default=Path("/tmp/cpe_probe_repos"))
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--only", type=str, default="")
    ap.add_argument("--out", type=Path, default=Path("outputs/cpe_env_probe.json"))
    args = ap.parse_args()

    args.env_root.mkdir(parents=True, exist_ok=True)
    if args.work_root.exists():
        shutil.rmtree(args.work_root)
    args.work_root.mkdir(parents=True)

    wanted = {n.strip() for n in args.only.split(",") if n.strip()}
    results: dict[str, dict] = {}
    for repo_src in sorted(p for p in args.dataset.iterdir() if p.is_dir()):
        name = repo_src.name
        if wanted and name not in wanted:
            continue
        repo = args.work_root / name
        _copy_repo(repo_src, repo)
        env_dir = args.env_root / name
        provision = _provision(repo_src, env_dir, timeout=args.timeout)
        python = env_dir / "bin" / "python"
        if not python.is_file():
            results[name] = {"provision": provision}
            print(f"{name:32s} provision={provision['status']}", flush=True)
            continue
        check = _run_pytest(python, repo, "check_tests", args.timeout)
        unit = _run_pytest(python, repo, "unit_tests", args.timeout)
        results[name] = {
            "provision": provision,
            "env_python": str(python),
            "check_tests": check,
            "unit_tests": unit,
        }
        print(
            f"{name:32s} env={provision['status']:16s} "
            f"check={check.get('status'):8s}"
            f"({check.get('passed', 0)}p/{check.get('failed', 0)}f/{check.get('errors', 0)}e) "
            f"unit={unit.get('status'):8s}"
            f"({unit.get('passed', 0)}p/{unit.get('failed', 0)}f/{unit.get('errors', 0)}e)",
            flush=True,
        )

    usable = [
        n
        for n, r in results.items()
        if r.get("check_tests", {}).get("status") == "ok"
        and r.get("unit_tests", {}).get("status") == "ok"
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"results": results, "usable": usable}, indent=2), encoding="utf-8"
    )
    print(f"\nusable={len(usable)}/{len(results)}: {', '.join(usable)}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
