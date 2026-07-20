"""Hybrid Codex session lifecycle: generator, lineage, lifecycle adapter, executor."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestra.backends.base import (
    AgentRequest,
    AgentSessionPolicy,
    BackendExecutionContext,
    BackendSessionRef,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.capabilities import SessionPolicy
from orchestra.backends.catalog import KNOWN_BACKEND_CAPABILITIES, capabilities_for
from orchestra.backends.codex_lifecycle import CodexLifecycleError, CodexThreadLifecycleAdapter
from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.backends.errors import BackendCapabilityError
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.fast_loop.candidate_generator import RuleBasedLocalCandidateGenerator
from orchestra.control.fast_loop.capability import validate_candidate_against_capabilities
from orchestra.control.fast_loop.diagnosis import diagnose_subtask_failure
from orchestra.control.fast_loop.hybrid_generator import HybridCodexLocalCandidateGenerator
from orchestra.control.fast_loop.schemas import (
    CodexSessionMode,
    FastLoopBudget,
    FastLoopConfig,
    HybridCodexConfig,
    LocalCandidate,
    MissingParentPolicy,
    NodeSessionDirective,
)
from orchestra.control.session_lineage import (
    AgentSessionLineageRecord,
    SessionLineageResolver,
    SessionParentResolutionError,
    append_lineage_records,
    lineage_identity_key,
    make_lineage_id,
    validate_session_workspace_binding,
)
from orchestra.control.task_state import (
    BackendSessionRecord,
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
    TaskExecutionState,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.workspaces.base import WorkspaceRef

GRAPH = "configs/graphs/codex_single_implementer.yaml"
CONTRACTS = "configs/contracts"
PLAN = Path("configs/plans/codex_tiny_repo_single_subtask.yaml")
FIXTURE = Path("tests/fixtures/codex_tiny_repo").resolve()
AGENT_NODE = "codex_implementer"


class _FakeTurn:
    final_response = "ok"
    usage = SimpleNamespace(input_tokens=2, output_tokens=1)


def _write_calc(workspace: str, body: str) -> None:
    Path(workspace, "calculator.py").write_text(body, encoding="utf-8")


class _FakeThread:
    def __init__(self, cwd: str, thread_id: str) -> None:
        self.cwd = cwd
        self.id = thread_id

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del kwargs
        if (
            "Previous attempt failed" in prompt
            or "Harness" in prompt
            or "pytest" in prompt
        ):
            _write_calc(self.cwd, "def add(a, b):\n    return a + b\n")
        else:
            _write_calc(self.cwd, "def add(a, b):\n    return a * b\n")
        return _FakeTurn()


class FakeHybridCodex:
    """Deterministic AsyncCodex stand-in with thread_start/resume/fork."""

    def __init__(
        self,
        *,
        supports_resume: bool = True,
        supports_fork: bool = True,
        fake_fork: bool = False,
    ) -> None:
        self.threads: dict[str, _FakeThread] = {}
        self.calls: list[tuple[str, str | None]] = []
        self._n = 0
        self.supports_resume = supports_resume
        self.supports_fork = supports_fork
        self.fake_fork = fake_fork

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self._n += 1
        tid = f"thread-{self._n}"
        thread = _FakeThread(kwargs["cwd"], tid)
        self.threads[tid] = thread
        self.calls.append(("start", None))
        return thread

    async def thread_resume(self, parent_thread_id: str, **kwargs):  # noqa: ANN003
        if not self.supports_resume:
            raise AttributeError("thread_resume unavailable")
        self.calls.append(("resume", parent_thread_id))
        parent = self.threads[parent_thread_id]
        resumed = _FakeThread(kwargs["cwd"], parent.id)
        return resumed

    async def thread_fork(self, parent_thread_id: str, **kwargs):  # noqa: ANN003
        if not self.supports_fork:
            raise AttributeError("thread_fork unavailable")
        self.calls.append(("fork", parent_thread_id))
        if self.fake_fork:
            return self.threads[parent_thread_id]
        self._n += 1
        tid = f"thread-fork-{self._n}"
        forked = _FakeThread(kwargs["cwd"], tid)
        self.threads[tid] = forked
        return forked


def _hybrid_config(
    *,
    mode: CodexSessionMode = CodexSessionMode.HYBRID,
    enable_resume: bool = True,
    enable_fork: bool = True,
    add_fresh_critic: bool = True,
    missing_parent_policy: MissingParentPolicy = MissingParentPolicy.REJECT,
) -> FastLoopConfig:
    hybrid = HybridCodexConfig(
        enable_resume=enable_resume,
        enable_fork=enable_fork,
        add_fresh_critic=add_fresh_critic,
        missing_parent_policy=missing_parent_policy,
    )
    return FastLoopConfig(
        codex_session_mode=mode,
        hybrid_codex=hybrid,
        budget=FastLoopBudget(max_candidates=3),
    )


def _resume_only_config() -> FastLoopConfig:
    return _hybrid_config(
        mode=CodexSessionMode.RESUME_ONLY,
        enable_fork=False,
        add_fresh_critic=False,
    )


def _git_workspace(tmp_path: Path, name: str) -> str:
    ws = tmp_path / name
    shutil.copytree(FIXTURE, ws)
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=ws,
        check=True,
        capture_output=True,
    )
    return str(ws)


def _failed_subtask(*, session_id: str = "thread-t0") -> SubtaskState:
    return SubtaskState(
        spec=SubtaskSpec(
            subtask_id="implement_fix",
            title="Fix repository bug",
            objective="Make tests pass",
            keystone_harness_id="repository_test_harness",
            local_graph_template=GRAPH,
            budget=BudgetSpec(),
        ),
        status=SubtaskStatus.HARNESS_FAILED,
        failure_reason=SubtaskFailureReason.HARNESS,
        failure_message="assert add(1, 1) == 2 failed",
        workspace_ref=str(FIXTURE),
        backend_sessions=[
            BackendSessionRecord(
                node_id=AGENT_NODE,
                backend_id="codex_sdk",
                attempt_id=1,
                session_ref=BackendSessionRef(
                    backend_id="codex_sdk",
                    session_id=session_id,
                ),
            )
        ],
    )


def _failed_state(*, session_id: str = "thread-t0") -> TaskExecutionState:
    plan = TaskPlan.model_validate(yaml.safe_load(PLAN.read_text(encoding="utf-8")))
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["implement_fix"] = _failed_subtask(session_id=session_id)
    return state


def _diagnosis(state: TaskExecutionState) -> object:
    graph = load_graph(GRAPH)
    return diagnose_subtask_failure(
        subtask_state=state.subtasks["implement_fix"],
        graph=graph,
    )


def _generator(config: FastLoopConfig | None = None) -> HybridCodexLocalCandidateGenerator:
    return HybridCodexLocalCandidateGenerator(
        compiler=build_compiler(CONTRACTS),
        config=config or _hybrid_config(),
    )


def _run_context(
    tmp_path: Path,
    *,
    directives: dict[str, NodeSessionDirective] | None = None,
) -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id="run-hybrid",
        task_id="codex_tiny_repo",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="contracts",
        subtask_id="implement_fix",
        node_session_directives=dict(directives or {}),
    )


def _backend_context(workspace: str) -> BackendExecutionContext:
    return BackendExecutionContext(
        run_id="run-hybrid-backend",
        task_id="codex_tiny_repo",
        subtask_id="implement_fix",
        node_id=AGENT_NODE,
        workspace_ref=workspace,
    )


def _agent_request(
    *,
    policy: AgentSessionPolicy,
    parent_id: str | None,
    workspace: str,
) -> AgentRequest:
    session_ref = None
    if parent_id:
        session_ref = BackendSessionRef(backend_id="codex_sdk", session_id=parent_id)
    return AgentRequest(
        request_id="req-1",
        task_id="codex_tiny_repo",
        subtask_id="implement_fix",
        node_id=AGENT_NODE,
        role="implementer",
        instruction="fix",
        session_policy=policy,
        session_ref=session_ref,
        model=ModelSpec(name="gpt-test", temperature=0.0, max_tokens=256),
        output_contract=OutputContract(
            parser_id="repository_change",
            output_schema="RepositoryChangeArtifact",
        ),
        backend_config={
            "type": "codex_sdk",
            "thread_policy": "fresh",
            "sandbox": "workspace_write",
            "approval_policy": "never",
            "require_git_diff": True,
        },
        messages=[{"role": "user", "content": "fix calculator"}],
        rendered_context="fix calculator",
        contract_id="codex_implementer",
        timeout_seconds=60,
    )


def test_fresh_only_mode_preserves_existing_behavior():
    graph = load_graph(GRAPH)
    sub = _failed_subtask()
    diagnosis = diagnose_subtask_failure(subtask_state=sub, graph=graph)
    gen = _generator(_hybrid_config(mode=CodexSessionMode.FRESH_ONLY))
    cands = gen.generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=3),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
        state=_failed_state(),
        subtask_id="implement_fix",
    )
    rule = RuleBasedLocalCandidateGenerator(compiler=build_compiler(CONTRACTS))
    expected = rule.generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=3),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
    )
    assert len(cands) == len(expected)
    assert all(c.session_policy is SessionPolicy.FRESH for c in cands)
    assert all(not c.session_directives for c in cands)


def test_hybrid_generator_creates_resume_candidate():
    state = _failed_state()
    cands = _generator(_resume_only_config()).generate(
        graph=load_graph(GRAPH),
        diagnosis=_diagnosis(state),
        budget=FastLoopBudget(max_candidates=3),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
        state=state,
        subtask_id="implement_fix",
    )
    resume = next(c for c in cands if c.candidate_id == "cand_resume")
    assert resume.session_policy is SessionPolicy.RESUME
    directive = resume.session_directives[AGENT_NODE]
    assert directive.policy is SessionPolicy.RESUME
    assert directive.source_session_ref is not None
    assert directive.source_session_ref.session_id == "thread-t0"


def test_hybrid_generator_creates_fork_candidate():
    state = _failed_state()
    cands = _generator(
        _hybrid_config(
            mode=CodexSessionMode.FORK_ONLY,
            enable_resume=False,
            add_fresh_critic=False,
        )
    ).generate(
        graph=load_graph(GRAPH),
        diagnosis=_diagnosis(state),
        budget=FastLoopBudget(max_candidates=3),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
        state=state,
        subtask_id="implement_fix",
    )
    fork = next(c for c in cands if c.candidate_id == "cand_fork")
    assert fork.session_policy is SessionPolicy.FORK
    assert fork.session_directives[AGENT_NODE].policy is SessionPolicy.FORK


def test_hybrid_generator_creates_fresh_critic_candidate():
    state = _failed_state()
    cands = _generator(
        _hybrid_config(
            mode=CodexSessionMode.HYBRID,
            enable_resume=False,
            enable_fork=True,
            add_fresh_critic=True,
        )
    ).generate(
        graph=load_graph(GRAPH),
        diagnosis=_diagnosis(state),
        budget=FastLoopBudget(max_candidates=3),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
        state=state,
        subtask_id="implement_fix",
    )
    critic = next(c for c in cands if c.candidate_id == "cand_critic_repair")
    verifier_ids = [n for n in critic.session_directives if n.endswith("__verifier")]
    if verifier_ids:
        assert all(
            critic.session_directives[v].policy is SessionPolicy.FRESH for v in verifier_ids
        )
    assert critic.session_directives[AGENT_NODE].policy in {
        SessionPolicy.FORK,
        SessionPolicy.RESUME,
    }


def test_node_directives_allow_mixed_policies():
    graph = load_graph(GRAPH)
    parent = BackendSessionRef(backend_id="codex_sdk", session_id="thread-t0")
    verifier_id = f"{AGENT_NODE}__verifier"
    directives = {
        AGENT_NODE: NodeSessionDirective(
            node_id=AGENT_NODE,
            backend_id="codex_sdk",
            policy=SessionPolicy.FORK,
            source_session_ref=parent,
            source_node_id=AGENT_NODE,
            require_parent_session=True,
            lineage_reason="fork repairer",
        ),
        verifier_id: NodeSessionDirective(
            node_id=verifier_id,
            backend_id="structured_llm",
            policy=SessionPolicy.FRESH,
            lineage_reason="fresh critic verifier",
        ),
    }
    cand = LocalCandidate(
        candidate_id="mixed_policies",
        parent_graph_hash=graph.content_hash,
        edits=[],
        graph=graph.clone(),
        session_policy=SessionPolicy.FORK,
        session_directives=directives,
        generation_reason="mixed critic FRESH + repairer FORK",
    )
    caps = {
        "codex_sdk": capabilities_for("codex_sdk"),
        "structured_llm": capabilities_for("structured_llm"),
    }
    assert caps["codex_sdk"] is not None
    assert caps["structured_llm"] is not None
    result = validate_candidate_against_capabilities(cand, caps)
    assert result.compatible is True


def test_candidate_global_policy_not_used_for_node_execution(tmp_path):
    graph = load_graph(GRAPH)
    node = next(n for n in graph.nodes if n.node_id == AGENT_NODE)
    assert isinstance(node, AgentNodeSpec)
    parent = BackendSessionRef(backend_id="codex_sdk", session_id="thread-t0")
    directive = NodeSessionDirective(
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.FORK,
        source_session_ref=parent,
        source_node_id=AGENT_NODE,
        require_parent_session=True,
        lineage_reason="directive overrides legacy candidate policy",
    )
    executor = AgentNodeExecutor(load_contracts(CONTRACTS), AgentBackendRegistry())
    request = executor._build_request(
        node,
        {},
        _run_context(tmp_path, directives={AGENT_NODE: directive}),
    )
    assert request.session_policy is AgentSessionPolicy.FORK
    assert request.session_ref is not None
    assert request.session_ref.session_id == "thread-t0"


def test_parent_session_resolved_by_exact_node():
    state = _failed_state(session_id="thread-exact")
    resolver = SessionLineageResolver()
    ref = resolver.resolve_failed_initial_parent(
        state=state,
        subtask_id="implement_fix",
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
    )
    assert ref is not None
    assert ref.session_id == "thread-exact"


def test_parent_session_not_taken_from_other_subtask():
    state = _failed_state(session_id="thread-main")
    other = BackendSessionRecord(
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        attempt_id=1,
        session_ref=BackendSessionRef(
            backend_id="codex_sdk",
            session_id="thread-other-subtask",
        ),
    )
    other_sub = SubtaskState(
        spec=SubtaskSpec(
            subtask_id="other_subtask",
            title="other",
            objective="other",
            keystone_harness_id="repository_test_harness",
            local_graph_template=GRAPH,
            budget=BudgetSpec(),
        ),
        status=SubtaskStatus.HARNESS_FAILED,
        backend_sessions=[other],
    )
    state.subtasks["other_subtask"] = other_sub
    resolver = SessionLineageResolver()
    ref = resolver.resolve_failed_initial_parent(
        state=state,
        subtask_id="implement_fix",
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
    )
    assert ref is not None
    assert ref.session_id == "thread-main"


def test_ambiguous_parent_session_fails_closed():
    state = _failed_state(session_id="thread-a")
    sub = state.subtasks["implement_fix"]
    sub.backend_sessions.append(
        BackendSessionRecord(
            node_id=AGENT_NODE,
            backend_id="codex_sdk",
            attempt_id=2,
            session_ref=BackendSessionRef(
                backend_id="codex_sdk",
                session_id="thread-b",
            ),
        )
    )
    resolver = SessionLineageResolver()
    with pytest.raises(SessionParentResolutionError, match="ambiguous"):
        resolver.resolve_parent(
            state=state,
            subtask_id="implement_fix",
            node_id=AGENT_NODE,
            backend_id="codex_sdk",
        )


def test_resume_request_contains_parent_session_ref(tmp_path):
    graph = load_graph(GRAPH)
    node = next(n for n in graph.nodes if n.node_id == AGENT_NODE)
    directive = NodeSessionDirective(
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.RESUME,
        source_session_ref=BackendSessionRef(
            backend_id="codex_sdk",
            session_id="thread-parent",
        ),
        source_node_id=AGENT_NODE,
        require_parent_session=True,
        lineage_reason="resume test",
    )
    executor = AgentNodeExecutor(load_contracts(CONTRACTS), AgentBackendRegistry())
    request = executor._build_request(
        node,
        {},
        _run_context(tmp_path, directives={AGENT_NODE: directive}),
    )
    assert request.session_policy is AgentSessionPolicy.RESUME
    assert request.session_ref.session_id == "thread-parent"


def test_fork_request_contains_parent_session_ref(tmp_path):
    graph = load_graph(GRAPH)
    node = next(n for n in graph.nodes if n.node_id == AGENT_NODE)
    directive = NodeSessionDirective(
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.FORK,
        source_session_ref=BackendSessionRef(
            backend_id="codex_sdk",
            session_id="thread-parent",
        ),
        source_node_id=AGENT_NODE,
        require_parent_session=True,
        lineage_reason="fork test",
    )
    executor = AgentNodeExecutor(load_contracts(CONTRACTS), AgentBackendRegistry())
    request = executor._build_request(
        node,
        {},
        _run_context(tmp_path, directives={AGENT_NODE: directive}),
    )
    assert request.session_policy is AgentSessionPolicy.FORK
    assert request.session_ref.session_id == "thread-parent"


def test_fresh_request_has_no_parent_session_ref(tmp_path):
    graph = load_graph(GRAPH)
    node = next(n for n in graph.nodes if n.node_id == AGENT_NODE)
    directive = NodeSessionDirective(
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.FRESH,
        lineage_reason="fresh critic",
    )
    executor = AgentNodeExecutor(load_contracts(CONTRACTS), AgentBackendRegistry())
    request = executor._build_request(
        node,
        {},
        _run_context(tmp_path, directives={AGENT_NODE: directive}),
    )
    assert request.session_policy is AgentSessionPolicy.FRESH
    assert request.session_ref is None


@pytest.mark.asyncio
async def test_codex_fresh_starts_new_thread(tmp_path):
    pytest.importorskip("openai_codex")
    client = FakeHybridCodex()
    backend = CodexSDKBackend(client_factory=lambda: client)
    ws = _git_workspace(tmp_path, "ws")
    _write_calc(ws, "def add(a, b):\n    return a - b\n")
    result = await backend.run(
        _agent_request(
            policy=AgentSessionPolicy.FRESH,
            parent_id=None,
            workspace=ws,
        ),
        _backend_context(ws),
    )
    assert result.status.value == "success"
    assert ("start", None) in client.calls
    assert not any(call[0] == "resume" for call in client.calls)
    assert not any(call[0] == "fork" for call in client.calls)


@pytest.mark.asyncio
async def test_codex_resume_uses_real_sdk_resume(tmp_path):
    pytest.importorskip("openai_codex")
    client = FakeHybridCodex()
    await client.thread_start(cwd=str(tmp_path))
    backend = CodexSDKBackend(client_factory=lambda: client)
    ws = _git_workspace(tmp_path, "ws-resume")
    _write_calc(ws, "def add(a, b):\n    return a - b\n")
    result = await backend.run(
        _agent_request(
            policy=AgentSessionPolicy.RESUME,
            parent_id="thread-1",
            workspace=ws,
        ),
        _backend_context(ws),
    )
    assert result.status.value == "success"
    assert ("resume", "thread-1") in client.calls


@pytest.mark.asyncio
async def test_codex_fork_uses_real_sdk_fork(tmp_path):
    pytest.importorskip("openai_codex")
    client = FakeHybridCodex()
    await client.thread_start(cwd=str(tmp_path))
    backend = CodexSDKBackend(client_factory=lambda: client)
    ws = _git_workspace(tmp_path, "ws-fork")
    _write_calc(ws, "def add(a, b):\n    return a - b\n")
    result = await backend.run(
        _agent_request(
            policy=AgentSessionPolicy.FORK,
            parent_id="thread-1",
            workspace=ws,
        ),
        _backend_context(ws),
    )
    assert result.status.value == "success"
    assert ("fork", "thread-1") in client.calls


@pytest.mark.asyncio
async def test_fork_returns_new_session_with_parent(tmp_path):
    pytest.importorskip("openai_codex")
    client = FakeHybridCodex()
    await client.thread_start(cwd=str(tmp_path))
    backend = CodexSDKBackend(client_factory=lambda: client)
    ws = _git_workspace(tmp_path, "ws-fork-parent")
    _write_calc(ws, "def add(a, b):\n    return a - b\n")
    result = await backend.run(
        _agent_request(
            policy=AgentSessionPolicy.FORK,
            parent_id="thread-1",
            workspace=ws,
        ),
        _backend_context(ws),
    )
    assert result.session_ref is not None
    assert result.session_ref.session_id != "thread-1"
    assert result.session_ref.parent_session_id == "thread-1"


@pytest.mark.asyncio
async def test_fake_fork_is_forbidden(tmp_path):
    pytest.importorskip("openai_codex")
    client = FakeHybridCodex(fake_fork=True)
    await client.thread_start(cwd=str(tmp_path))
    adapter = CodexThreadLifecycleAdapter(client)
    with pytest.raises(CodexLifecycleError, match="fake FORK forbidden"):
        await adapter.fork(
            parent_thread_id="thread-1",
            cwd=str(tmp_path),
            sandbox=None,
            approval_mode=None,
            model=None,
        )


def test_resume_unsupported_is_rejected():
    client = SimpleNamespace(thread_start=lambda **kw: None)
    adapter = CodexThreadLifecycleAdapter(client)
    with pytest.raises(CodexLifecycleError, match="thread_resume"):
        adapter.assert_supported(AgentSessionPolicy.RESUME)


def test_fork_unsupported_is_rejected():
    client = SimpleNamespace(
        thread_start=lambda **kw: None,
        thread_resume=lambda *_a, **_k: None,
    )
    adapter = CodexThreadLifecycleAdapter(client)
    with pytest.raises(CodexLifecycleError, match="thread_fork"):
        adapter.assert_supported(AgentSessionPolicy.FORK)


def test_no_silent_fresh_downgrade():
    state = _failed_state()
    state.subtasks["implement_fix"].backend_sessions = []
    cands = _generator(_resume_only_config()).generate(
        graph=load_graph(GRAPH),
        diagnosis=_diagnosis(state),
        budget=FastLoopBudget(max_candidates=3),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
        state=state,
        subtask_id="implement_fix",
    )
    resume = next(c for c in cands if c.candidate_id == "cand_resume")
    assert resume.compatibility_rejected is True
    assert "no silent FRESH label" in (resume.rejection_message or "")


def test_candidate_workspace_bound_to_child_session():
    directive = NodeSessionDirective(
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.FORK,
        source_session_ref=BackendSessionRef(
            backend_id="codex_sdk",
            session_id="thread-parent",
        ),
        source_node_id=AGENT_NODE,
        require_parent_session=True,
        workspace_binding="candidate_isolated",
        lineage_reason="bind child workspace",
    )
    parent = AgentSessionLineageRecord(
        lineage_id=make_lineage_id(
            task_id="codex_tiny_repo",
            subtask_id="implement_fix",
            attempt_id=1,
            candidate_id=None,
            node_id=AGENT_NODE,
            backend_id="codex_sdk",
            session_id="thread-parent",
        ),
        task_id="codex_tiny_repo",
        subtask_id="implement_fix",
        attempt_id=1,
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.FRESH,
        session_ref=BackendSessionRef(
            backend_id="codex_sdk",
            session_id="thread-parent",
        ),
        workspace_ref="/canonical/base",
        graph_hash="g1",
        state_version=1,
    )
    child_ws = WorkspaceRef(
        workspace_id="cand_fork",
        path="/tmp/candidate/cand_fork",
        task_id="codex_tiny_repo",
        subtask_id="implement_fix",
        base_revision="rev-base",
    )
    compat = validate_session_workspace_binding(
        directive,
        parent,
        child_ws,
        canonical_workspace_ref="/canonical/base",
    )
    assert compat.compatible is True


def test_parent_workspace_mismatch_rejected():
    directive = NodeSessionDirective(
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.RESUME,
        source_session_ref=BackendSessionRef(
            backend_id="codex_sdk",
            session_id="thread-parent",
        ),
        source_node_id=AGENT_NODE,
        require_parent_session=True,
        workspace_binding="require_same_base_revision",
        lineage_reason="revision gate",
    )
    parent = AgentSessionLineageRecord(
        lineage_id="lin-1",
        task_id="t",
        subtask_id="s",
        attempt_id=1,
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.FRESH,
        session_ref=BackendSessionRef(backend_id="codex_sdk", session_id="thread-parent"),
        workspace_ref="/base",
        workspace_base_revision="rev-a",
        graph_hash="g",
        state_version=1,
    )
    child_ws = WorkspaceRef(
        workspace_id="c1",
        path="/tmp/c1",
        task_id="t",
        subtask_id="s",
        base_revision="rev-b",
    )
    compat = validate_session_workspace_binding(directive, parent, child_ws)
    assert compat.compatible is False
    assert "base revision" in (compat.reason or "")


def test_two_forks_use_separate_workspaces():
    directive = NodeSessionDirective(
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.FORK,
        source_session_ref=BackendSessionRef(
            backend_id="codex_sdk",
            session_id="thread-parent",
        ),
        source_node_id=AGENT_NODE,
        require_parent_session=True,
        lineage_reason="fork isolation",
    )
    shared_path = "/tmp/shared-candidate"
    child_ws = WorkspaceRef(
        workspace_id="cand_b",
        path=shared_path,
        task_id="t",
        subtask_id="s",
    )
    compat = validate_session_workspace_binding(
        directive,
        None,
        child_ws,
        other_candidate_workspaces={shared_path},
    )
    assert compat.compatible is False
    assert "shared with another candidate" in (compat.reason or "")


@pytest.mark.asyncio
async def test_session_lineage_checkpoint_roundtrip(tmp_path):
    record = AgentSessionLineageRecord(
        lineage_id=make_lineage_id(
            task_id="codex_tiny_repo",
            subtask_id="implement_fix",
            attempt_id=1,
            candidate_id="cand_fork",
            node_id=AGENT_NODE,
            backend_id="codex_sdk",
            session_id="thread-fork-1",
        ),
        task_id="codex_tiny_repo",
        subtask_id="implement_fix",
        attempt_id=2,
        candidate_id="cand_fork",
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.FORK,
        session_ref=BackendSessionRef(
            backend_id="codex_sdk",
            session_id="thread-fork-1",
            parent_session_id="thread-t0",
        ),
        parent_session_ref=BackendSessionRef(
            backend_id="codex_sdk",
            session_id="thread-t0",
        ),
        workspace_ref=str(tmp_path / "cand_fork"),
        graph_hash="graph-hash",
        state_version=3,
        lineage_reason="checkpoint roundtrip",
    )
    state = _failed_state()
    state.session_lineage_records = [record]
    store = TaskCheckpointStore(tmp_path)
    await store.save(state)
    loaded = await store.load("codex_tiny_repo")
    assert loaded is not None
    assert len(loaded.session_lineage_records) == 1
    loaded_record = loaded.session_lineage_records[0]
    if isinstance(loaded_record, dict):
        loaded_record = AgentSessionLineageRecord.model_validate(loaded_record)
    assert loaded_record.session_ref.session_id == "thread-fork-1"
    assert loaded_record.parent_session_ref.session_id == "thread-t0"


def test_session_lineage_deduplicates():
    record = AgentSessionLineageRecord(
        lineage_id="lin-dup",
        task_id="t",
        subtask_id="s",
        attempt_id=1,
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.FRESH,
        session_ref=BackendSessionRef(backend_id="codex_sdk", session_id="thread-1"),
        workspace_ref="/ws",
        graph_hash="g",
        state_version=1,
    )
    merged = append_lineage_records([record], [record.model_copy()])
    assert len(merged) == 1
    assert lineage_identity_key(record) == lineage_identity_key(merged[0])


@pytest.mark.asyncio
async def test_restart_preserves_selected_parent_session(tmp_path):
    parent = BackendSessionRef(backend_id="codex_sdk", session_id="thread-t0")
    directive = NodeSessionDirective(
        node_id=AGENT_NODE,
        backend_id="codex_sdk",
        policy=SessionPolicy.RESUME,
        source_session_ref=parent,
        source_node_id=AGENT_NODE,
        require_parent_session=True,
        lineage_reason="selected resume candidate",
    )
    from orchestra.control.fast_loop.schemas import (
        CandidateRecord,
        CandidateStatus,
        FastLoopState,
    )

    state = _failed_state()
    fl = FastLoopState(
        subtask_id="implement_fix",
        base_attempt_id=1,
        base_graph_hash=load_graph(GRAPH).content_hash,
        diagnosis=_diagnosis(state),
        selected_candidate_id="cand_resume",
        candidates=[
            CandidateRecord(
                candidate_id="cand_resume",
                attempt_id=2,
                graph_hash="g2",
                parent_graph_hash=load_graph(GRAPH).content_hash,
                edits=[],
                status=CandidateStatus.COMMITTED,
                session_policy=SessionPolicy.RESUME,
                session_directives={AGENT_NODE: directive},
            )
        ],
    )
    state.fast_loop_states["implement_fix"] = fl
    state.session_lineage_records = [
        AgentSessionLineageRecord(
            lineage_id="lin-parent",
            task_id=state.task_id,
            subtask_id="implement_fix",
            attempt_id=1,
            node_id=AGENT_NODE,
            backend_id="codex_sdk",
            policy=SessionPolicy.FRESH,
            session_ref=parent,
            workspace_ref=str(FIXTURE),
            graph_hash=load_graph(GRAPH).content_hash,
            state_version=1,
            lineage_reason="initial_attempt_before_fast_loop",
        )
    ]
    store = TaskCheckpointStore(tmp_path)
    await store.save(state)
    loaded = await store.load(state.task_id)
    assert loaded is not None
    winner = loaded.fast_loop_states["implement_fix"].candidates[0]
    restored = winner.session_directives[AGENT_NODE]
    if isinstance(restored, dict):
        restored = NodeSessionDirective.model_validate(restored)
    assert restored.source_session_ref.session_id == "thread-t0"


def test_executor_rejects_resume_without_directive(tmp_path):
    graph = load_graph(GRAPH)
    node = next(n for n in graph.nodes if n.node_id == AGENT_NODE)
    fork_node = node.model_copy(update={"session_policy": "resume"})
    executor = AgentNodeExecutor(load_contracts(CONTRACTS), AgentBackendRegistry())
    with pytest.raises(BackendCapabilityError, match="no silent FRESH downgrade"):
        executor._build_request(fork_node, {}, _run_context(tmp_path))
