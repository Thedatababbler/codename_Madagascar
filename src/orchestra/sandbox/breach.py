"""Sandbox breach log (user decision 2026-10-11, docs/sandbox_breach_log.md).

After every run, each command an agent ran (captured turn items; the Codex
session logs in each private CODEX_HOME when items are missing) and its output
are classified into the events below. Each event goes, as one JSON line, to
``<run_dir>/sandbox_breach.log`` and to the global index
``outputs/sandbox/breach_index.jsonl``.

| event | level |
|---|---|
| read_denied: a read of a forbidden path, refused by Landlock | WARNING |
| list_denied: listing or searching a forbidden path (``find /``, ``ls``) | WARNING |
| network_denied: an external connection, refused by the network guard | WARNING |
| package_fetch: ``pip install`` / ``pip download`` / ``curl`` / ``wget`` / remote ``git clone`` | WARNING |
| ld_preload_tamper: unsetting, overriding or exporting ``LD_PRELOAD`` | CRITICAL |
| sandbox_tamper: touching sandbox configuration, variables or launch arguments | CRITICAL |
| canary: a memory canary in any agent input, output, record or workspace file | CRITICAL |
| capture_empty: an agent call whose commands were not captured (cannot be audited) | CRITICAL |
| unblocked_breach: an out-of-bounds command the audit finds and nothing refused | CRITICAL |

A record: time, run id, role, event, level, target (a directory, never a file
name or content), command (200 characters). A run with any CRITICAL event is
void: it is listed in ``outputs/sandbox/void_runs.jsonl`` and its ledger records
are dropped from every statistic (``is_void``). A run with 5 or more WARNINGs
is marked for manual review.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestra.sandbox import audit as A

ROOT = Path(__file__).resolve().parents[3]
INDEX = ROOT / "outputs" / "sandbox" / "breach_index.jsonl"
VOID = ROOT / "outputs" / "sandbox" / "void_runs.jsonl"
LOG_NAME = "sandbox_breach.log"
REVIEW_WARNINGS = 5

LEVEL = {"read_denied": "WARNING", "list_denied": "WARNING", "network_denied": "WARNING", "package_fetch": "WARNING",
         "ld_preload_tamper": "CRITICAL", "sandbox_tamper": "CRITICAL", "canary": "CRITICAL", "capture_empty": "CRITICAL",
         "unblocked_breach": "CRITICAL"}

_DENIED = re.compile(r"(?:Permission denied|EACCES|Operation not permitted)[^\n]{0,20}?['\"`]?(/[^\s'\"`:]+)?"
                     r"|(/[^\s'\"`:]+)['\"`]?:? Permission denied")
_NETGUARD = re.compile(r"adamas-netguard: [a-z]+ to a non-loopback address refused")
_FETCH = re.compile(r"\b(pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+(install|download)\b|\bcurl\b|\bwget\b"
                    r"|\bgit\s+clone\s+(?:-\S+\s+)*(?:https?|git|ssh)://|\bgit\s+clone\s+(?:-\S+\s+)*\S+@\S+:")
_LIST = re.compile(r"(?:^|[;&|(\s\"'])(find|ls|tree|du|rg|grep\s+-[a-zA-Z]*r[a-zA-Z]*|locate)\b")
_READ_VERB = re.compile(r"(?:^|[;&|(\s\"'])(cat|head|tail|sed|less|more|cp|rsync|tar|awk|od|xxd|strings|python3?|grep|rg)\b")
_SANDBOX_TAMPER = re.compile(r"ADAMAS_SANDBOX|ADAMAS_SANDBOXED|sandbox\.yaml|orchestra\.sandbox|libnetguard|landlock"
                             r"|--policy\b|policy\.json|\bunshare\b|\bnsenter\b|\bprctl\b|seccomp")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dir_only(path: str) -> str:
    """A target is recorded at directory level only: a file path becomes its directory."""
    p = path.rstrip("/") or "/"
    return p if os.path.isdir(p) else (os.path.dirname(p) or "/")


def _allowed_roots(run_dir: Path) -> list[str]:
    """Paths the run's agent policies granted (read or write), from sandbox_manifest.json."""
    m = Path(run_dir) / "sandbox_manifest.json"
    if not m.is_file():
        return []
    roots: set[str] = set()
    for e in json.loads(m.read_text(encoding="utf-8")).get("entries") or []:
        pol = e.get("policy") or e.get("agent_policy") or {}
        for p in [*(pol.get("read") or []), *(pol.get("write") or [])]:
            roots |= {os.path.normpath(p), os.path.realpath(p)}
    return sorted(roots)


