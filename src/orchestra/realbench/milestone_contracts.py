"""Milestone public contract checks derived from public_design (+ optional LLM).

Contracts are runner-owned JSON kept **outside** the agent workspace and handed
to the harness by absolute path; they never read hidden tests. LLM enrichment is
optional and fail-closed to a deterministic baseline.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from orchestra.realbench.public_harness import export_candidates, load_public_design

CONTRACTS_SUFFIX = ".contracts.json"
ALLOWED_CHECK_TYPES = frozenset(
    {"import", "export", "export_any", "callable_or_class", "module_file_exists"}
)
Role = Literal["discovery", "implementation", "integration"]


def contracts_path_for(harness_dir: Path, milestone_id: str) -> Path:
    """Runner-side path holding one milestone's frozen contract checks."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", milestone_id) or "milestone"
    return Path(harness_dir) / f"{safe}{CONTRACTS_SUFFIX}"


def _scaffold_check(root: str, modules: list[str]) -> dict[str, Any]:
    """Existence check that respects single-file modules vs packages.

    A public tree entry such as ``SnoopR.py`` must not be turned into a
    ``SnoopR/__init__.py`` requirement: forcing a package there makes agents
    invent a directory layout the hidden evaluation never overlays.
    """
    is_package = any(mod.startswith(root + ".") for mod in modules)
    path = f"{root}/__init__.py" if is_package else f"{root}.py"
    return {
        "type": "module_file_exists",
        "path": path,
        "required_levels": ["discovery", "implementation", "integration"],
    }


_LEVEL_ORDER = ("discovery", "implementation", "integration")


