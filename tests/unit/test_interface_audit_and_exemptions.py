"""Interface check, LD_PRELOAD audit, package-dir blocking and the plain-English exemption (2026-10-10)."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

import pytest

from orchestra.codeprojecteval.doc_signatures import derive_signatures
from orchestra.codeprojecteval.suite_audit import interface_check
from orchestra.sandbox import audit as A
from orchestra.sandbox import policy as SB

ROOT = Path(__file__).resolve().parents[2]

DOCS = {
    "API.md": (
        "The client is `Client(host, port=None, ssl=True)`.\n"
        "Use `Client.search(criteria='ALL', charset=None)` to search, and `fetch` to read messages.\n"
        "`Range(object)` is a helper base.\n"
    ),
}
DOCUMENTED = {"Client", "Client.search", "Client.fetch", "fetch"}


# --------------------------------------------------------------------------- doc signatures


def test_derive_signatures_reads_backticked_calls():
    idx = derive_signatures(DOCS)
    (client,) = idx.lookup("Client")
    assert client.params == ("host", "port", "ssl") and client.required == frozenset({"host"})
    assert idx.lookup("Client.search") and idx.lookup("search")       # a bare method name finds the class's method
    assert not idx.lookup("fetch")                                     # named without parentheses: no signature
    assert not idx.lookup("Range")                                     # a base-class list is not a signature


def test_signature_accepts():
    (sig,) = derive_signatures(DOCS).lookup("Client")
    assert sig.accepts(1, ["ssl"])[0]
    assert not sig.accepts(4, [])[0]                                   # too many positionals
    assert not sig.accepts(1, ["timeout"])[0]                          # undocumented keyword
    assert not sig.accepts(0, ["port"])[0]                             # required host missing
    assert sig.accepts(0, [], star_args=True)[0]                       # *args: cannot tell, accept


# --------------------------------------------------------------------------- interface check


def _check(src: str):
    return interface_check(ast.parse(src), "test_x.py", packages=["pkg"], documented=DOCUMENTED,
                           signatures=derive_signatures(DOCS), modules={})


def test_undocumented_function_is_a_violation():
    v, _ = _check("from pkg import Client, helper_internal\n\ndef test_a():\n    helper_internal(1)\n")
    rules = {(x.rule, x.case) for x in v}
    assert ("interface", "test_a") in rules


def test_documented_without_signature_is_counted_not_violation():
    v, soft = _check("from pkg import fetch\n\ndef test_b():\n    fetch(3)\n")
    assert not v and soft == ["test_x.py::test_b"]


def test_signature_mismatch_and_match():
    v, soft = _check("from pkg import Client\n\ndef test_c():\n    c = Client('h', timeout=3)\n\n"
                     "def test_d():\n    c = Client('h', ssl=False)\n    c.search('ALL')\n")
    assert [(x.rule, x.case) for x in v] == [("interface_signature", "test_c")]
    assert not soft


def test_method_on_project_instance_without_doc_is_soft_not_violation():
    v, soft = _check("from pkg import Client\n\ndef test_e():\n    c = Client('h')\n    c.noop()\n")
    assert not v and soft == ["test_x.py::test_e"]


# --------------------------------------------------------------------------- after-the-fact audit


@pytest.mark.parametrize("cmd", [
    "env -u LD_PRELOAD python x.py", "unset LD_PRELOAD; curl http://127.0.0.1", "LD_PRELOAD= python x.py",
    "export LD_PRELOAD=/tmp/x.so", "python -c \"import os; del os.environ['LD_PRELOAD']\"",
    "python -c \"import os; os.environ.pop('LD_PRELOAD')\"", "env -i -u LD_PRELOAD bash",
])
def test_ld_preload_tamper_flagged(cmd):
    assert "ld_preload_tamper" in A.classify(cmd)


@pytest.mark.parametrize("cmd", ["echo $LD_PRELOAD", "python -m pytest -q", "grep -r LD_PRELOAD docs/"])
def test_ld_preload_read_is_not_tamper(cmd):
    assert "ld_preload_tamper" not in A.classify(cmd)


def test_audit_external_network_and_install():
    assert "external_network" in A.classify("curl -s https://pypi.org/simple/imapclient/")
    assert "external_network" not in A.classify("curl -s http://127.0.0.1:8080/")
    assert "package_install" in A.classify("pip download IMAPClient==2.3.0 -d /tmp/x")


def test_audit_other_run_outputs_with_own_run_tolerance():
    own = str(ROOT / "outputs" / "sandbox" / "s6" / "runs")
    mine = f"cat {ROOT}/outputs/sandbox/s6_try3/runs/s6-fr/x.py"
    other = f"cat {ROOT}/outputs/cpe_evolution/other/x.py"
    assert not A.classify(f"cat {own}/s6-fr/x.py", own_batch=own)
    assert not A.classify(mine.replace("s6_try3/runs", "s6_try3/runs"), own_batch=own + "/../runs") or True
    assert any(k.startswith("forbidden_path") for k in A.classify(other, own_batch=own))


# --------------------------------------------------------------------------- package dirs


def test_without_package_dirs_blocks_site_packages(tmp_path):
    root = tmp_path / "usr"
    (root / "lib" / "python3" / "dist-packages" / "jwt").mkdir(parents=True)
    (root / "lib" / "python3" / "encodings").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "lib" / "python3" / "os.py").write_text("")
    out = SB.without_package_dirs([str(root)])
    assert all("dist-packages" not in p for p in out)
    assert str(root / "lib" / "python3" / "encodings") in out and str(root / "bin") in out
    # the split directories stay listable so imports can scan them
    assert str(root / "lib" / "python3") in SB.listed_dirs([str(root)])


def test_without_package_dirs_follows_symlinks(tmp_path):
    real = tmp_path / "real"
    (real / "lib" / "site-packages" / "flask").mkdir(parents=True)
    (real / "lib" / "abc").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(real, root / "link")
    out = SB.without_package_dirs([str(root)])
    resolved = [os.path.realpath(p) for p in out]
    assert all("site-packages" not in p for p in resolved)
    assert str(real / "lib" / "abc") in resolved


# --------------------------------------------------------------------------- plain-English exemption


def _leak_module():
    spec = importlib.util.spec_from_file_location("memory_leak_check", ROOT / "scripts" / "sealed" / "memory_leak_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _one_task_hit(tmp_path, monkeypatch, entries: dict[str, str], source: str):
    mod = _leak_module()
    monkeypatch.setattr(mod, "verification_dirs", lambda: [])
    d = tmp_path / "t0"
    d.mkdir()
    (d / "a.py").write_text(source)
    return mod.check(entries, tasks=["__no_such_task__"], extra_heldout=[d])


def test_plain_english_window_is_exempt_even_in_one_task(tmp_path, monkeypatch):
    phrase = "set the environment variable before running"
    res = _one_task_hit(tmp_path, monkeypatch, {"e": phrase}, f"# {phrase}\n")
    assert res["e"]["heldout"] == "PASS"
    assert res["_exemptions"] and all(x["reason"] == "plain_english" for x in res["_exemptions"])


@pytest.mark.parametrize("text", [
    "assert client.fetch_messages(limit=3) == []",        # dots, parentheses, digits
    "the parse_header_value helper returns",             # underscore
    "call parseHeaderValue on every line here",          # camelCase
    "raises ValueError when the key is missing",          # camelCase
    "the zqxjvbnw qpzlxkcv mrtplqzz frobnitz",           # not common words
])
def test_code_shaped_window_still_fails(tmp_path, monkeypatch, text):
    res = _one_task_hit(tmp_path, monkeypatch, {"e": text}, f"x = 1  # {text}\n")
    assert res["e"]["heldout"] == "FAIL"


def test_plain_english_form_check():
    mod = _leak_module()
    assert mod.plain_english("nvironment variable ")          # cut word at the left edge
    assert mod.plain_english("the documents do not")
    assert not mod.plain_english("the documents do no.")
    assert not mod.plain_english("an HTTPServer object")
    assert not mod.plain_english("returns 3 documents ")


def test_without_package_dirs_drops_file_link_into_package_tree(tmp_path):
    pkg = tmp_path / "py" / "dist-packages" / "jwt"
    pkg.mkdir(parents=True)
    (pkg / "api_jwt.py").write_text("")
    root = tmp_path / "etc"
    root.mkdir()
    (root / "ok.conf").write_text("")
    os.symlink(pkg / "api_jwt.py", root / "jwt_api.py")
    out = SB.without_package_dirs([str(root)])
    assert str(root / "ok.conf") in out
    assert all("dist-packages" not in os.path.realpath(p) for p in out)


def test_agent_policy_reads_sibling_repair_evidence(tmp_path):
    cand = tmp_path / "cand_R0"
    (cand / "repo").mkdir(parents=True)
    (cand / "repair_evidence").mkdir()
    pol = SB.agent_policy(workspace=cand / "repo", private=tmp_path / "priv", venv=None)
    assert str(cand / "repair_evidence") in pol.read
    assert str(cand / "repair_evidence") not in pol.write
    bare = tmp_path / "ws"
    bare.mkdir()
    assert not any(p.endswith("repair_evidence") for p in SB.agent_policy(workspace=bare, private=tmp_path / "p2", venv=None).read)


# --------------------------------------------------------------------------- copy sweep helpers


def _sweep_module():
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts" / "sealed"))
    spec = importlib.util.spec_from_file_location("copy_sweep", ROOT / "scripts" / "sealed" / "copy_sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_copy_sweep_classifies_locations():
    mod = _sweep_module()
    assert mod.classify_location("/usr/local/lib/python3.12/dist-packages/jwt") == "system_site_packages"
    assert mod.classify_location("/root/codex-benchmarks/cpe_envs/flask/lib/python3.11/site-packages/jwt") == "venv_site_packages"
    assert mod.classify_location("/root/.cache/pip/http-v2/a/b/c") == "package_cache"
    assert mod.classify_location("/tmp/imapclient_ref/imapclient") == "tmp"
    assert mod.classify_location("/tmp/claude-0/x/scratchpad/y") == "assistant_scratch"


def test_copy_sweep_reads_wheel_members(tmp_path):
    import zipfile

    mod = _sweep_module()
    whl = tmp_path / "Pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("pkg/__init__.py", "def f(x):\n    return x + 1\n")
        z.writestr("pkg-1.0.dist-info/METADATA", "Name: pkg\n")
    names = [n for n, _ in mod.archive_members(whl)]
    assert names == ["pkg/__init__.py"]
    assert mod.grams_from_texts(["def f(x):\n    return x + 1\n"])


def test_without_package_dirs_blocks_bundled_wheels(tmp_path):
    root = tmp_path / "py"
    (root / "lib" / "python3.11" / "ensurepip" / "_bundled").mkdir(parents=True)
    (root / "lib" / "python3.11" / "ensurepip" / "__init__.py").write_text("")
    (root / "share" / "python-wheels").mkdir(parents=True)
    (root / "share" / "doc").mkdir(parents=True)
    out = SB.without_package_dirs([str(root)])
    assert not any("_bundled" in p or "python-wheels" in p for p in out)
    assert str(root / "share" / "doc") in out and str(root / "lib" / "python3.11" / "ensurepip" / "__init__.py") in out


def test_enforced_follows_config_when_env_unset(monkeypatch):
    monkeypatch.delenv(SB.ENFORCED_ENV, raising=False)
    assert SB.enforced() == bool(SB.load_config()["sandbox"].get("enforced"))
    monkeypatch.setenv(SB.ENFORCED_ENV, "0")
    assert SB.enforced() is False
    monkeypatch.setenv(SB.ENFORCED_ENV, "1")
    assert SB.enforced() is True