def _outside(path: str, allowed: list[str], workspace: str = "") -> bool:
    """True when ``path`` lies outside every granted root (compared as real paths: /bin is /usr/bin)."""
    if not path.startswith("/"):
        return False
    cands = {os.path.normpath(path), os.path.realpath(path)}
    if workspace and any(rp == workspace or rp.startswith(workspace.rstrip("/") + "/") for rp in cands):
        return False
    return not any(rp == a or rp.startswith(a.rstrip("/") + "/") for rp in cands for a in allowed)


def _tokens(command: str) -> list[str]:
    """Shell tokens, with ``bash -lc '<script>'`` wrappers (how Codex runs every command) unwrapped."""
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.split()
    if len(toks) >= 3 and os.path.basename(toks[0]) in ("bash", "sh", "zsh", "dash") and toks[1] in ("-c", "-lc", "-ic", "-lic"):
        return _tokens(toks[2]) + toks[3:]          # the wrapper itself is not a target
    return toks


def _paths_in(command: str) -> list[str]:
    toks = _tokens(command)
    # an executable named by path (/usr/bin/python3, /bin/cat) is what runs, not what is read or listed
    toks = [t for i, t in enumerate(toks) if not (i == 0 or (i > 0 and toks[i - 1] in (";", "&&", "||", "|")))]
    out = []
    for t in toks:
        for part in re.split(r"[=;&|<>()]", t):
            if part.startswith("/") and len(part) > 1 or part == "/":
                out.append(part)
    return out


def calls(run_dir: Path) -> list[dict[str, Any]]:
    """Every captured agent command with its output, role and source: turn items first, session logs otherwise."""
    run_dir = Path(run_dir)
    out: list[dict[str, Any]] = []
    have_items: set[str] = set()
    for f in sorted(run_dir.rglob("*.items.json")):
        if "backend_traces" not in f.parts:
            continue
        role = f.parent.name
        have_items.add(str(f.parent))
        try:
            items = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            continue
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("type") == "commandExecution":
                out.append({"role": role, "command": str(it.get("command") or ""), "output": str(it.get("aggregated_output") or ""),
                            "exit_code": it.get("exit_code"), "source": str(f)})
    for f in sorted(run_dir.rglob("*.jsonl")):
        if "codex_home" not in f.parts or "sessions" not in f.parts:
            continue
        node_dir = str(f).split("/.sandbox/")[0]
        if node_dir in have_items:
            continue
        role = Path(node_dir).name
        pending: dict[str, dict[str, Any]] = {}
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                pl = (json.loads(line).get("payload") or {})
            except ValueError:
                continue
            if pl.get("type") == "function_call":
                cmds = A.commands_from_session(json.dumps({"payload": pl}))
                pending[str(pl.get("call_id"))] = {"role": role, "command": cmds[0] if cmds else "", "output": "",
                                                   "exit_code": None, "source": str(f)}
            elif pl.get("type") == "function_call_output" and str(pl.get("call_id")) in pending:
                o = pl.get("output")
                pending[str(pl.get("call_id"))]["output"] = o if isinstance(o, str) else json.dumps(o)
        out += list(pending.values())
    return out


def agent_calls(run_dir: Path) -> list[dict[str, Any]]:
    """Agent calls the manifest records (one entry per confined Codex start)."""
    m = Path(run_dir) / "sandbox_manifest.json"
    if not m.is_file():
        return []
    return [e for e in json.loads(m.read_text(encoding="utf-8")).get("entries") or [] if e.get("kind") == "agent"]


