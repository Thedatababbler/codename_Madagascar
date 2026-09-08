#!/usr/bin/env python
"""Convert one NL2Repo-Bench task into CodeProjectEval's on-disk shape.

NL2Repo ships a single `start.md` per task and keeps the upstream test suite
inside a Docker image the repository does not distribute. This writes, under
`--dataset-root/nl2_<task>/`: docs/PRD.md (introduction + instruction),
docs/architecture_design.md (environment, tree, API guide),
docs/directory_tree.txt, requirements.txt (the document's pins), the upstream
source at the document's source dir as the reference, unit_tests/ (the
upstream suite at the tag whose collected count is closest to the
document's), a minimal check_tests/, and config.json. It also builds the task
environment under `--env-root/nl2_<task>/` with the document's Python version.

Usage: nl2repo_to_cpe.py <task> [--url URL] [--tag TAG]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

BENCH = Path("/root/codex-benchmarks/nl2repo/NL2RepoBench/test_files")
UPSTREAM = Path("/root/codex-benchmarks/nl2repo/upstream")
DATASET_ROOT = Path("/root/codex-benchmarks/nl2repo/cpe_format")
ENV_ROOT = Path("/root/codex-benchmarks/nl2repo/envs")
URLS = Path("/root/projects/AdaMAS/outputs/nl2repo/upstream_urls.json")


def sections(md: str) -> list[tuple[str, str]]:
    """(heading, body) for every `## ` section, in order; heading '' for the preamble."""
    out: list[tuple[str, str]] = []
    title, buf = "", []
    for line in md.splitlines(keepends=True):
        if line.startswith("## "):
            out.append((title, "".join(buf)))
            title, buf = line[3:].strip(), [line]
        else:
            buf.append(line)
    out.append((title, "".join(buf)))
    return out


def python_version(md: str) -> str:
    m = re.search(r"Python version[^\n]*?(\d+\.\d+)(?:\.\d+)?", md)
    v = m.group(1) if m else "3.11"
    return "3.8" if v in ("3.6", "3.7") else v  # uv ships no 3.7 build


def pins(md: str) -> list[str]:
    """`name  version` lines in the dependency block -> name==version."""
    m = re.search(r"### [^\n]*[Dd]epend[^\n]*\n\s*```[^\n]*\n(.*?)```", md, re.S)
    reqs = []
    for line in (m.group(1) if m else "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        spec = re.match(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]*\])?)\s*([<>=!~]=?\s*[^\s,]+(?:\s*,\s*[<>=!~]=?\s*[^\s,]+)*)?", line)
        parts = line.split()
        if spec and spec.group(2):
            name, ver = spec.group(1), spec.group(2).replace(" ", "")
            req = f"{name}{ver}"
        elif len(parts) >= 2 and re.match(r"^[A-Za-z0-9_.\-\[\]]+$", parts[0]) and re.match(r"^\d", parts[1]):
            name, req = parts[0], f"{parts[0]}=={parts[1]}"
        else:
            continue
        if name.split("[")[0].lower() in ("pip", "setuptools", "wheel"):
            continue
        reqs.append(req)
    return reqs


def tree_block(md: str) -> str:
    m = re.search(r"### Project Directory Structure\s*```[^\n]*\n(.*?)```", md, re.S)
    if m:
        return m.group(1).strip("\n")
    m = re.search(r"### Project Directory Structure\s*\n(.*?)(?:\n### |\n## |\Z)", md, re.S)
    return (m.group(1) if m else "").strip("\n")


def tree_paths(tree: str) -> list[str]:
    """Reconstruct relative paths from a `├──`/`└──` tree."""
    paths, stack = [], []
    for raw in tree.splitlines():
        m = re.match(r"^((?:[│ ]   |\s{4})*)(?:├── |└── )?(.+?)\s*$", raw)
        if not m or not m.group(2) or "── " not in raw and m.group(1) == "" and not raw.strip():
            continue
        depth = len(m.group(1)) // 4
        name = re.split(r"\s+#|\s{2,}", m.group(2))[0].strip().rstrip("/")
        if not name:
            continue
        if "── " not in raw:
            # a bare root line such as "aiofiles/" or "." -- ignore as a path component
            if depth == 0:
                stack = []
                continue
        stack = stack[:depth] + [name]
        paths.append("/".join(stack))
    return paths


def source_dir(paths: list[str]) -> str:
    """Shallowest package dir (holds __init__.py), preferring src/<pkg>."""
    pkgs = sorted({p.rsplit("/", 1)[0] for p in paths if p.endswith("/__init__.py")}, key=lambda s: (s.count("/"), s))
    pkgs = [p for p in pkgs if not re.match(r"^(tests?|docs?|examples?|benchmarks?)(/|$)", p)]
    if not pkgs:
        return ""
    src = [p for p in pkgs if p.startswith("src/")]
    return (src or pkgs)[0]


def test_extras(up: Path) -> list[str]:
    """Dependency names from pyproject optional groups named test/tests/dev/testing."""
    try:
        import tomllib
        data = tomllib.loads((up / "pyproject.toml").read_text())
    except Exception:
        data = {}
    groups = dict((data.get("project") or {}).get("optional-dependencies") or {})
    for req in list(up.glob("*requirements*test*.txt")) + list(up.glob("*requirements*dev*.txt")) + list(up.glob("test-requirements.txt")):
        groups[req.name] = [l.strip() for l in req.read_text(errors="replace").splitlines() if l.strip() and not l.startswith(("#", "-"))]
    names: list[str] = []
    for key, deps in groups.items():
        if any(k in key.lower() for k in ("test", "dev")):
            for d in deps:
                name = re.split(r"[<>=!~;\[ ]", d.strip(), 1)[0]
                if name and name.lower() not in names:
                    names.append(name)
    return names


def ordered_tags(tags: list[str]) -> list[str]:
    """Release tags newest first, parsed as versions (a leading v/V ignored)."""
    from packaging.version import InvalidVersion, Version
    parsed = []
    for t in tags:
        core = t[1:] if t[:1] in "vV" and t[1:2].isdigit() else t
        try:
            v = Version(core)
        except InvalidVersion:
            continue
        if v.is_prerelease or v.is_devrelease:
            continue
        parsed.append((v, t))
    return [t for _, t in sorted(parsed, reverse=True)]


def find_tests_dir(up: Path) -> str | None:
    """A tests directory anywhere in the checkout (voluptuous keeps it in the package)."""
    hits = [p for p in up.rglob("*") if p.is_dir() and p.name in ("tests", "test") and ".git" not in p.parts and "venv" not in p.parts]
    hits = [p for p in hits if any(p.rglob("test*.py"))]
    hits.sort(key=lambda p: len(p.parts))
    return str(hits[0].relative_to(up)) if hits else None


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def collect_count(python: Path, repo: Path, tests_rel: str) -> int:
    r = run([str(python), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", tests_rel], cwd=repo)
    m = re.search(r"(\d+) tests? collected", r.stdout + r.stderr)
    return int(m.group(1)) if m else -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--url")
    ap.add_argument("--tag")
    ap.add_argument("--dataset-root", type=Path, default=DATASET_ROOT)
    ap.add_argument("--env-root", type=Path, default=ENV_ROOT)
    ap.add_argument("--max-tags", type=int, default=6)
    a = ap.parse_args()
    task = a.task
    src_dir = BENCH / task
    md = (src_dir / "start.md").read_text(encoding="utf-8", errors="replace")
    want = int((src_dir / "test_case_count.txt").read_text().strip() or 0)
    test_items = [t.strip("/") for t in json.loads((src_dir / "test_files.json").read_text())]
    tests_rel = test_items[0] if len(test_items) == 1 and "." not in Path(test_items[0]).name else str(Path(test_items[0]).parent)
    url = a.url or (json.load(open(URLS)).get(task) if URLS.exists() else None)
    if not url:
        print(f"no upstream url for {task}", file=sys.stderr); return 2
    tid = f"nl2_{task}"
    out = a.dataset_root / tid
    py = python_version(md); reqs = pins(md); tree = tree_block(md); paths = tree_paths(tree)
    sdir = source_dir(paths)
    print(f"[{task}] python {py}; {len(reqs)} pins; source_dir={sdir!r}; tests={tests_rel}; want {want} cases; url {url}")
    if not sdir:
        print("could not infer source dir from the document tree", file=sys.stderr); return 3
    # environment
    env = a.env_root / tid
    if not (env / "bin" / "python").exists():
        r = run(["uv", "venv", "-q", "--python", py, str(env)])
        if r.returncode: print(r.stderr[-500:], file=sys.stderr); return 4
    epy = env / "bin" / "python"
    if reqs:
        r = run(["uv", "pip", "install", "-q", "--python", str(epy), *reqs])
        if r.returncode:
            print("pin install failed; retrying without version pins", file=sys.stderr)
            run(["uv", "pip", "install", "-q", "--python", str(epy), *[x.split("==")[0] for x in reqs]])
    run(["uv", "pip", "install", "-q", "--python", str(epy), "pytest"])
    # upstream
    up = UPSTREAM / task
    if not up.exists():
        r = run(["git", "clone", "-q", url, str(up)])
        if r.returncode: print(r.stderr[-400:], file=sys.stderr); return 5
    tags = ordered_tags(run(["git", "tag"], cwd=up).stdout.split())
    cands = [a.tag] if a.tag else (tags[: a.max_tags] or ["HEAD"])
    best = None
    for tag in cands:
        run(["git", "checkout", "-q", "-f", tag], cwd=up)
        if not (up / tests_rel).exists():
            found = find_tests_dir(up)
            if not found:
                continue
            tests_rel = found
        run(["uv", "pip", "install", "-q", "--python", str(epy), "-e", str(up)])
        n = collect_count(epy, up, tests_rel)
        print(f"  tag {tag}: {n} collected")
        tol = max(3, want // 20)
        if n > 0 and (best is None or abs(n - want) + tol <= abs(best[1] - want)):
            best = (tag, n)  # nearer by more than the tolerance; ties keep the newer tag
        if n == want:
            break
    if best is None:
        print("no tag collected any tests", file=sys.stderr); return 6
    tag, n = best
    run(["git", "checkout", "-q", "-f", tag], cwd=up)
    run(["uv", "pip", "uninstall", "-q", "--python", str(epy), task], )
    # The upstream suite's own extras (aiohttp for aiofiles' server tests, ...):
    # the document pins the runtime, not what the tests need.
    extras = test_extras(up)
    if extras:
        run(["uv", "pip", "install", "-q", "--python", str(epy), *extras])
        print(f"  test extras: {extras}")
    print(f"  chosen {tag} ({n} vs document {want})")
    # lay out the task
    if out.exists():
        shutil.rmtree(out)
    (out / "docs").mkdir(parents=True)
    secs = sections(md)
    prd = "".join(b for t, b in secs if t == "" or t.startswith("Introduction") or t.startswith("Natural Language"))
    arch = "".join(b for t, b in secs if not (t == "" or t.startswith("Introduction") or t.startswith("Natural Language")))
    (out / "docs" / "PRD.md").write_text(prd)
    (out / "docs" / "architecture_design.md").write_text(arch)
    (out / "docs" / "directory_tree.txt").write_text(tree + "\n")
    (out / "requirements.txt").write_text("\n".join(reqs) + "\n")
    ref_src = up / sdir
    if not ref_src.is_dir():
        alt = [p for p in up.rglob("__init__.py") if p.parent.name == Path(sdir).name]
        if alt: ref_src = alt[0].parent
    shutil.copytree(ref_src, out / sdir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(up / tests_rel, out / "unit_tests", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if not (out / "unit_tests" / "__init__.py").exists():
        (out / "unit_tests" / "__init__.py").write_text("")
    for extra in ("conftest.py", "pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml"):
        if (up / extra).exists() and extra == "conftest.py":
            shutil.copy2(up / extra, out / "unit_tests" / "conftest_root.py")
    ct = out / "check_tests"; ct.mkdir()
    (ct / "__init__.py").write_text("")
    pkg = Path(sdir).name
    (ct / "test_smoke.py").write_text(f'import importlib\n\n\ndef test_package_imports():\n    assert importlib.import_module("{pkg}")\n')
    cfg = {"PRD": "docs/PRD.md", "UML": [], "dependencies": "requirements.txt", "architecture_design": "docs/architecture_design.md",
           "language": "python", "source_code": sdir, "unit_tests": "unit_tests", "check_tests": "check_tests", "usage_examples": "",
           "required_files": ["requirements.txt"], "unit_test_script": "pytest unit_tests", "check_test_script": "pytest check_tests",
           "nl2repo": {"task": task, "upstream": url, "tag": tag, "collected": n, "document_cases": want, "python": py, "tests_dir": tests_rel, "test_items": test_items}}
    (out / "config.json").write_text(json.dumps(cfg, indent=2))
    (out / "setup_shell_script.sh").write_text("pip install -r requirements.txt\n")
    print(f"  wrote {out}; env {env}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
