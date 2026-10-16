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
PKG_ALIAS = '''# --- AdaMAS (NL2Repo conversion, 2026-10-16, c): the upstream suite lives inside the package and its conftest
# imports `{target}`; the converted suite is this directory, so `{target}` is aliased to it (an implementation built
# from the documents has no test helpers inside its package)
import importlib as _adamas_il3
import pathlib as _adamas_pl3
import sys as _adamas_sys3

_adamas_here3 = _adamas_pl3.Path(__file__).resolve().parent
if str(_adamas_here3.parent) not in _adamas_sys3.path:
    _adamas_sys3.path.insert(0, str(_adamas_here3.parent))
_adamas_pkg3 = _adamas_il3.import_module(_adamas_here3.name)
_adamas_sys3.modules["{target}"] = _adamas_pkg3
try:
    setattr(_adamas_il3.import_module("{parent}"), "{leaf}", _adamas_pkg3)
except Exception:  # an implementation without the parent package fails its own tests, not this alias
    pass
# --- end AdaMAS (c)
'''
SELF_ALIAS = '''
# --- AdaMAS (NL2Repo conversion, 2026-10-16, d): `{target}` is this conftest (other suite modules import from it)
import sys as _adamas_sys4
_adamas_sys4.modules["{target}"] = _adamas_sys4.modules[__name__]
# --- end AdaMAS (d)
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
    if what == "subpkg_alias":
        # upstream keeps its suites inside the package (cerberus/tests, cerberus/benchmarks) and imports them as
        # `cerberus.tests...`; the converted held-out directory is that package's copy. A meta-path finder loads
        # `<pkg>.<sub>...` straight from the held-out directory under that very name (no double import, no order issue)
        import re as _re
        pkg, subs = sys.argv[3], sys.argv[4].split(",")
        text = _re.sub(r"# --- AdaMAS \(NL2Repo conversion, 2026-10-16, f\).*?# --- end AdaMAS \(f\)\n\n?", "", text, flags=_re.S)
        block = f'''# --- AdaMAS (NL2Repo conversion, 2026-10-16, f): the suites are imported as {pkg}.<{",".join(subs)}>
import importlib.abc as _adamas_abc
import importlib.util as _adamas_util
import pathlib as _adamas_pl6
import sys as _adamas_sys6

_adamas_root6 = _adamas_pl6.Path(__file__).resolve().parent


class _AdamasSuiteFinder(_adamas_abc.MetaPathFinder):
    prefixes = tuple("{pkg}." + s for s in {subs!r})

    def find_spec(self, fullname, path=None, target=None):
        if not any(fullname == p or fullname.startswith(p + ".") for p in self.prefixes):
            return None
        rel = _adamas_pl6.Path(*fullname.split(".")[1:])
        pkg_dir = _adamas_root6 / rel
        if (pkg_dir / "__init__.py").is_file():
            return _adamas_util.spec_from_file_location(fullname, pkg_dir / "__init__.py",
                                                        submodule_search_locations=[str(pkg_dir)])
        mod = _adamas_root6 / rel.with_suffix(".py")
        if mod.is_file():
            return _adamas_util.spec_from_file_location(fullname, mod)
        return None


if not any(type(f).__name__ == "_AdamasSuiteFinder" for f in _adamas_sys6.meta_path):
    _adamas_sys6.meta_path.insert(0, _AdamasSuiteFinder())
# --- end AdaMAS (f)
'''
        conf.write_text(block + "\n" + text, encoding="utf-8")
        print(task, what, pkg, subs, "written")
        return 0
    if what == "restore":
        # put the converted suite's __init__.py and conftest.py back to the upstream originals (the converter's copy)
        import hashlib as _h
        import json as _j
        import shutil as _sh
        cfg = G.task_config(task)
        nl = cfg.get("nl2repo") or {}
        up = Path("/root/.adamas_sealed/work/upstream") / nl.get("task", task.removeprefix("nl2_")) / nl.get("tests_dir", "tests")
        done = {}
        for name in ("__init__.py", "conftest.py"):
            src, dst = up / name, G.heldout_dir(task) / name
            if src.is_file():
                _sh.copy2(src, dst)
                done[name] = _h.sha256(dst.read_bytes()).hexdigest()[:16]
            elif name == "__init__.py":
                dst.write_text("")
                done[name] = "empty"
        print(task, "restored", _j.dumps(done))
        return 0
    if what == "strip_cd":
        import re as _re
        text = _re.sub(r"# --- AdaMAS \(NL2Repo conversion, 2026-10-16, c\).*?# --- end AdaMAS \(c\)\n\n?", "", text, flags=_re.S)
        text = _re.sub(r"\n# --- AdaMAS \(NL2Repo conversion, 2026-10-16, d\).*?# --- end AdaMAS \(d\)\n", "", text, flags=_re.S)
        conf.write_text(text, encoding="utf-8")
        print(task, what, "done")
        return 0
    if what == "init_alias":
        target = sys.argv[3]
        init = G.heldout_dir(task) / "__init__.py"
        itext = init.read_text(encoding="utf-8") if init.is_file() else ""
        if "(e)" in itext:
            print(task, "already fixed (e)")
            return 0
        block = (f"# --- AdaMAS (NL2Repo conversion, 2026-10-16, e): the upstream suite is the package `{target}`;\n"
                 f"# the converted suite is this package, registered under that name before anything imports it\n"
                 f"import sys as _adamas_sys5\n_adamas_sys5.modules.setdefault(\"{target}\", _adamas_sys5.modules[__name__])\n"
                 f"# --- end AdaMAS (e)\n")
        init.write_text(block + itext, encoding="utf-8")
        print(task, what, target, "written")
        return 0
    if what == "pkg_alias":
        target = sys.argv[3]
        if "(c)" in text:
            print(task, "already fixed (c)")
            return 0
        parent, leaf = target.rsplit(".", 1)
        conf.write_text(PKG_ALIAS.format(target=target, parent=parent, leaf=leaf) + "\n" + text, encoding="utf-8")
        print(task, what, target, "written")
        return 0
    if what == "self_alias":
        target = sys.argv[3]
        if "(d)" in text:
            print(task, "already fixed (d)")
            return 0
        conf.write_text(text + SELF_ALIAS.format(target=target), encoding="utf-8")
        print(task, what, target, "written")
        return 0
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