def classify_call(c: dict[str, Any], *, allowed: list[str], own_batch: str = "") -> list[tuple[str, str]]:
    """(event, target) pairs for one command and its output."""
    cmd, out = c.get("command") or "", c.get("output") or ""
    ws = ""
    ev: list[tuple[str, str]] = []
    kinds = A.classify(cmd, own_batch=own_batch)
    if "ld_preload_tamper" in kinds:
        ev.append(("ld_preload_tamper", ""))
    if _SANDBOX_TAMPER.search(cmd):
        ev.append(("sandbox_tamper", ""))
    if _FETCH.search(cmd) or "package_install" in kinds:
        ev.append(("package_fetch", ""))
    if _NETGUARD.search(out) or ("external_network" in kinds and re.search(r"refused|PermissionError|Errno 13|Errno 111", out)):
        ev.append(("network_denied", ""))
    elif "external_network" in kinds:
        ev.append(("unblocked_breach", "external network"))
    # a refusal fails the command; "Permission denied" inside a file that was read successfully is content
    denied = {(m.group(1) or m.group(2) or "") for m in _DENIED.finditer(out)} if c.get("exit_code") not in (0, "0") else set()
    targets = [p for p in _paths_in(cmd) if _outside(p, allowed, ws)]
    if _LIST.search(cmd) and targets:
        for t in sorted(set(targets)):
            ev.append(("list_denied", _dir_only(t)))
    for d in sorted(x for x in denied if x):
        if _outside(d, allowed, ws):
            ev.append(("read_denied", _dir_only(d)))
    if denied and not any(e[0] in ("read_denied", "list_denied") for e in ev):
        ev.append(("read_denied", ""))
    # out of bounds and not refused: a reading command on a forbidden path that exited 0 with output and no denial
    forb = [k for k in kinds if k.startswith("forbidden_path")]
    if forb and not denied and _READ_VERB.search(cmd) and c.get("exit_code") in (0, "0") and out.strip() \
            and any(_outside(p, allowed, ws) and os.path.exists(p) for p in _paths_in(cmd)):
        ev.append(("unblocked_breach", forb[0].split(":", 1)[-1]))
    return ev


def scan_run(run_dir: Path, *, run_id: str = "", write: bool = True) -> dict[str, Any]:
    """Classify a finished run, write its breach log and the global index; returns the summary."""
    from orchestra.memory.runtime import canary_hits

    run_dir = Path(run_dir)
    run_id = run_id or str(run_dir)
    allowed = _allowed_roots(run_dir)
    events: list[dict[str, Any]] = []
    cs = calls(run_dir)

    def add(role: str, event: str, target: str, command: str) -> None:
        events.append({"time": _now(), "run_id": run_id, "role": role, "event": event, "level": LEVEL[event],
                       "target": target, "command": command[:200]})

    seen = set()
    for c in cs:
        for event, target in classify_call(c, allowed=allowed, own_batch=str(run_dir.parent)):
            key = (c["role"], event, target, c["command"][:200])
            if key not in seen:
                seen.add(key)
                add(c["role"], event, target, c["command"])
    for hit in canary_hits(run_dir):
        if Path(hit).name in (LOG_NAME, "SANDBOX_VOID"):
            continue
        add("-", "canary", _dir_only(str(hit)), "canary found in a run file")
    for e in agent_calls(run_dir):
        # per call: its turn items or its session log must exist, else the call cannot be audited
        rid = str(e.get("request_id") or "")
        priv = Path(str(e.get("private_dir") or ""))
        has_session = (bool(str(priv)) and priv.is_dir() and any(priv.rglob("rollout-*.jsonl"))) or \
            (bool(rid) and any(run_dir.rglob(f".sandbox/{rid}/codex_home/sessions/**/rollout-*.jsonl")))   # run moved since
        has_items = bool(rid) and any(run_dir.rglob(f"{rid}.items.json"))
        if not has_session and not has_items:
            add(str(e.get("node_id") or e.get("role") or "-"), "capture_empty", "", f"agent call {rid or '?'}: no turn items, no session log")
    if write:
        log = run_dir / LOG_NAME
        with log.open("w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        INDEX.parent.mkdir(parents=True, exist_ok=True)
        with INDEX.open("a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    s = summary_of(events, runs=1, commands=len(cs))
    s["run_id"] = run_id
    s["void"] = s["critical"] > 0
    s["manual_review"] = s["warning"] >= REVIEW_WARNINGS
    if write and s["void"]:
        mark_void(run_dir, run_id, [e for e in events if e["level"] == "CRITICAL"])
    return s


def summary_of(events: Iterable[dict[str, Any]], *, runs: int, commands: int) -> dict[str, Any]:
    evs = list(events)
    warn = [e for e in evs if e["level"] == "WARNING"]
    crit = [e for e in evs if e["level"] == "CRITICAL"]
    return {"runs": runs, "commands": commands, "warning": len(warn), "critical": len(crit),
            "warning_by_event": dict(Counter(e["event"] for e in warn)), "critical_events": crit}


def mark_void(run_dir: Path, run_id: str, critical: list[dict[str, Any]]) -> None:
    VOID.parent.mkdir(parents=True, exist_ok=True)
    with VOID.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"time": _now(), "run_id": run_id, "run_dir": str(Path(run_dir).resolve()),
                             "critical": len(critical)}) + "\n")
    (Path(run_dir) / "SANDBOX_VOID").write_text(json.dumps(critical, indent=1), encoding="utf-8")


