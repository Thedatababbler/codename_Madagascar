"""T0 of the memory spec (2026-10-09): formats, validator, recall, assembly, delivery checks, memory-off identity."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import orchestra.control  # noqa: F401  -- package import order (executors <-> control)
import orchestra.control.fast_loop.plan_candidates  # noqa: F401  -- and (subgraph_builder <-> plan_candidates)
from orchestra.memory import assemble as A
from orchestra.memory import judge as J
from orchestra.memory import runtime as R
from orchestra.memory import store as S
from orchestra.memory import validate as V
from orchestra.memory.recall import applies, recall

CATS_FP = [
    {"category_id": "FP-TYPE", "name": "types", "definition": "return element types", "signals": ["protocol client"], "state": "active"},
    {"category_id": "FP-DOC-EXC", "name": "exceptions", "definition": "documented exception class", "signals": [], "state": "active"},
    {"category_id": "FP-OLD", "name": "old", "definition": "candidate category", "signals": [], "state": "candidate"},
]


def pit(pid, cat="FP-TYPE", state="trial", fixes=1, **aw):
    return {"pitfall_id": pid, "category_id": cat, "symptom": "s", "cause": "c", "lesson": f"lesson {pid}",
            "applies_when": aw, "evidence": {"source_records": [], "fixes_observed": fixes, "milestones_observed": 1}, "state": state}


def make_store(tmp_path: Path, *, pitfalls=None, patterns=None, repair=None, rules=None) -> Path:
    root = tmp_path / "memory"
    S.write_yaml(root / "domain_tags.yaml", {"tags": ["network_protocol", "cli_tool", "storage"]})
    S.write_yaml(root / "first_pass" / "categories.yaml", CATS_FP)
    S.write_yaml(root / "first_pass" / "pitfalls.yaml", pitfalls or [])
    S.write_yaml(root / "first_pass" / "patterns.yaml", patterns or [])
    S.write_yaml(root / "repair" / "categories.yaml", [{"category_id": "RP-E3", "name": "main", "definition": "main path", "state": "active"}])
    S.write_yaml(root / "repair" / "patterns.yaml", repair or [])
    S.write_yaml(root / "author" / "categories.yaml", [{"category_id": "AU-MAIN_PATH", "name": "main", "definition": "main", "state": "active"}])
    S.write_yaml(root / "author" / "rules.yaml", rules or [])
    for b in S.BANKS:
        (root / b / "SKILL.md").write_text(f"# skill of {b}\nRead the catalogue.\n", encoding="utf-8")
        S.write_yaml(root / b / "CHANGELOG.yaml", [{"version": 1, "canary": f"CANARY-{S.PREFIX[b]}-0123456789abcdef"}])
    (root / "VERSION").write_text("1\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _clean_env():
    yield
    R.MemoryRun.deactivate()
    os.environ.pop(S.ENABLE_ENV, None)


# --------------------------------------------------------------------------- formats and store


def test_load_and_snapshot_and_version_bump(tmp_path):
    root = make_store(tmp_path, pitfalls=[pit("P-0001")])
    v = S.pin(root)
    assert v.version == 1 and v.directory == root / "versions" / "v1"
    assert [p["pitfall_id"] for p in v.entries("first_pass", "pitfalls")] == ["P-0001"]
    assert [c["category_id"] for c in v.active_categories("first_pass")] == ["FP-TYPE", "FP-DOC-EXC"]
    new = S.commit_write(root, "first_pass", "pitfalls", [pit("P-0001"), pit("P-0002")], reason="t", origin="test", changed_ids=["P-0002"])
    assert new == 2 and S.current_version(root) == 2
    assert len(S.pin(root).entries("first_pass", "pitfalls")) == 2
    assert len(v.entries("first_pass", "pitfalls")) == 1          # the old pin still reads v1
    assert not os.access(root / "versions" / "v1" / "first_pass" / "pitfalls.yaml", os.W_OK) or os.geteuid() == 0
    log = S.read_yaml_list(root / "first_pass" / "CHANGELOG.yaml")
    assert log[-1]["version"] == 2 and log[-1]["changed"] == ["P-0002"]


def test_format_errors():
    cats = ["FP-TYPE"]
    assert V.format_errors("first_pass", "pitfalls", pit("P-1"), category_ids=cats, domain_tags=["storage"]) == []
    errs = V.format_errors("first_pass", "pitfalls", {**pit("P-1", cat="FP-NOPE"), "state": "weird"}, category_ids=cats)
    assert any("unknown category" in e for e in errs) and any("state" in e for e in errs)
    errs = V.format_errors("first_pass", "pitfalls", pit("P-1", domain_tags=["quantum"]), category_ids=cats, domain_tags=["storage"])
    assert any("vocabulary" in e for e in errs)


# --------------------------------------------------------------------------- validator rejections


IDS = V.Identifiers(strong=frozenset({"cookiecutter", "find_template", "nontemplatedinputdirexception"}),
                    weak=frozenset({"vcs", "copy"}))


def test_validator_rejects_training_identifiers():
    assert V.identifier_leaks("Use find_template for that", IDS) == ["find_template"]
    assert V.identifier_leaks("the cookiecutter project", IDS) == ["cookiecutter"]
    assert V.identifier_leaks("copy the bytes as they are", IDS) == []           # plain English word
    assert V.identifier_leaks("call `copy` with the folder", IDS) == ["copy"]      # module name as code
    assert V.identifier_leaks("vcs.clone(url)", IDS) == ["vcs"]


def test_validator_rejects_test_task_source_and_template():
    sources = {"R-1": {"task": "flask", "split": "test"}, "R-2": {"task": "imapclient", "split": "train"}}
    e = {**pit("P-1"), "evidence": {"source_records": ["R-2", "R-1"]}}
    assert any("test task" in x for x in V.source_errors(e, sources, ["flask"]))
    assert V.source_errors({**pit("P-1"), "evidence": {"source_records": ["R-2"]}}, sources, ["flask"]) == []
    pattern = {"pattern_id": "FP-PAT-01", "category_id": "FP-TYPE", "state": "trial",
               "action": {"instruction": "x", "template": "review_then_fix"}}
    assert any("template" in x for x in V.format_errors("first_pass", "patterns", pattern, category_ids=["FP-TYPE"]))


def test_validator_rejects_heldout_substring(tmp_path):
    fake = tmp_path / "fake_heldout"
    fake.mkdir()
    (fake / "test_x.py").write_text("def test_x():\n    assert parse('alpha beta gamma delta') == 7\n", encoding="utf-8")
    entries = [{**pit("P-1"), "lesson": "Remember that parse('alpha beta gamma delta') returns a count."}, pit("P-2")]
    verdicts = V.validate_entries("first_pass", "pitfalls", entries, category_ids=["FP-TYPE"], domain_tags=[],
                                  identifiers=IDS, sources={}, test_tasks=[], extra_heldout=[fake], heldout_tasks=["__none__"])
    by = {v.entry_id: v for v in verdicts}
    assert not by["P-1"].ok and any("held-out" in r for r in by["P-1"].reasons)
    assert by["P-2"].ok


# --------------------------------------------------------------------------- recall


def test_recall_filters_orders_and_truncates():
    entries = [pit("P-1", fixes=1), pit("P-2", state="active", fixes=1), pit("P-3", fixes=9), pit("P-4", state="retired", fixes=99),
               pit("P-5", cat="FP-DOC-EXC", fixes=5), pit("P-6", fixes=50, domain_tags=["storage"]),
               pit("P-7", fixes=40, position=["first"])]
    feats = {"position_group": "middle"}
    got = recall(entries, categories=["FP-TYPE", "FP-DOC-EXC"], features=feats, domain_tags=["network_protocol"], limit=3, id_field="pitfall_id")
    assert [g["pitfall_id"] for g in got] == ["P-2", "P-3", "P-5"]          # active first, then fixes; P-4 retired, P-6/P-7 filtered
    got = recall(entries, categories=["FP-TYPE"], features=feats, domain_tags=["storage"], limit=10, id_field="pitfall_id")
    assert [g["pitfall_id"] for g in got] == ["P-2", "P-6", "P-3", "P-1"]
    assert applies({"applies_when": {"n_focus_files": {"op": "<=", "value": 6}}}, features={"n_focus_files": 6}, domain_tags=[])
    assert not applies({"applies_when": {"n_focus_files": {"op": "<=", "value": 6}}}, features={"n_focus_files": 7}, domain_tags=[])


# --------------------------------------------------------------------------- assembly


def test_assembly_headings_tags_and_ack():
    block = A.first_pass_block([pit("P-1"), pit("P-2")], [{"pattern_id": "FP-PAT-02", "action": {"instruction": "list the return types"}}])
    assert block.startswith(A.PITFALL_HEADING)
    assert A.PATTERN_HEADING in block and "[MEM:P-1]" in block and "[MEM:FP-PAT-02] list the return types" in block
    assert A.parse_tags(block) == ["P-1", "P-2", "FP-PAT-02"]
    assert A.parse_ack("done.\nMEMORY_ACK: P-1, [MEM:P-2], FP-PAT-02\n") == ["P-1", "P-2", "FP-PAT-02"]
    assert A.parse_ack("no ack here") is None
    assert A.first_pass_block([], []) == ""


def _milestone():
    from orchestra.realbench.milestone_planner import (
        AgentDraft,
        MilestoneAcceptance,
        MilestoneDraft,
    )

    writer = AgentDraft(role_id="agent_1_builder", title="Implementer", mandate="build it", role="implementer")
    return MilestoneDraft(milestone_id="m1", title="M1", objective="do m1", risk_rationale="r", gate_level="implementation", focus_paths=["pkg/a.py"],
                          acceptance=MilestoneAcceptance(criteria=["works"]), agents=[writer])


def _compile(tmp_path, milestone, agent):
    from orchestra.realbench.subgraph_builder import materialize_agent_contract

    d = tmp_path / f"contracts_{len(list(tmp_path.iterdir()))}"
    d.mkdir()
    entry = materialize_agent_contract(contracts_dir=d, milestone=milestone, agent=agent, agent_backend="codex_sdk")
    import yaml

    return yaml.safe_load(Path(entry["contract_path"]).read_text(encoding="utf-8"))


def test_compiled_prompt_carries_block_and_ack(tmp_path):
    from dataclasses import replace

    root = make_store(tmp_path)
    R.MemoryRun.activate(tmp_path / "run", {"memory": {}}, view=S.pin(root))
    m = _milestone()
    block = A.first_pass_block([pit("P-1")], [])
    agent = replace(m.agents[0], memory_block=block)
    c = _compile(tmp_path, m, agent)
    assert c["system_prompt_template"].endswith(block)
    assert c["user_prompt_template"].endswith(A.ACK_REQUIREMENT)


def test_memory_off_prompts_byte_identical(tmp_path):
    m = _milestone()
    R.MemoryRun.deactivate()
    plain = _compile(tmp_path, m, m.agents[0])
    assert "MEM:" not in plain["system_prompt_template"] and A.ACK_REQUIREMENT not in plain["user_prompt_template"]
    assert "memory_block" not in m.agents[0].to_dict()
    # the same draft compiled again is identical byte for byte
    again = _compile(tmp_path, m, m.agents[0])
    assert plain == again


def test_candidate_copy_loses_first_pass_block():
    block = A.first_pass_block([pit("P-1")], [])
    msgs = [{"role": "system", "content": "SYS\n\n" + block}, {"role": "user", "content": "USER\n\n" + A.ACK_REQUIREMENT},
            {"role": "user", "content": A.repair_block("E3-S1", "fix it") + "\n\n" + A.ACK_REQUIREMENT}]
    out = A.strip_first_pass(msgs)
    assert out[0]["content"] == "SYS" and out[1]["content"] == "USER"
    assert "[MEM:E3-S1]" in out[2]["content"]


# --------------------------------------------------------------------------- judge


def _view(tmp_path):
    return S.pin(make_store(tmp_path))


def test_judge_parses_and_verifies_skill_version(tmp_path):
    view = _view(tmp_path)
    _, version = J.skill_block(view.skill("first_pass"))
    ok = json.dumps({"domain_tags": ["network_protocol"], "selected": [{"category_id": "FP-TYPE", "reason": "r", "confidence": "high"}],
                     "skill_version": version})
    j = J.judge(view, "first_pass", milestone_text="m", docs_excerpt="", call=lambda s, p: ok)
    assert j.category_ids == ["FP-TYPE"] and j.skill_version == version


@pytest.mark.parametrize("answer, msg", [
    ("not json at all", "no JSON"),
    ('{"domain_tags": [], "selected": [], "skill_version": "deadbeef"}', "skill_version"),
    ('{"domain_tags": [], "selected": [{"category_id": "FP-MADEUP"}], "skill_version": "@V"}', "outside the catalogue"),
    ('{"domain_tags": [], "selected": [{"category_id": "FP-OLD"}], "skill_version": "@V"}', "outside the catalogue"),
    ('{"domain_tags": ["quantum"], "selected": [], "skill_version": "@V"}', "vocabulary"),
])
def test_judge_failures_stop(tmp_path, answer, msg):
    view = _view(tmp_path)
    _, version = J.skill_block(view.skill("first_pass"))
    with pytest.raises(J.JudgeError, match=msg):
        J.judge(view, "first_pass", milestone_text="m", docs_excerpt="", call=lambda s, p: answer.replace("@V", version))


# --------------------------------------------------------------------------- delivery checks (spec §4.2)


def _run(tmp_path, **cfg):
    root = make_store(tmp_path, pitfalls=[pit("P-1"), pit("P-2")],
                      repair=[{"pattern_id": "E3-S1", "category_id": "RP-E3", "state": "active", "action": {"instruction": "x"}}])
    return R.MemoryRun.activate(tmp_path / "run", {"memory": cfg}, view=S.pin(root))


def _prompt(tags):
    return "SYS\n\n" + A.first_pass_block([pit(t) for t in tags], []) + "\n\nUSER\n\n" + A.ACK_REQUIREMENT


def _after(run, pre, tmp_path, *, prompt, final, captured=True, rid="r1"):
    trace = tmp_path / "trace"
    trace.mkdir(exist_ok=True)
    if captured:
        (trace / f"{rid}.prompt.txt").write_text(prompt, encoding="utf-8")
    R.after_result(run, pre, node_id="n", contract_id="c1", task_id="t__subtask__m1", role="Implementer", prompt=prompt,
                   final_output=final, status="success", trace_dir=str(trace), request_id=rid, seeded=False)


def test_delivery_ok_path(tmp_path):
    run = _run(tmp_path)
    run.register_contract("c1", "first_pass", ["P-1", "P-2"])
    p = _prompt(["P-1", "P-2"])
    pre = R.before_send(run, node_id="n", contract_id="c1", task_id="t__subtask__m1", role="Implementer", prompt=p)
    _after(run, pre, tmp_path, prompt=p, final="done\nMEMORY_ACK: P-1, P-2")
    assert not (run.run_dir / R.VIOLATION_FILE).exists()


def test_fail_tag_missing_from_backend_prompt(tmp_path):
    run = _run(tmp_path)
    run.register_contract("c1", "first_pass", ["P-1", "P-2"])
    with pytest.raises(R.MemoryDeliveryError, match="tag_missing"):
        R.before_send(run, node_id="n", contract_id="c1", task_id="t__subtask__m1", role="Implementer", prompt=_prompt(["P-1"]))
    assert json.loads((run.run_dir / R.VIOLATION_FILE).read_text())[0]["kind"] == "tag_missing"


def test_fail_capture_empty(tmp_path):
    run = _run(tmp_path)
    p = _prompt(["P-1"])
    pre = R.before_send(run, node_id="n", contract_id="c1", task_id="t__subtask__m1", role="Implementer", prompt=p)
    with pytest.raises(R.MemoryDeliveryError, match="capture_empty"):
        _after(run, pre, tmp_path, prompt=p, final="MEMORY_ACK: P-1", captured=False)


@pytest.mark.parametrize("final, kind", [("all done", "ack_missing"), ("MEMORY_ACK: P-1", "ack_mismatch"),
                                         ("MEMORY_ACK: P-1, P-2, P-9", "ack_mismatch")])
def test_fail_ack(tmp_path, final, kind):
    run = _run(tmp_path)
    p = _prompt(["P-1", "P-2"])
    pre = R.before_send(run, node_id="n", contract_id="c1", task_id="t__subtask__m1", role="Implementer", prompt=p)
    with pytest.raises(R.MemoryDeliveryError, match=kind):
        _after(run, pre, tmp_path, prompt=p, final=final)


def test_fail_cross_bank_and_canary(tmp_path):
    run = _run(tmp_path)
    with pytest.raises(R.MemoryDeliveryError, match="cross_bank"):
        R.before_send(run, node_id="n", contract_id="cx", task_id="t__m1__candidate__cand_x", role="Improver", prompt=_prompt(["P-1"]))
    run2 = _run(tmp_path / "b")
    with pytest.raises(R.MemoryDeliveryError, match="canary"):
        R.before_send(run2, node_id="n", contract_id="cx", task_id="t__subtask__m1", role="Implementer",
                      prompt="hello CANARY-FP-0123456789abcdef")


def test_fail_required_role_did_not_run(tmp_path):
    run = _run(tmp_path)
    run.record("first_pass", {"recalled": {"pitfalls": [], "patterns": ["FP-PAT-01"]}, "tags": [], "writer_contract": "",
                              "roles_added": ["behaviour_critic"]}, key="m1")
    audit = R.audit_run(run.run_dir)
    assert not audit["ok"] and any("behaviour_critic did not run" in p for p in audit["problems"])


def test_memory_access_detected_in_commands(tmp_path):
    root = tmp_path / "memory"
    assert R.memory_access_in(f'{{"command": "cat {root}/first_pass/pitfalls.yaml"}}', root)
    assert R.memory_access_in('{"command": "cat ../../memory/first_pass/pitfalls.yaml"}', root)
    assert not R.memory_access_in('{"command": "pytest -q"}', root)


def test_ack_requirement_names_no_entry():
    # the fixed requirement reaches every bank's prompts: it must not carry an id of any bank
    assert A.parse_tags(A.ACK_REQUIREMENT) == []
    import re

    assert not re.search(r"\b(?:P-\d{4}|FP-PAT-\d+|[A-Z]\d?-[A-Z]\d+|AU-R-\d+)\b", A.ACK_REQUIREMENT)


# --------------------------------------------------------------------------- executor integration


@pytest.mark.asyncio
async def test_executor_runs_checks_end_to_end(tmp_path):
    from orchestra.backends.factory import build_structured_llm_registry
    from orchestra.executors.agent import AgentNodeExecutor
    from orchestra.executors.harness import HarnessNodeExecutor
    from orchestra.executors.registry import NodeExecutorRegistry
    from orchestra.ir.contracts import AgentContract
    from orchestra.ir.nodes import AgentNodeSpec, NodeKind
    from orchestra.llm.base_async import AsyncLLMClient
    from orchestra.llm.usage import LLMResponse, LLMUsage
    from orchestra.runtime.backend import RunContext
    from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
    from orchestra.sandbox.mock import MockSandbox

    replies = {}

    class _Client(AsyncLLMClient):
        async def generate(self, **kw):
            return LLMResponse(text=replies["text"], model="mock", latency_ms=1, usage=LLMUsage())

    run = _run(tmp_path)
    run.register_contract("coder", "first_pass", ["P-1"])
    contract = AgentContract(
        contract_id="coder", role="Implementer", system_prompt_template="x\n\n" + A.first_pass_block([pit("P-1")], []),
        user_prompt_template="{artifacts_json}\n\n" + A.ACK_REQUIREMENT, model="mock", temperature=0.0, max_tokens=16,
        timeout_seconds=5, input_schema="ProblemArtifact", output_schema="CodeArtifact", parser_id="python_code",
    )
    reg = NodeExecutorRegistry(agent_executor=AgentNodeExecutor({"coder": contract}, build_structured_llm_registry(_Client())),
                               harness_executor=HarnessNodeExecutor(MockSandbox()))
    limits = RuntimeLimits()
    ctx = RunContext(run_id="r", task_id="t__subtask__m1", run_dir=tmp_path / "rd", limits=limits,
                     semaphores=RuntimeSemaphores(limits), contract_hash="c")
    node = AgentNodeSpec(node_id="coder", node_kind=NodeKind.AGENT, contract_id="coder",
                         input_slots={"problem": "ProblemArtifact"}, output_slots={"code": "CodeArtifact"})
    replies["text"] = "```python\nprint(1)\n```\nMEMORY_ACK: P-1"
    res = await reg.execute_safely(node, {}, ctx)
    assert res.succeeded
    nodes = [json.loads(x) for x in (run.run_dir / R.NODES_FILE).read_text().splitlines()]
    assert nodes[-1]["ok"] and nodes[-1]["memory_ack"] == ["P-1"]
    replies["text"] = "```python\nprint(1)\n```\n"
    with pytest.raises(R.MemoryDeliveryError):
        await reg.execute_safely(node, {}, ctx)
