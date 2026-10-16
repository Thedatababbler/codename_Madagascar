#!/usr/bin/env python3
"""Collection fixes for converted NL2Repo hard tasks, written into the held-out conftest without showing it (sealed).

- ``alias``: the upstream suite imports itself as ``tests`` and its conftest as ``conftest`` (deepdiff); the converted
  suite lives in ``unit_tests``. Prepends a block that puts the suite dir on sys.path and aliases ``tests``.
- ``ignore_suite``: autopep8's ``suite/`` holds sample inputs named like tests; upstream never collects them.

    uv run python scripts/sealed/nl2_hard_fix.py alias nl2_deepdiff
    uv run python scripts/sealed/nl2_hard_fix.py ignore_suite nl2_autopep8
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guard as G  # noqa: E402

ALIAS = '''# --- AdaMAS (NL2Repo conversion, 2026-10-15): the suite imports itself as `tests` and its conftest as `conftest`
import importlib as _adamas_il
import pathlib as _adamas_pl
import sys as _adamas_sys

_adamas_here = _adamas_pl.Path(__file__).resolve().parent
if str(_adamas_here) not in _adamas_sys.path:
    _adamas_sys.path.insert(0, str(_adamas_here))
if str(_adamas_here.parent) not in _adamas_sys.path:
    _adamas_sys.path.insert(0, str(_adamas_here.parent))
_adamas_sys.modules.setdefault("tests", _adamas_il.import_module(_adamas_here.name))
# --- end AdaMAS
'''
CONFTEST_ALIAS = '''
# --- AdaMAS (NL2Repo conversion, 2026-10-15, b): `from conftest import ...` means the upstream root conftest,
# which the converter keeps as conftest_root.py beside this file; its fixtures are registered here too
import sys as _adamas_sys2
from conftest_root import *  # noqa: F401,F403
_adamas_sys2.modules["conftest"] = _adamas_sys2.modules["conftest_root"]
# --- end AdaMAS (b)
'''
IGNORE = '''
# --- AdaMAS (NL2Repo conversion, 2026-10-15): sample inputs, not tests (upstream does not collect them)
collect_ignore_glob = ["suite/*"]
# --- end AdaMAS
'''
MARK = "# --- AdaMAS (NL2Repo conversion, 2026-10-15)"


def main() -> int:
    what, task = sys.argv[1], sys.argv[2]
    conf = G.heldout_dir(task) / "conftest.py"
    text = conf.read_text(encoding="utf-8") if conf.is_file() else ""
    if what == "conftest_alias":
        import re as _re
        text = _re.sub(r"\n# --- AdaMAS \(NL2Repo conversion, 2026-10-15, b\).*?# --- end AdaMAS \(b\)\n", "\n", text, flags=_re.S)
        conf.write_text(text + CONFTEST_ALIAS, encoding="utf-8")
        print(task, what, "written")
        return 0
    if MARK in text:
        print(task, "already fixed")
        return 0
    if what == "alias":
        conf.write_text(ALIAS + "\n" + text, encoding="utf-8")
    elif what == "ignore_suite":
        conf.write_text(text + IGNORE, encoding="utf-8")
    else:
        raise SystemExit(f"unknown fix {what}")
    print(task, what, "written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