def void_runs() -> set[str]:
    if not VOID.is_file():
        return set()
    out = set()
    for line in VOID.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["run_dir"])
        except (ValueError, KeyError):
            continue
    return out


def is_void(run_dir: str | Path, _cache: dict[str, Any] = {}) -> bool:   # noqa: B006 -- per-process cache
    """A run with a CRITICAL sandbox event: its results count nowhere."""
    if not run_dir:
        return False
    mtime = VOID.stat().st_mtime if VOID.is_file() else 0.0
    if _cache.get("mtime") != mtime:
        _cache.update(mtime=mtime, runs=void_runs())
    rd = str(Path(run_dir).resolve())
    return any(rd == v or rd.startswith(v.rstrip("/") + "/") or v.startswith(rd.rstrip("/") + "/") for v in _cache["runs"])


def report_section(summaries: list[dict[str, Any]]) -> str:
    """The "沙盒越界警告" section every task report carries (format: docs/sandbox_breach_log.md)."""
    runs = sum(s.get("runs", 1) for s in summaries)
    cmds = sum(s.get("commands", 0) for s in summaries)
    warn = Counter()
    for s in summaries:
        warn.update(s.get("warning_by_event") or {})
    crit = [e for s in summaries for e in s.get("critical_events") or []]
    review = [s["run_id"] for s in summaries if s.get("manual_review")]
    lines = ["## 沙盒越界警告", f"- 运行数：{runs}；命令总数：{cmds}",
             f"- WARNING：{sum(warn.values())} 条" + ("（" + "，".join(f"{k} {v}" for k, v in sorted(warn.items())) + "）" if warn else "")]
    lines.append(f"- CRITICAL：{len(crit)} 条" + ("" if crit else ""))
    for e in crit:
        lines.append(f"  - {e['run_id']} | {e['role']} | {e['event']} | {e['command'][:120]}")
    if review:
        lines.append("- 需人工复核（WARNING ≥ 5）：" + "，".join(review))
    if crit:
        lines.append("- 结论：有越界（以上运行作废，不计入任何统计）")
    elif warn:
        lines.append("- 结论：无越界成功的命令；WARNING 均被拦截，逐条见各运行的 sandbox_breach.log")
    else:
        lines.append("- 结论：无越界")
    return "\n".join(lines)


__all__ = ["INDEX", "LEVEL", "LOG_NAME", "VOID", "agent_calls", "calls", "classify_call", "is_void", "mark_void",
           "report_section", "scan_run", "summary_of", "void_runs"]
