"""Per-run memory state, delivery records and the fail-closed checks (memory spec §4, §2).

A run with memory on calls ``MemoryRun.activate`` once. That pins the snapshot,
writes ``<run_dir>/memory_run.json`` (pinned version, contract registry) and
sets two environment variables:
- ``ADAMAS_MEMORY_RUN`` makes the executor run the checks below;
- ``ADAMAS_CAPTURE_BACKEND`` makes the Codex backend store the exact prompt it
  sends, its turn items and the final reply under ``backend_traces/``.

Everything the spec asks to record goes to ``<run_dir>/memory_delivery.json``.
Node records first go to ``memory_delivery.nodes.jsonl``, which is safe to
append from concurrent nodes, and are merged into it by ``audit_run``.

A check that fails does two things:
- it writes ``<run_dir>/MEMORY_VIOLATION.json``, which marks the run invalid
  for every reader;
- it raises ``MemoryDeliveryError``, a BaseException, so the scheduler's
  ``except Exception`` cannot turn it into an ordinary node failure and the
  run stops.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestra.memory.assemble import ACK_REQUIREMENT, parse_ack, parse_tags
from orchestra.memory.store import BANKS, ID_FIELD, MemoryView, memory_root, pin, sha256_text

RUN_ENV = "ADAMAS_MEMORY_RUN"
CAPTURE_ENV = "ADAMAS_CAPTURE_BACKEND"
CANARY_RE = re.compile(r"CANARY-(?:FP|RP|AU)-[0-9a-f]{16}")
VIOLATION_FILE = "MEMORY_VIOLATION.json"
DELIVERY_FILE = "memory_delivery.json"
NODES_FILE = "memory_delivery.nodes.jsonl"


class MemoryDeliveryError(BaseException):
    """A memory delivery check failed; the run stops and counts for nothing."""


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class MemoryRun:
    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = Path(path)
        self.data = data
        self._view: MemoryView | None = None
        self._bank_of: dict[str, str] | None = None

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def activate(cls, run_dir: Path, cfg: dict[str, Any], *, view: MemoryView | None = None, task_id: str = "") -> MemoryRun:
        run_dir = Path(run_dir)
        view = view or pin(memory_root(cfg))
        path = run_dir / "memory_run.json"
        data = {
            "version": view.version, "snapshot": str(view.directory), "memory_root": str(view.root),
            "task_id": task_id, "config": cfg.get("memory") or {}, "run_dir": str(run_dir), "expected": {},
            "started": _now(),
        }
        run_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.environ[RUN_ENV] = str(path)
        os.environ[CAPTURE_ENV] = "1"
        run = cls(path, data)
        run._view = view
        run.record("run", {"memory_version": view.version, "snapshot": str(view.directory), "task_id": task_id, "started": data["started"]})
        return run

    @classmethod
    def current(cls) -> MemoryRun | None:
        p = (os.environ.get(RUN_ENV) or "").strip()
        if not p or not Path(p).is_file():
            return None
        return cls(Path(p), json.loads(Path(p).read_text(encoding="utf-8")))

    @staticmethod
    def deactivate() -> None:
        os.environ.pop(RUN_ENV, None)
        os.environ.pop(CAPTURE_ENV, None)

    @property
    def run_dir(self) -> Path:
        return Path(self.data["run_dir"])

    @property
    def cfg(self) -> dict[str, Any]:
        return dict(self.data.get("config") or {})

    def view(self) -> MemoryView:
        if self._view is None:
            self._view = MemoryView(root=Path(self.data["memory_root"]), version=int(self.data["version"]),
                                    directory=Path(self.data["snapshot"]))
        return self._view

    def bank_of(self, entry_id: str) -> str | None:
        if self._bank_of is None:
            m: dict[str, str] = {}
            v = self.view()
            for bank, names in BANKS.items():
                for name in names:
                    if name == "categories":
                        continue
                    for e in v.entries(bank, name):
                        m[str(e.get(ID_FIELD[name]))] = bank
            self._bank_of = m
        return self._bank_of.get(entry_id)

    # ------------------------------------------------------------------ registry and records

    def register_contract(self, contract_id: str, bank: str, tags: list[str], **extra: Any) -> None:
        with _locked(self.path):
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data.setdefault("expected", {})[contract_id] = {"bank": bank, "tags": list(tags), **extra}
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
            self.data = data

    def expected_for(self, contract_id: str) -> dict[str, Any] | None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return (data.get("expected") or {}).get(contract_id)

    def record(self, section: str, value: Any, key: str | None = None) -> None:
        p = self.run_dir / DELIVERY_FILE
        with _locked(p):
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
            if key is None:
                data[section] = value
            else:
                data.setdefault(section, {})[key] = value
            p.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    def node_record(self, rec: dict[str, Any]) -> None:
        p = self.run_dir / NODES_FILE
        with _locked(p):
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def violation(self, kind: str, detail: str, **ctx: Any) -> None:
        p = self.run_dir / VIOLATION_FILE
        with _locked(p):
            items = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []
            items.append({"kind": kind, "detail": detail, "at": _now(), **ctx})
            p.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
        if self.cfg.get("fail_closed", True):
            raise MemoryDeliveryError(f"memory delivery check failed ({kind}): {detail}")


# ---------------------------------------------------------------------- node checks


def node_bank(*, task_id: str, role: str, expected: dict[str, Any] | None) -> str:
    if "__candidate__" in task_id:
        return "repair"
    if expected:
        return str(expected.get("bank"))
    if role == "test_author":
        return "author"
    return "first_pass"


def before_send(run: MemoryRun, *, node_id: str, contract_id: str, task_id: str, role: str, prompt: str) -> dict[str, Any]:
    """Checks on the exact prompt about to be sent; returns what ``after_result`` needs."""
    expected = None if "__candidate__" in task_id else run.expected_for(contract_id)
    tags = parse_tags(prompt)
    ctx = {"node_id": node_id, "contract_id": contract_id, "task_id": task_id}
    if CANARY_RE.search(prompt):
        run.violation("canary_in_prompt", "a memory canary reached an agent prompt", **ctx)
    if expected:
        missing = [t for t in expected.get("tags") or [] if t not in tags]
        if missing:
            run.violation("tag_missing", f"recalled entries not in the backend prompt: {missing}", **ctx)
    bank = node_bank(task_id=task_id, role=role, expected=expected)
    foreign = [t for t in tags if run.bank_of(t) not in (bank, None)]
    unknown = [t for t in tags if run.bank_of(t) is None]
    if foreign:
        run.violation("cross_bank", f"{bank} node carries entries of another bank: {foreign}", **ctx)
    if unknown:
        run.violation("unknown_tag", f"tags not in the pinned memory version: {unknown}", **ctx)
    if tags and ACK_REQUIREMENT not in prompt:
        run.violation("ack_requirement_missing", "a prompt with memory tags lacks the MEMORY_ACK requirement", **ctx)
    return {"tags": tags, "bank": bank, "expected": expected}


def after_result(run: MemoryRun, pre: dict[str, Any], *, node_id: str, contract_id: str, task_id: str, role: str,
                 prompt: str, final_output: str, status: str, trace_dir: str, request_id: str, seeded: bool) -> None:
    ctx = {"node_id": node_id, "contract_id": contract_id, "task_id": task_id}
    rec: dict[str, Any] = {
        "at": _now(), **ctx, "role": role, "bank": pre["bank"], "request_id": request_id, "status": status,
        "seeded": seeded, "tags_in_prompt": pre["tags"], "prompt_sha256": sha256_text(prompt),
    }
    if seeded:
        rec["note"] = "replayed stored first run; no backend call"
        run.node_record(rec)
        return
    captured = Path(trace_dir) / f"{request_id}.prompt.txt"
    sent = captured.read_text(encoding="utf-8") if captured.is_file() else ""
    rec["captured_prompt"] = str(captured)
    if not sent.strip():
        run.node_record({**rec, "ok": False})
        run.violation("capture_empty", "the backend prompt capture is empty", **ctx)
    missing = [t for t in pre["tags"] if t not in parse_tags(sent)]
    if missing:
        run.node_record({**rec, "ok": False})
        run.violation("tag_missing_in_capture", f"tags absent from the captured backend prompt: {missing}", **ctx)
    items = Path(trace_dir) / f"{request_id}.items.json"
    if items.is_file():
        text = items.read_text(encoding="utf-8", errors="replace")
        if CANARY_RE.search(text):
            run.node_record({**rec, "ok": False})
            run.violation("canary_in_trace", "a memory canary appears in the agent's turn items", **ctx)
        if memory_access_in(text, Path(run.data["memory_root"])):
            run.node_record({**rec, "ok": False})
            run.violation("memory_access", "the agent's commands touched the memory store", **ctx)
    if CANARY_RE.search(final_output or ""):
        run.node_record({**rec, "ok": False})
        run.violation("canary_in_output", "a memory canary appears in the agent's reply", **ctx)
    ack = parse_ack(final_output or "")
    rec["memory_ack"] = ack
    if pre["tags"] and run.cfg.get("require_memory_ack", True):
        if ack is None:
            run.node_record({**rec, "ok": False})
            run.violation("ack_missing", "the final reply has no MEMORY_ACK line", **ctx)
        if set(ack or []) != set(pre["tags"]):
            run.node_record({**rec, "ok": False})
            run.violation("ack_mismatch", f"MEMORY_ACK {ack} != recalled {pre['tags']}", **ctx)
    rec["ok"] = True
    run.node_record(rec)


def memory_access_in(text: str, root: Path) -> bool:
    """A path into the memory store in an agent's commands or file operations."""
    low = text.replace("\\\\", "/")
    if str(root) in low:
        return True
    return bool(re.search(r"(?<![A-Za-z0-9_])memory/(?:first_pass|repair|author|transfer|versions)/", low))


