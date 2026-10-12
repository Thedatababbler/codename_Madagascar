"""Sandbox-only rule (user decision 2026-10-11): every entry point that starts an agent or runs workspace code
refuses to start without the sandbox, and nothing turns the sandbox off."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestra.sandbox import guard as G
from orchestra.sandbox import landlock as L
from orchestra.sandbox import policy as SB

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _real_rules(monkeypatch):
    """These tests check the real rule: the test suite's exemption is off and the readiness cache is cleared."""
    monkeypatch.delenv(SB.TEST_UNCONFINED_ENV, raising=False)
    G._READY.clear()
    yield
    G._READY.clear()


@pytest.fixture(params=["no_landlock_net", "config_off"])
def sandbox_unavailable(request, monkeypatch):
    if request.param == "no_landlock_net":
        monkeypatch.setattr(L, "abi_version", lambda: 3)
    else:
        real = SB.load_config
        monkeypatch.setattr(SB, "load_config", lambda *a, **k: {**real(), "sandbox": {"enforced": False}})
    return request.param


# --------------------------------------------------------------------------- every entry refuses


def test_require_sandbox_refuses(sandbox_unavailable):
    with pytest.raises(SB.SandboxRequiredError):
        G.require_sandbox("implementer")


def test_codex_session_refuses(sandbox_unavailable, tmp_path):
    from orchestra.backends import codex_sdk as C

    req = SimpleNamespace(node_id="m1:repairer", request_id="r", task_id="t")
    with pytest.raises(SB.SandboxRequiredError):
        C._sandboxed_config(req, SimpleNamespace(trace_dir=str(tmp_path / "trace")), str(tmp_path))


def test_codex_healthcheck_refuses(sandbox_unavailable):
    from orchestra.backends.codex_sdk import CodexSDKBackend

    health = asyncio.run(CodexSDKBackend().healthcheck())
    assert health.healthy is False


def test_smolagents_code_refused_always():
    from orchestra.backends.smolagents_code import SmolagentsCodeBackend

    health = asyncio.run(SmolagentsCodeBackend().healthcheck())
    assert health.healthy is False and "sandbox required" in health.detail


def test_workspace_code_refuses(sandbox_unavailable, tmp_path):
    # gates, repair evidence, resample frames, authored suites: confine_command / run_confined
    with pytest.raises(SB.SandboxRequiredError):
        SB.confine_command(["/usr/bin/python3", "-c", "1"], cwd=tmp_path, env={})
    with pytest.raises(SB.SandboxRequiredError):
        G.run_confined(["/usr/bin/python3", "-c", "1"], cwd=tmp_path)


def test_heldout_scoring_refuses(sandbox_unavailable, tmp_path):
    with pytest.raises(SB.SandboxRequiredError):
        SB.confine_scoring(["/usr/bin/python3", "-c", "1"], {})


def test_run_entry_refuses(sandbox_unavailable, tmp_path):
    from orchestra.cli import run_codeprojecteval_decomp as R

    with pytest.raises(SB.SandboxRequiredError):
        R._start_sandbox({}, run_dir=tmp_path, harness_dir=tmp_path, task_id="t", workspace=tmp_path)


def test_lcb_and_docker_executors_refuse():
    for mod in ("orchestra.sandbox.lcb_official", "orchestra.sandbox.docker"):
        src = Path(__import__(mod, fromlist=["x"]).__file__).read_text()
        assert "raise SandboxRequiredError" in src


def test_baseline_runners_require_the_sandbox():
    for f in ("runner.py", "runner_codex.py"):
        p = Path("/root/projects/mas-baselines") / f
        if not p.exists():
            pytest.skip("mas-baselines not present")
        src = p.read_text()
        assert "require_sandbox(" in src and "run_confined(" in src
        assert "subprocess.run(\n            [str(py), \"-m\", \"pytest\"" not in src
    assert "sandboxed_codex_config(" in (Path("/root/projects/mas-baselines") / "runner_codex.py").read_text()


def test_unconfined_launch_is_refused():
    with pytest.raises(SB.SandboxRequiredError):
        G.assert_confined_launch(["/usr/bin/python3", "-c", "1"], {}, role="implementer")


def test_launch_without_network_guard_is_refused(tmp_path):
    cmd, env, _ = SB.confine_command(["/usr/bin/python3", "-c", "1"], cwd=tmp_path, env={})
    env = dict(env)
    env.pop("LD_PRELOAD")
    with pytest.raises(SB.SandboxRequiredError):
        G.assert_confined_launch(cmd, env, role="gate")


# --------------------------------------------------------------------------- nothing turns it off


def test_env_and_config_cannot_turn_it_off(monkeypatch):
    for flag in ("0", "false", "off", ""):
        monkeypatch.setenv(SB.ENFORCED_ENV, flag)
        assert SB.enforced() is True
    with pytest.raises(SB.SandboxRequiredError):
        SB.load_config({"sandbox": {"enforced": False}})


def test_test_exemption_needs_pytest_and_a_running_test(monkeypatch):
    monkeypatch.setenv(SB.TEST_UNCONFINED_ENV, "1")
    assert SB.enforced() is False                       # inside this test
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert SB.enforced() is True                        # same process, outside a test
    out = subprocess.run([sys.executable, "-c", "from orchestra.sandbox.policy import enforced; print(enforced())"],
                         capture_output=True, text=True, env={**os.environ, SB.TEST_UNCONFINED_ENV: "1", "PYTEST_CURRENT_TEST": "x"})
    assert out.stdout.strip() == "True"                 # a child process is not pytest


