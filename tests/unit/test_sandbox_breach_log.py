"""Breach log (user decision 2026-10-11): every event class, triggered by scripted commands inside the real
sandbox (no model), is recorded at the right level, in the run log and in the global index, and summarised."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestra.sandbox import breach as B
from orchestra.sandbox import policy as SB

ROOT = Path(__file__).resolve().parents[2]
VENV = "/root/codex-benchmarks/cpe_envs/tinydb"
NODE = "agent_1_author_implementer"

# (event the command must raise, shell command run inside the sandbox)
SCRIPTED = [
    ("read_denied", f"cat {ROOT}/memory/VERSION"),
    ("list_denied", "ls /root/projects"),
    ("list_denied", "find / -maxdepth 1 -name nothing_here"),
    ("network_denied", "python3 -c \"import socket; socket.create_connection(('151.101.0.223', 443), 3)\""),
    ("package_fetch", "pip download --no-deps -d /tmp/x requests"),
    ("package_fetch", "curl -s https://pypi.org/simple/"),
    ("ld_preload_tamper", "env -u LD_PRELOAD true"),
    ("sandbox_tamper", "export ADAMAS_SANDBOX_ENFORCED=0; echo off"),
]


def _confined_items(ws: Path, private: Path) -> list[dict]:
    pol = SB.agent_policy(workspace=ws, private=private, venv=VENV)
    pp = SB.write_policy(pol, private / "policy.json")
    env = SB.launcher_env(SB.private_env(private))
    items = []
    for _event, cmd in SCRIPTED:
        r = subprocess.run([*SB.launcher(pp), "/bin/bash", "-lc", cmd], cwd=ws, env=env, capture_output=True, text=True, timeout=60)
        items.append({"type": "commandExecution", "command": f"/bin/bash -lc {json.dumps(cmd)}",
                      "aggregated_output": r.stdout + r.stderr, "exit_code": r.returncode})
    return items, pol


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "INDEX", tmp_path / "index.jsonl")
    monkeypatch.setattr(B, "VOID", tmp_path / "void.jsonl")
    rd = tmp_path / "batch" / "task"
    ws = rd / "workspaces" / "m1" / "repo"
    ws.mkdir(parents=True)
    trace = rd / "tasks" / "rb_t" / "backend_traces" / NODE
    trace.mkdir(parents=True)
    private = trace / ".sandbox" / "req1"
    items, pol = _confined_items(ws, private)
    # an out-of-bounds read the sandbox did not stop (as if it had failed): the audit must call it CRITICAL
    items.append({"type": "commandExecution", "command": f"/bin/bash -lc 'cat {ROOT}/memory/VERSION'",
                  "aggregated_output": "7\n", "exit_code": 0})
    (trace / "req1.items.json").write_text(json.dumps(items))
    # a canary in a workspace file
    canary = "CANARY-AU-0123456789abcdef"
    (ws / "notes.txt").write_text(f"copied: {canary}\n")
    # two agent calls in the manifest: req1 captured, req2 with no items and no session log
    for rid in ("req1", "req2"):
        SB.write_manifest(rd, {"kind": "agent", "node_id": NODE, "request_id": rid, "private_dir": str(trace / ".sandbox" / rid),
                               "policy": pol.to_dict(), "workspace": str(ws)})
    return rd


def test_every_event_class_recorded(run):
    s = B.scan_run(run, run_id="test-run")
    events = [json.loads(x) for x in (run / B.LOG_NAME).read_text().splitlines()]
    got = {(e["event"], e["level"]) for e in events}
    for event, _cmd in SCRIPTED:
        assert (event, B.LEVEL[event]) in got, (event, sorted(got))
    for event in ("canary", "capture_empty", "unblocked_breach"):
        assert (event, "CRITICAL") in got, (event, sorted(got))
    for e in events:
        assert set(e) == {"time", "run_id", "role", "event", "level", "target", "command"}
        assert len(e["command"]) <= 200
        assert not e["target"].endswith((".py", ".yaml", "VERSION"))       # directory level only
    assert all(e["role"] in (NODE, "-") or e["role"].startswith(NODE) for e in events if e["event"] != "capture_empty")
    # the global index holds the same events
    index = [json.loads(x) for x in B.INDEX.read_text().splitlines()]
    assert len(index) == len(events)
    # CRITICAL voids the run
    assert s["void"] and (run / "SANDBOX_VOID").is_file() and B.is_void(run)


def test_report_section_format(run):
    s = B.scan_run(run, run_id="test-run")
    text = B.report_section([s])
    assert text.startswith("## 沙盒越界警告")
    assert "- 运行数：1；命令总数：" in text and "- WARNING：" in text and "- CRITICAL：" in text
    assert "test-run |" in text and "结论：有越界" in text


def test_clean_run_reports_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "INDEX", tmp_path / "index.jsonl")
    monkeypatch.setattr(B, "VOID", tmp_path / "void.jsonl")
    rd = tmp_path / "b" / "t"
    trace = rd / "tasks" / "x" / "backend_traces" / NODE
    trace.mkdir(parents=True)
    (trace / "r.items.json").write_text(json.dumps([{"type": "commandExecution", "command": "/bin/bash -lc 'pytest -q'",
                                                      "aggregated_output": "3 passed", "exit_code": 0}]))
    SB.write_manifest(rd, {"kind": "agent", "node_id": NODE, "request_id": "r", "private_dir": str(trace / ".sandbox" / "r"),
                           "policy": {"read": [], "write": [str(rd)]}})
    s = B.scan_run(rd, run_id="clean")
    assert s["warning"] == 0 and s["critical"] == 0 and not s["void"]
    assert "结论：无越界" in B.report_section([s])


def test_five_warnings_ask_for_review(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "INDEX", tmp_path / "index.jsonl")
    monkeypatch.setattr(B, "VOID", tmp_path / "void.jsonl")
    rd = tmp_path / "b" / "t"
    trace = rd / "tasks" / "x" / "backend_traces" / NODE
    trace.mkdir(parents=True)
    items = [{"type": "commandExecution", "command": f"/bin/bash -lc 'ls /root/projects/p{i}'",
              "aggregated_output": f"ls: cannot open directory '/root/projects/p{i}': Permission denied", "exit_code": 2} for i in range(5)]
    (trace / "r.items.json").write_text(json.dumps(items))
    SB.write_manifest(rd, {"kind": "agent", "node_id": NODE, "request_id": "r", "policy": {"read": [], "write": [str(rd)]}})
    s = B.scan_run(rd, run_id="noisy")
    assert s["warning"] >= 5 and s["manual_review"] and not s["void"]
    assert "需人工复核" in B.report_section([s])


def test_void_run_records_leave_the_ledger(tmp_path, monkeypatch):
    from orchestra.control.evolution.ledger import read_jsonl

    monkeypatch.setattr(B, "VOID", tmp_path / "void.jsonl")
    bad, good = tmp_path / "bad_run", tmp_path / "good_run"
    bad.mkdir()
    B.mark_void(bad, "bad", [])
    led = tmp_path / "candidates.jsonl"
    led.write_text(json.dumps({"run_dir": str(bad), "x": 1}) + "\n" + json.dumps({"run_dir": str(good), "x": 2}) + "\n")
    assert [r["x"] for r in read_jsonl(led)] == [2]
    assert [r["x"] for r in read_jsonl(led, include_void=True)] == [1, 2]