# ---------------------------------------------------------------------- end-of-run audit


def _nodes(run_dir: Path) -> list[dict[str, Any]]:
    p = run_dir / NODES_FILE
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def canary_hits(run_dir: Path, *, extra_dirs: list[Path] | None = None) -> list[str]:
    """Files under the run (and ``extra_dirs``) containing a canary; the run's own memory bookkeeping excluded."""
    hits = []
    skip = {"memory_run.json", DELIVERY_FILE, NODES_FILE, VIOLATION_FILE}
    for base in [Path(run_dir), *(extra_dirs or [])]:
        for p in base.rglob("*"):
            if not p.is_file() or p.name in skip or p.stat().st_size > 20_000_000:
                continue
            try:
                if CANARY_RE.search(p.read_text(encoding="utf-8", errors="ignore")):
                    hits.append(str(p))
            except OSError:
                continue
    return hits


def audit_run(run_dir: Path, *, required_roles: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Merge node records into memory_delivery.json and check what can only be checked after the run."""
    run_dir = Path(run_dir)
    run_path = run_dir / "memory_run.json"
    data = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else {}
    nodes = _nodes(run_dir)
    delivery_p = run_dir / DELIVERY_FILE
    delivery = json.loads(delivery_p.read_text(encoding="utf-8")) if delivery_p.is_file() else {}
    problems: list[str] = []
    by_contract: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        by_contract.setdefault(n.get("contract_id", ""), []).append(n)
    contracts = {}
    for cid, exp in (data.get("expected") or {}).items():
        sent = [n for n in by_contract.get(cid, []) if not n.get("seeded")]
        state = "sent" if sent else ("seeded" if by_contract.get(cid) else "not run")
        contracts[cid] = {"bank": exp.get("bank"), "tags": exp.get("tags"), "state": state,
                          "ack": [n.get("memory_ack") for n in sent], "ok": all(n.get("ok") for n in sent) if sent else None}
    for mid, fp in (delivery.get("first_pass") or {}).items():
        cid = fp.get("writer_contract")
        recalled = list((fp.get("recalled") or {}).get("pitfalls") or []) + list((fp.get("recalled") or {}).get("patterns") or [])
        recalled_text = [t for t in recalled if t in set(fp.get("tags") or recalled)]
        if recalled_text and cid not in (data.get("expected") or {}):
            problems.append(f"{mid}: recalled {recalled_text} but no compiled contract registered them")
        for role in fp.get("roles_added") or []:
            ran = [n for n in nodes if n.get("role") == role and mid in str(n.get("contract_id", "")) and "__candidate__" not in n.get("task_id", "")]
            if not ran:
                problems.append(f"{mid}: required role {role} did not run")
    for key, rp in (delivery.get("repair") or {}).items():
        cand = rp.get("candidate_id", "")
        ran = [n for n in nodes if str(n.get("task_id", "")).endswith(f"__candidate__{cand}") and n.get("tags_in_prompt")]
        if not ran:
            rp_state = "not run"
        else:
            rp_state = "sent"
            if not all(set(rp.get("tags") or []) <= set(n.get("tags_in_prompt") or []) for n in ran):
                problems.append(f"repair {cand}: recalled {rp.get('tags')} not all in the prompt")
        delivery["repair"][key]["state"] = rp_state
    captured_empty = [n for n in nodes if not n.get("seeded") and not n.get("captured_prompt")]
    if captured_empty:
        problems.append(f"{len(captured_empty)} agent calls without a prompt capture")
    canaries = canary_hits(run_dir)
    if canaries:
        problems.append(f"canary found in {len(canaries)} run files")
    if problems:
        # found after the run: the run is marked invalid, nothing left to stop
        vp = run_dir / VIOLATION_FILE
        with _locked(vp):
            items = json.loads(vp.read_text(encoding="utf-8")) if vp.is_file() else []
            items += [{"kind": "audit", "detail": p, "at": _now()} for p in problems]
            vp.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
    violations = json.loads((run_dir / VIOLATION_FILE).read_text(encoding="utf-8")) if (run_dir / VIOLATION_FILE).is_file() else []
    audit = {"at": _now(), "nodes": len(nodes), "contracts": contracts, "violations": violations,
             "canary_hits": canaries, "problems": problems, "ok": not problems and not violations}
    delivery["nodes"] = nodes
    delivery["audit"] = audit
    delivery_p.write_text(json.dumps(delivery, indent=1, ensure_ascii=False), encoding="utf-8")
    return audit


__all__ = [
    "CANARY_RE", "CAPTURE_ENV", "DELIVERY_FILE", "MemoryDeliveryError", "MemoryRun", "RUN_ENV", "VIOLATION_FILE",
    "after_result", "audit_run", "before_send", "canary_hits", "memory_access_in", "node_bank",
]