# --------------------------------------------------------------------------- the launcher verifies itself


def test_launcher_refuses_when_canary_readable(tmp_path):
    # a policy that (wrongly) grants the repository: the launcher's own check must stop it
    pol = L.Policy(read=[*SB.system_reads(), str(ROOT)], write=[str(tmp_path)], connect_ports=SB.net_ports())
    pol_path = tmp_path / "bad.json"
    pol_path.write_text(json.dumps(pol.to_dict()))      # written directly: write_policy would refuse it first
    env = SB.launcher_env({"PATH": "/usr/bin:/bin", "LD_PRELOAD": str(SB.ensure_netguard())})
    r = subprocess.run([*SB.launcher(pol_path), "/usr/bin/true"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 126 and "refusing to run unconfined" in r.stderr


def test_launcher_refuses_without_network_guard(tmp_path):
    pol = L.Policy(read=SB.system_reads(), write=[str(tmp_path)], connect_ports=SB.net_ports())
    pol_path = SB.write_policy(pol, tmp_path / "p.json")
    r = subprocess.run([*SB.launcher(pol_path), "/usr/bin/true"], env=SB.launcher_env({"PATH": "/usr/bin:/bin"}),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 126 and "network guard" in r.stderr


def test_launcher_runs_when_confined(tmp_path):
    pol = L.Policy(read=SB.system_reads(), write=[str(tmp_path)], connect_ports=SB.net_ports())
    pol_path = SB.write_policy(pol, tmp_path / "p.json")
    env = SB.launcher_env({"PATH": "/usr/bin:/bin", "LD_PRELOAD": str(SB.ensure_netguard())})
    r = subprocess.run([*SB.launcher(pol_path), "/usr/bin/true"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- explicit deny list, upstream moved


@pytest.mark.parametrize("path", ["/venv/mas", "/root/projects/mas-baselines", "/root/projects/EvoMAS",
                                  "/root/codex-benchmarks/realbench_codex_vanilla", "/usr/local/lib/python3.12/dist-packages",
                                  "/root/.adamas_sealed", "/root"])
def test_policy_granting_a_denied_path_is_refused(path, tmp_path):
    with pytest.raises(SB.SandboxRequiredError):
        SB.write_policy(L.Policy(read=[path], write=[str(tmp_path)]), tmp_path / "p.json")


def test_nl2repo_upstream_moved():
    assert not Path("/root/codex-benchmarks/nl2repo/upstream").exists()
    archive = SB.SEALED_ARCHIVE_DIR / "nl2repo_upstream.tar.gz"
    assert archive.is_file() and (SB.SEALED_ARCHIVE_DIR / "nl2repo_upstream.tar.gz.sha256").is_file()


def test_edge_comparison_with_missing_value_is_false():
    from orchestra.ir.edges import EdgeCondition

    cond = EdgeCondition(source_field="behaviour_failed_count", operator="greater_than", value=0)
    assert cond.evaluate({"behaviour_failed_count": None}) is False
    assert cond.evaluate({"behaviour_failed_count": 2}) is True


def test_custody_collection_roots_pytest_at_the_target(tmp_path, monkeypatch):
    # sandboxed custody collects <tmp holder>/spec_tests: pytest must be rooted where that path lives
    src = __import__("orchestra.codeprojecteval.harness", fromlist=["x"])._check_script_source()
    ns: dict = {"__name__": "check_script_probe", "__file__": str(tmp_path / "adamas_cpe_check.py")}
    exec(compile(src, "adamas_cpe_check.py", "exec"), ns)
    monkeypatch.setenv("ADAMAS_SANDBOXED", "1")
    holder = tmp_path / "holder"
    (holder / "spec_tests").mkdir(parents=True)
    env = ns["_pytest_env"](tmp_path, holder, target=holder / "spec_tests")
    assert f"--rootdir={holder}" in env["PYTEST_ADDOPTS"]


def test_author_node_in_a_repair_candidate_is_author_bank():
    from orchestra.memory.runtime import node_bank

    assert node_bank(task_id="rb_x__m1__candidate__cand_feedback", role="Specification test author", expected=None,
                     node_id="agent_1_test_author_test_author") == "author"
    assert node_bank(task_id="rb_x__m1__candidate__cand_R0", role="Gate repairer", expected=None,
                     node_id="agent_1_improver_gate_repairer") == "repair"
    assert node_bank(task_id="rb_x__subtask__m1", role="Builder", expected=None, node_id="agent_2_builder_implementer") == "first_pass"


def test_strip_first_pass_keeps_ack_when_author_rules_remain():
    from orchestra.memory.assemble import ACK_REQUIREMENT, rules_block, strip_first_pass

    sys_msg = "You write tests.\n\n" + rules_block([{"rule_id": "AU-R-101", "rule": "use the testkit"}])
    msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": "Do it.\n\n" + ACK_REQUIREMENT}]
    out = strip_first_pass(msgs)
    assert "AU-R-101" in out[0]["content"] and ACK_REQUIREMENT in out[1]["content"]
    plain = strip_first_pass([{"role": "system", "content": "Build."}, {"role": "user", "content": "Go.\n\n" + ACK_REQUIREMENT}])
    assert ACK_REQUIREMENT not in plain[1]["content"]
