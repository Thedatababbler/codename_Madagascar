"""Documented-behaviour inventory of a milestone (author-evolution spec §2.1-2.2).

One extraction call (temperature 0, prompt and reply on disk) lists the observable
behaviours the documents state for the milestone's public symbols, each with a verbatim
quote. The program then keeps only items whose quote is a document substring (after
whitespace / punctuation normalisation) and whose symbol the milestone owns, and decides
the tier from ``configs/author/tier_rules.yaml``; the extractor's own tier is kept only as
a reference. Nothing here reads the held-out suite or the reference implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from orchestra.codeprojecteval.public_symbols import derive_public_symbols
from orchestra.codeprojecteval.suite_audit import normalise

TIER_RULES_PATH = Path("configs/author/tier_rules.yaml")
KINDS = ("main_path", "boundary", "state_transition", "error_path", "integration", "protocol")
INVENTORY_MODEL_ENV = "ADAMAS_AUTHOR_INVENTORY_MODEL"
EXTRACTOR_VERSION = "inv-v1"

SYSTEM = """You list the observable behaviours a project's design documents state, for one milestone.
You never invent behaviour: every item quotes one sentence (or a contiguous part of one) of the
documents word for word. Output JSON only."""

PROMPT = """Milestone `{milestone_id}`: {objective}

Acceptance criteria:
{criteria}

Focus paths: {focus}

Public symbols this milestone owns (only these may appear as `symbol`):
{symbols}

List every observable behaviour the documents below state for these symbols: what a call
returns, which state changes, which exception it raises and when, what boundary or default
applies, how it interacts with another documented symbol. One item per behaviour.

Return a JSON array; each element:
{{"symbol": "<one owned symbol>", "quote": "<words copied exactly from the documents>",
  "kind": "main_path|boundary|state_transition|error_path|integration|protocol",
  "tier": "hard|soft", "tier_reason": "<why>"}}

"hard": the quote names a concrete input or condition AND a concrete expected result.
"soft": a general promise ("supports several formats").

