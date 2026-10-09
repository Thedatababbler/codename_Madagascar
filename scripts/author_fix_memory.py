#!/usr/bin/env python3
"""Author fake-object fix §6.1: the author skill section and four human rules, written to memory/author/.

Every text passes the memory validator (format, training identifiers, sources,
held-out / verification substrings) before it is written; the write bumps the
memory version and snapshots it. Idempotent: rules already present are kept.

    uv run python scripts/author_fix_memory.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestra.memory.store import commit_write, load_config, memory_root, read_yaml_list  # noqa: E402
from orchestra.memory.validate import build_identifiers, substring_check, validate_entries  # noqa: E402

DATE = "2026-10-09"
SECTION_TITLE = "## 如何测试依赖外部世界的行为"
SECTION = SECTION_TITLE + """

When the behaviour under test talks to the outside world (network, other processes, the file
system outside the test's own directory, a user's home or configuration), keep the library the
program calls real and replace only the world it talks to with a real local one. A hand-written
stand-in for the library itself encodes your guess about its interface, and a correct
implementation that calls the real interface differently then fails.

Tools of `adamas_testkit` (importable at module top in gate and verification suites):
- a download or an HTTP API: `local_http_server(files)`, plain or streamed bodies, gives a base URL;
- cloning or checking out a repository: `local_git_repo(files, commits, branches, tags)` with real git, gives a path or `file://` URL;
- a protocol client (mail, chat, a custom wire format): `local_tcp_server(script)` answers a scripted dialogue;
- configuration or state under the user's home: `temp_home()`;
- an external command that must not really run: `fake_command(name, script)`, prepended to the original PATH and run by an absolute-path interpreter.

When none of these can stand in, build the double from the real object with
`unittest.mock.create_autospec(...)` or `Mock(spec=...)`, never by hand. Assert only what the
documents say is observable (return values, file contents, state, raised exceptions), not how the
program called its dependency, unless the documents prescribe the call.
"""

RULES = [
    {"rule_id": "AU-R-101", "category_id": "AU-MAIN_PATH",
     "trigger": "a behaviour uses the network, another process or the file system",
     "rule": "Do not replace network, process or file-system functions or classes of the standard library or of third-party libraries "
             "(requests, urllib, socket, subprocess, shutil, os I/O and the like); give the program a real local environment from "
             "adamas_testkit instead (local HTTP server, local git repository, local TCP server, temporary home, prepended fake command)."},
    {"rule_id": "AU-R-102", "category_id": "AU-MAIN_PATH",
     "trigger": "no local environment can stand in for a dependency",
     "rule": "Build the double with unittest.mock.create_autospec(<real object>) or Mock(spec=<real object>); never write a stand-in "
             "function or class by hand, and never put one into an object of the project."},
    {"rule_id": "AU-R-103", "category_id": "AU-MAIN_PATH",
     "trigger": "a test needs a fake external command or a changed environment variable",
     "rule": "Never replace PATH or any other environment variable as a whole; put the directory of the fake command in front of the "
             "original PATH and run the fake command with an absolute-path interpreter (adamas_testkit.fake_command does both)."},
    {"rule_id": "AU-R-104", "category_id": "AU-MAIN_PATH",
     "trigger": "a test involves a dependency the program calls",
     "rule": "Assert only results the documents describe as observable (return values, file contents, state changes, raised exceptions); "
             "do not assert how the program called its dependency (arguments, keywords, call counts) unless the documents prescribe the call."},
]


def main() -> int:
    cfg = load_config()
    root = memory_root(cfg)
    train = list(cfg["transfer"]["train_tasks"])
    tags = json.loads(json.dumps(__import__("yaml").safe_load((root / "domain_tags.yaml").read_text())))["tags"]
    rules = read_yaml_list(root / "author" / "rules.yaml")
    have = {r["rule_id"] for r in rules}
    new = [{**r, "state": "active", "origin": f"human:{DATE}", "evidence": {"source": "author fake-object fix spec 2026-10-09"}}
           for r in RULES if r["rule_id"] not in have]
    cats = [c["category_id"] for c in read_yaml_list(root / "author" / "categories.yaml")]
    verdicts = validate_entries("author", "rules", new, category_ids=cats, domain_tags=tags, identifiers=build_identifiers(train),
                                sources={}, test_tasks=list(cfg["transfer"]["test_tasks"]))
    skill_path = root / "author" / "SKILL.md"
    skill = skill_path.read_text(encoding="utf-8")
    skill_check = substring_check({"skill_section": SECTION})["skill_section"] if SECTION_TITLE not in skill else {"heldout": "PASS", "verification": "PASS"}
    report = {"rules": {v.entry_id: (v.ok, v.reasons) for v in verdicts}, "skill_section": skill_check}
    print(json.dumps(report, indent=1, ensure_ascii=False))
    if not all(v.ok for v in verdicts) or "FAIL" in skill_check.values():
        print("not written: a text failed validation")
        return 1
    if SECTION_TITLE not in skill:
        skill_path.write_text(skill.rstrip() + "\n\n" + SECTION, encoding="utf-8")
    if new:
        v = commit_write(root, "author", "rules", rules + new, reason="author fake-object fix §6.1: skill section + four human rules",
                         origin=f"human:{DATE}", changed_ids=[r["rule_id"] for r in new])
        print("memory version", v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