def _merge_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse checks that target the same symbol, unioning required levels."""
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for check in checks:
        key = (
            str(check.get("type") or ""),
            str(
                check.get("module")
                or check.get("path")
                or ",".join(check.get("modules") or [])
            ),
            str(check.get("symbol") or ""),
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(check)
            continue
        levels = set(existing.get("required_levels") or []) | set(
            check.get("required_levels") or []
        )
        existing["required_levels"] = [x for x in _LEVEL_ORDER if x in levels]
    return list(merged.values())


def build_deterministic_contracts(
    workspace: Path,
    *,
    role: Role,
    focus_paths: list[str] | None = None,
    milestone_id: str | None = None,
) -> dict[str, Any]:
    """Build frozen public checks for one milestone role."""
    modules, exports, packages = load_public_design(Path(workspace))
    focus = [str(p).replace("\\", "/").strip("/") for p in (focus_paths or []) if p]
    focus_mods = modules
    if focus:
        narrowed: list[str] = []
        for mod in modules:
            mod_path = mod.replace(".", "/")
            if any(
                mod == f
                or mod.startswith(f + ".")
                or mod_path.startswith(f.rstrip("/") + "/")
                or f.rstrip("/") in {mod, packages[0] if packages else ""}
                or mod.split(".", 1)[0] == f.split("/", 1)[0]
                for f in focus
            ):
                narrowed.append(mod)
        if narrowed:
            focus_mods = narrowed

    checks: list[dict[str, Any]] = []
    # Discovery: files/scaffold existence for focused packages.
    for pkg in packages[:12]:
        checks.append(_scaffold_check(pkg, modules))
    for mod in focus_mods[:40]:
        checks.append(
            {
                "type": "import",
                "module": mod,
                "required_levels": ["implementation", "integration"],
            }
        )
        short = mod.rsplit(".", 1)[-1]
        wanted = exports.get(short) or exports.get(mod) or []
        if not wanted:
            continue
        # Candidates come from the full tree: narrowing to the focused module
        # would re-impose the symbol on a same-named sibling that never owned it.
        candidates = export_candidates(modules, short if short in exports else mod)
        if len(candidates) > 1:
            for sym in wanted[:20]:
                checks.append(
                    {
                        "type": "export_any",
                        "modules": candidates,
                        "symbol": sym,
                        "required_levels": ["integration"],
                    }
                )
            continue
        for sym in wanted[:20]:
            checks.append(
                {
                    "type": "callable_or_class",
                    "module": mod,
                    "symbol": sym,
                    "required_levels": ["integration"],
                }
            )
            checks.append(
                {
                    "type": "export",
                    "module": mod,
                    "symbol": sym,
                    "required_levels": ["integration"],
                }
            )

    return {
        "schema_version": "1.0",
        "milestone_id": milestone_id or role,
        "role": role,
        "focus_paths": focus,
        "source": "public_design",
        "generator": "deterministic",
        "checks": checks,
    }


def _sanitize_llm_checks(raw_checks: list[Any]) -> list[dict[str, Any]]:
    """Keep only AST-safe public contract check shapes."""
    out: list[dict[str, Any]] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("type") or "").strip()
        if ctype not in ALLOWED_CHECK_TYPES:
            continue
        cleaned: dict[str, Any] = {"type": ctype}
        if ctype == "module_file_exists":
            path = str(item.get("path") or "").replace("\\", "/")
            if not path or path.startswith("/") or ".." in path.split("/"):
                continue
            cleaned["path"] = path
        elif ctype == "export_any":
            mods = [
                m
                for m in (str(x).strip() for x in (item.get("modules") or []))
                if re.match(r"^[A-Za-z_][A-Za-z0-9_\.]*$", m)
            ]
            sym = str(item.get("symbol") or "").strip()
            if not mods or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", sym):
                continue
            cleaned["modules"] = mods[:8]
            cleaned["symbol"] = sym
        else:
            mod = str(item.get("module") or "").strip()
            if not mod or not re.match(r"^[A-Za-z_][A-Za-z0-9_\.]*$", mod):
                continue
            cleaned["module"] = mod
            if ctype in {"export", "callable_or_class"}:
                sym = str(item.get("symbol") or "").strip()
                if not sym or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", sym):
                    continue
                cleaned["symbol"] = sym
        levels = item.get("required_levels") or ["integration"]
        if not isinstance(levels, list):
            levels = ["integration"]
        cleaned["required_levels"] = [
            str(x)
            for x in levels
            if str(x) in {"discovery", "implementation", "integration"}
        ] or ["integration"]
        out.append(cleaned)
    return out[:80]


def maybe_enrich_contracts_with_llm(
    contracts: dict[str, Any],
    *,
    workspace: Path,
    role: Role,
) -> dict[str, Any]:
    """Optionally ask an LLM for extra public checks; never invent hidden tests.

    Enabled only when ``ADAMAS_REALBENCH_LLM_CONTRACTS=1``. Failures fall back
    to the deterministic contract unchanged.
    """
    flag = os.getenv("ADAMAS_REALBENCH_LLM_CONTRACTS", "").strip().lower()
    if flag not in {"1", "true", "yes"}:
        return contracts
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not api_key or not base_url:
        return contracts

    req_path = workspace / "REQUIREMENTS.md"
    requirements = (
        req_path.read_text(encoding="utf-8", errors="replace")[:6000]
        if req_path.is_file()
        else ""
    )
    prompt = (
        "Generate additional PUBLIC repository contract checks as JSON list.\n"
        "Allowed check types only: import, export, export_any "
        '(fields: modules[], symbol), callable_or_class, module_file_exists.\n'
        "Do NOT invent hidden tests, private APIs, or filesystem shell commands.\n"
        f"Milestone role: {role}\n"
        f"Existing checks: {json.dumps(contracts.get('checks')[:20], ensure_ascii=False)}\n"
        f"Requirements excerpt:\n{requirements}\n"
        "Return ONLY a JSON array of check objects."
    )
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))
        model = os.getenv("ADAMAS_REALBENCH_CONTRACT_MODEL") or os.getenv(
            "CODEX_MODEL", "gpt-5.4"
        )
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": "You emit only JSON arrays of public contract checks.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
        # Extract JSON array if fenced.
        match = re.search(r"\[[\s\S]*\]", content)
        payload = json.loads(match.group(0) if match else content)
        if not isinstance(payload, list):
            return contracts
        extra = _sanitize_llm_checks(payload)
        if not extra:
            return contracts
        merged = dict(contracts)
        merged["generator"] = "deterministic+llm"
        merged["checks"] = list(contracts.get("checks") or []) + extra
        return merged
    except Exception:  # noqa: BLE001 — fail closed to deterministic contracts
        return contracts


def materialize_milestone_contracts(
    workspace: Path,
    *,
    harness_dir: Path,
    role: Role,
    focus_paths: list[str] | None = None,
    milestone_id: str | None = None,
    enable_llm: bool | None = None,
    extra_checks: list[dict[str, Any]] | None = None,
    acceptance_criteria: list[str] | None = None,
    corner_cases: list[str] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Write one milestone's contract JSON into the runner-side harness dir.

    ``extra_checks`` carries planner-authored acceptance checks; they are merged
    on top of the deterministic public_design baseline so a dynamic milestone can
    pin the exact contract its downstream milestones depend on. Nothing is
    written into ``workspace`` — it is read for public_design only.
    """
    contracts = build_deterministic_contracts(
        workspace,
        role=role,
        focus_paths=focus_paths,
        milestone_id=milestone_id,
    )
    if extra_checks:
        from orchestra.realbench.milestone_planner import sanitize_contract_checks

        contracts["checks"] = _merge_checks(
            list(contracts.get("checks") or []) + sanitize_contract_checks(extra_checks)
        )
        contracts["generator"] = "deterministic+planner"
    if acceptance_criteria:
        contracts["acceptance_criteria"] = [str(x) for x in acceptance_criteria][:20]
    if corner_cases:
        contracts["corner_cases"] = [str(x) for x in corner_cases][:20]
    if enable_llm is None:
        enable_llm = os.getenv("ADAMAS_REALBENCH_LLM_CONTRACTS", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
    if enable_llm:
        contracts = maybe_enrich_contracts_with_llm(
            contracts, workspace=workspace, role=role
        )

    out_dir = Path(harness_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = contracts_path_for(out_dir, milestone_id or role)
    path.write_text(
        json.dumps(contracts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return contracts, path