=== Documents ===
{docs}
"""


@dataclass
class InventoryItem:
    item_id: str
    symbol: str
    quote: str
    kind: str
    tier: str
    tier_reason: str = ""
    extractor_tier: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Inventory:
    task: str
    milestone_id: str
    items: list[InventoryItem] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    owned_symbols: list[str] = field(default_factory=list)
    prompt_path: str = ""
    reply_path: str = ""
    model: str = ""
    version: str = EXTRACTOR_VERSION

    def hard(self) -> list[InventoryItem]:
        return [i for i in self.items if i.tier == "hard"]

    def soft(self) -> list[InventoryItem]:
        return [i for i in self.items if i.tier == "soft"]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [i.to_dict() for i in self.items]
        return d

    @classmethod
    def from_dict(cls, d: Mapping) -> Inventory:
        items = [InventoryItem(**i) for i in d.get("items") or []]
        rest = {k: v for k, v in d.items() if k != "items" and k in cls.__dataclass_fields__}
        return cls(items=items, **rest)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        return p


def load_inventory(path: str | Path) -> Inventory:
    return Inventory.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# --- tiers -----------------------------------------------------------------


def load_tier_rules(path: str | Path = TIER_RULES_PATH) -> dict[str, list[re.Pattern[str]]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {k: [re.compile(p, re.I) for p in (raw.get(k) or [])] for k in ("condition", "result", "soft_overrides")}


def decide_tier(quote: str, rules: Mapping[str, list[re.Pattern[str]]]) -> tuple[str, str]:
    """(tier, reason) by the rule table: hard needs a condition and a result."""
    has_cond = any(p.search(quote) for p in rules.get("condition", []))
    has_res = any(p.search(quote) for p in rules.get("result", []))
    if any(p.search(quote) for p in rules.get("soft_overrides", [])):
        return "soft", "general promise"
    if has_cond and has_res:
        return "hard", "condition and result"
    if not has_cond and not has_res:
        return "soft", "neither condition nor result"
    return "soft", "no condition" if not has_cond else "no result"


# --- checks ------------------------------------------------------------------


def quote_in_docs(quote: str, docs_norm: str) -> bool:
    q = normalise(quote)
    return len(q) >= 8 and q in docs_norm


def check_items(raw: Iterable[Mapping], *, docs: Mapping[str, str], owned: set[str],
                rules: Mapping[str, list[re.Pattern[str]]]) -> tuple[list[InventoryItem], list[dict]]:
    """Keep items whose quote is in the documents and whose symbol is owned; tier by rule."""
    docs_norm = normalise(" ".join(docs.values()))
    owned_leaf = {s.split(".")[-1] for s in owned} | owned
    kept: list[InventoryItem] = []
    dropped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for n, r in enumerate(raw, 1):
        sym = str(r.get("symbol") or "").strip().strip("`")
        quote = " ".join(str(r.get("quote") or "").split())
        kind = str(r.get("kind") or "main_path").strip()
        if not quote_in_docs(quote, docs_norm):
            dropped.append({"symbol": sym, "quote": quote[:200], "reason": "quote_not_in_documents"})
            continue
        if sym not in owned and sym.split(".")[-1] not in owned_leaf:
            dropped.append({"symbol": sym, "quote": quote[:200], "reason": "symbol_not_owned"})
            continue
        key = (sym, normalise(quote))
        if key in seen:
            continue
        seen.add(key)
        tier, reason = decide_tier(quote, rules)
        kept.append(InventoryItem(
            item_id=f"I{len(kept) + 1:03d}", symbol=sym, quote=quote,
            kind=kind if kind in KINDS else "main_path", tier=tier, tier_reason=reason,
            extractor_tier=str(r.get("tier") or "")))
    return kept, dropped


def parse_reply(text: str) -> list[dict]:
    t = text.strip()
    m = re.search(r"\[.*\]", t, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)]


# --- extraction --------------------------------------------------------------


def default_call(system: str, prompt: str, *, timeout: float = 900.0) -> tuple[str, str]:
    """OpenAI-compatible chat completion at temperature 0 (the planner's and evolver's route)."""
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("OPENAI_API_KEY and OPENAI_BASE_URL are required for inventory extraction")
    from openai import OpenAI

    model = os.getenv(INVENTORY_MODEL_ENV) or os.getenv("CODEX_MODEL") or os.getenv("SMOLAGENTS_MODEL") or "gpt-5.4"
    client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"), timeout=timeout)
    response = client.chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip(), model


def render_prompt(*, milestone_id: str, objective: str, criteria: Iterable[str], focus: Iterable[str],
                  owned: Iterable[str], docs: Mapping[str, str]) -> str:
    return PROMPT.format(
        milestone_id=milestone_id, objective=objective,
        criteria="\n".join(f"- {c}" for c in criteria) or "- (none)",
        focus=", ".join(f"`{p}`" for p in focus) or "(none)",
        symbols="\n".join(f"- `{s}`" for s in sorted(owned)) or "- (none derived)",
        docs="\n\n".join(f"--- {name} ---\n{text}" for name, text in docs.items()),
    )


def extract_inventory(*, task: str, milestone_id: str, objective: str, criteria: Iterable[str],
                      focus_paths: Iterable[str], docs: Mapping[str, str], out_dir: str | Path,
                      call: Callable[[str, str], tuple[str, str]] | None = None,
                      rules_path: str | Path = TIER_RULES_PATH) -> Inventory:
    """Extract, check and tier; prompt, reply and result written under ``out_dir``. Cached by
    content: the same documents, milestone and extractor version reuse the saved inventory."""
    focus = list(focus_paths)
    sym_inv = derive_public_symbols(docs)
    owned = sym_inv.owned_by(focus)
    prompt = render_prompt(milestone_id=milestone_id, objective=objective, criteria=list(criteria),
                           focus=focus, owned=owned, docs=docs)
    digest = hashlib.sha1((EXTRACTOR_VERSION + prompt).encode()).hexdigest()[:12]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    final = out / f"{milestone_id}.inventory.json"
    if final.is_file():
        cached = load_inventory(final)
        if Path(cached.prompt_path).name == f"{milestone_id}.{digest}.prompt.txt":
            return cached
    prompt_path = out / f"{milestone_id}.{digest}.prompt.txt"
    reply_path = out / f"{milestone_id}.{digest}.reply.txt"
    prompt_path.write_text(SYSTEM + "\n\n" + prompt, encoding="utf-8")
    reply, model = (call or default_call)(SYSTEM, prompt)
    reply_path.write_text(reply, encoding="utf-8")
    items, dropped = check_items(parse_reply(reply), docs=docs, owned=owned, rules=load_tier_rules(rules_path))
    inv = Inventory(task=task, milestone_id=milestone_id, items=items, dropped=dropped, owned_symbols=sorted(owned),
                    prompt_path=str(prompt_path), reply_path=str(reply_path), model=model)
    inv.save(final)
    return inv


def render_for_author(inv: Inventory) -> str:
    """The inventory block appended to the author prompt (spec §2.3)."""
    if not inv.items:
        return ""
    lines = ["## Documented behaviours to cover", "",
             "Each item below quotes the documents. Every **required** item needs at least one test whose "
             "citation comment quotes that sentence (or the part of it the test checks). Optional items may be "
             "tested; they never decide the milestone.", "", "Required:"]
    for i in inv.hard():
        lines.append(f"- [{i.item_id}] ({i.kind}) `{i.symbol}`: \"{i.quote}\"")
    if inv.soft():
        lines += ["", "Optional:"]
        for i in inv.soft():
            lines.append(f"- [{i.item_id}] ({i.kind}) `{i.symbol}`: \"{i.quote}\"")
    return "\n".join(lines)


__all__ = [
    "EXTRACTOR_VERSION", "Inventory", "InventoryItem", "KINDS", "check_items", "decide_tier", "extract_inventory",
    "load_inventory", "load_tier_rules", "parse_reply", "quote_in_docs", "render_for_author", "render_prompt",
]
