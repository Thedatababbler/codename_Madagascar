"""Milestone public contract tests derived from public_design (+ optional LLM).

Contracts are runner-owned, frozen into the workspace, and never read hidden
tests. LLM enrichment is optional and fail-closed to a deterministic baseline.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from orchestra.realbench.public_harness import (
    parse_expected_modules,
    parse_package_exports,
)

CONTRACTS_NAME = "adamas_milestone_contracts.json"
CONTRACT_TEST_NAME = "tests_public/test_milestone_contracts.py"
ALLOWED_CHECK_TYPES = frozenset(
    {"import", "export", "callable_or_class", "module_file_exists"}
)
Role = Literal["discovery", "implementation", "integration"]


def _load_public_design(workspace: Path) -> tuple[list[str], dict[str, list[str]], list[str]]:
    tree_path = workspace / "public_design" / "tree.txt"
    pkg_path = workspace / "public_design" / "package.json"
    tree = (
        tree_path.read_text(encoding="utf-8", errors="ignore")
        if tree_path.is_file()
        else ""
    )
    package_json: dict[str, Any] = {}
    if pkg_path.is_file():
        try:
            raw = json.loads(pkg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                package_json = raw
        except json.JSONDecodeError:
            package_json = {}
    modules = parse_expected_modules(tree)
    exports = parse_package_exports(package_json)
    packages: list[str] = []
    seen: set[str] = set()
    for mod in modules:
        root = mod.split(".", 1)[0]
        if root not in seen:
            seen.add(root)
            packages.append(root)
    return modules, exports, packages


def build_deterministic_contracts(
    workspace: Path,
    *,
    role: Role,
    focus_paths: list[str] | None = None,
    milestone_id: str | None = None,
) -> dict[str, Any]:
    """Build frozen public checks for one milestone role."""
    modules, exports, packages = _load_public_design(workspace)
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
        checks.append(
            {
                "type": "module_file_exists",
                "path": f"{pkg}/__init__.py",
                "required_levels": ["discovery", "implementation", "integration"],
            }
        )
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
        "Allowed check types only: import, export, callable_or_class, "
        "module_file_exists.\n"
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


def _contract_pytest_source() -> str:
    return '''"""Runner-owned milestone contracts (public_design only; do not edit)."""

from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_PATH = ROOT / "adamas_milestone_contracts.json"
LEVEL = __import__("os").environ.get("ADAMAS_PUBLIC_CHECK_LEVEL", "integration")


def _load() -> dict:
    if not CONTRACTS_PATH.is_file():
        return {"checks": []}
    return json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))


def _active(check: dict) -> bool:
    levels = check.get("required_levels") or ["integration"]
    return LEVEL in levels


@pytest.mark.parametrize("check", [c for c in _load().get("checks", []) if _active(c)])
def test_milestone_contract(check: dict) -> None:
    ctype = check.get("type")
    if ctype == "module_file_exists":
        assert (ROOT / check["path"]).exists(), check
        return
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if ctype == "import":
        importlib.import_module(check["module"])
        return
    mod = importlib.import_module(check["module"])
    sym = check["symbol"]
    assert hasattr(mod, sym), f"missing {check['module']}.{sym}"
    obj = getattr(mod, sym)
    if ctype == "callable_or_class":
        assert inspect.isclass(obj) or callable(obj), f"{sym} not callable/class"
'''


def materialize_milestone_contracts(
    workspace: Path,
    *,
    role: Role,
    focus_paths: list[str] | None = None,
    milestone_id: str | None = None,
    enable_llm: bool | None = None,
) -> dict[str, Any]:
    """Write milestone contract JSON + pytest module into workspace."""
    contracts = build_deterministic_contracts(
        workspace,
        role=role,
        focus_paths=focus_paths,
        milestone_id=milestone_id,
    )
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

    ws = Path(workspace)
    (ws / CONTRACTS_NAME).write_text(
        json.dumps(contracts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tests_dir = ws / "tests_public"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "__init__.py").touch()
    (ws / CONTRACT_TEST_NAME).write_text(_contract_pytest_source(), encoding="utf-8")
    # Cheap syntax validation of generated test module.
    ast.parse((ws / CONTRACT_TEST_NAME).read_text(encoding="utf-8"))
    return contracts
