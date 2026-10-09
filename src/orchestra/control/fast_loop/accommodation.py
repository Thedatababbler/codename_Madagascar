"""Does a repair fix tests by accommodating how they are built? (author fake-object fix §6.4)

A case the repair turns from failing to passing is *accommodated* when the code
change that fixed it only changes how the program calls a dependency:
- a keyword or argument added or dropped;
- another way to call an external command or library;
- PATH / executable lookup or environment handling;
- ``hasattr`` probing, or an ``except TypeError`` fallback around a call.

The observable result stays the same. Such cases are routed to the author as
``suite_suspect``, leave the gate score and the unified acceptance, and the
repair does not reach the transfer channel.

Program rules first, on the repair diff's changed lines:
- every changed line is a dependency-call line -> accommodated (all fixed cases);
- no changed line touches a dependency call -> not accommodated;
- a mix -> one temperature-0 model call decides per case (``call``; without one
  the mix stays undecided and nothing is marked).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

DEP_CALL = re.compile(
    r"\b(requests|urllib\d?|urllib3|httpx|socket|ssl|subprocess|shutil|asyncio|smtplib|imaplib|ftplib|telnetlib|select)\.\w+"
    r"|\bos\.(system|popen|exec\w*|spawn\w*|environ|getenv|putenv|pathsep|get_exec_path)\b"
    r"|\bshutil\.which\b|\bPATH\b|\bexecutable\b|/usr/bin/env|\bhasattr\(|\bexcept\s+TypeError\b|\*\*kwargs\b|\binspect\.signature\b"
    r"|\b(settimeout|setsockopt|makefile|sendall|recv|iter_content|raise_for_status)\b"
)
_TRIVIAL = re.compile(r"^\s*(#.*)?$|^\s*(import|from)\s+\S+|^\s*[)\]}],?\s*$|^\s*(try|else|finally):\s*$|^\s*pass\s*$")


@dataclass
class Judgement:
    verdict: str                      # accommodated | not_accommodated | mixed | undecided
    rule: str
    accommodated: list[str] = field(default_factory=list)
    model_answer: dict | None = None

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "rule": self.rule, "accommodated": self.accommodated, "model_answer": self.model_answer}


def changed_lines(patch: str, *, skip_tests: bool = True) -> list[str]:
    out, current = [], ""
    for ln in (patch or "").splitlines():
        if ln.startswith("diff --git "):
            current = ln.split(" b/", 1)[-1]
            continue
        if skip_tests and re.search(r"(^|/)(tests?|spec_tests|check_tests)/|(^|/)test_[^/]*\.py$|conftest\.py$", current):
            continue
        if (ln.startswith("+") and not ln.startswith("+++")) or (ln.startswith("-") and not ln.startswith("---")):
            body = ln[1:]
            if not _TRIVIAL.match(body):
                out.append(body.strip())
    return out


def rule_verdict(patch: str) -> tuple[str, str]:
    lines = changed_lines(patch)
    if not lines:
        return "not_accommodated", "no source change"
    dep = [ln for ln in lines if DEP_CALL.search(ln)]
    if not dep:
        return "not_accommodated", "no changed line touches a dependency call"
    if len(dep) == len(lines):
        return "accommodated", f"all {len(lines)} changed lines are dependency-call lines"
    return "mixed", f"{len(dep)} of {len(lines)} changed lines touch dependency calls"


SYSTEM = "You judge whether code repairs fix tests by real behaviour or by accommodating how the tests are built. Answer with JSON only."


def judge(patch: str, fixed_cases: Iterable[str], *, failures: dict[str, str] | None = None,
          call: Callable[[str, str], str] | None = None, max_patch: int = 9000) -> Judgement:
    cases = sorted(set(fixed_cases))
    verdict, why = rule_verdict(patch)
    if verdict == "accommodated":
        return Judgement(verdict, why, cases)
    if verdict == "not_accommodated" or not cases:
        return Judgement("not_accommodated", why)
    if call is None:
        return Judgement("undecided", why + "; no model call available")
    prompt = (
        "A repair made these failing tests pass:\n" + "\n".join(f"- {c}: {(failures or {}).get(c, '')[:300]}" for c in cases)
        + f"\n\nRepair diff (excerpt):\n```diff\n{patch[:max_patch]}\n```\n\n"
        "For each test, decide whether the change that fixed it ONLY changes how the program calls a dependency "
        "(adds or drops an argument or keyword, switches to another way of calling an external command or library, "
        "adjusts PATH / executable lookup / environment handling, probes attributes) without changing any result "
        "the program returns, writes or raises. Such a fix accommodates the test's own fake objects.\n"
        'Return {"accommodated": [<test ids>], "reason": "..."}'
    )
    raw = call(SYSTEM, prompt)
    try:
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except (AttributeError, ValueError):
        return Judgement("undecided", why + "; model answer not JSON", model_answer={"raw": raw[:500]})
    acc = [c for c in (data.get("accommodated") or []) if c in cases]
    return Judgement("accommodated" if acc else "not_accommodated", why + "; model decided", acc, model_answer=data)


__all__ = ["DEP_CALL", "Judgement", "changed_lines", "judge", "rule_verdict"]
