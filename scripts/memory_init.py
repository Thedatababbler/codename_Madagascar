#!/usr/bin/env python3
"""Create the memory store and migrate the existing tables into it (memory spec §1, §2.4, §5.5, §7).

- repair bank: every row of configs/playbook_v2/repair.yaml becomes a pattern
  (instruction inlined from instructions/<row_id>.md, state and statistics kept);
- author bank: every entry of the published rules document (configs/author/rules_doc/current);
- first-pass bank: pitfalls and patterns start empty (only the transfer channel
  writes them); no earlier first-pass entry is migrated;
- categories: E1-E9 (+ any) for repair, the six inventory kinds for the author,
  and for the first pass the six drafts of spec §5.5. Those are written only
  with ``--with-draft-categories``; otherwise the file holds no category.
- canaries: one in every CHANGELOG.yaml, one in a retired entry of every bank.

Every entry goes through the validator. The store is created once; an existing
store is left alone unless ``--force`` is given.

    uv run python scripts/memory_init.py [--with-draft-categories] [--force]
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestra.control.fast_loop.playbook_v2 import (  # noqa: E402
    REPAIR_TABLE_PATH,
    instruction_text,
    load_repair_table,
)
from orchestra.memory.store import BANKS, current_version, take_snapshot, write_yaml  # noqa: E402
from orchestra.memory.validate import build_identifiers, validate_entries  # noqa: E402

DOMAIN_TAGS = ["network_protocol", "cli_tool", "file_format", "web_framework", "data_validation", "template_engine", "storage"]

FIRST_PASS_DRAFT = [
    {"category_id": "FP-TYPE", "name": "协议或二进制数据的返回类型",
     "definition": "文档描述了返回元组、列表或字段，但没有说明元素是字节还是字符串；实现者需要决定协议或二进制数据的原始类型",
     "signals": ["阶段实现网络协议、二进制格式或底层数据的客户端/解析器", "文档中出现“返回 (x, y) 元组”“返回列表”而未说明元素类型"]},
    {"category_id": "FP-RAW-VS-STATUS", "name": "返回服务器原文还是状态值",
     "definition": "一个操作完成后，应返回对端给出的响应文本/数据，还是一个通用的状态值（如 OK、True）",
     "signals": ["文档写“返回服务器的响应”“返回命令结果”", "操作对应一次请求-响应交互"]},
    {"category_id": "FP-RESP-FILTER", "name": "响应内容的过滤规则",
     "definition": "返回的响应集合应包含哪些行或条目：例如只要未标记的推送行，不要表示命令结束的标记行",
     "signals": ["文档区分 tagged/untagged、完成行/数据行、头部/正文等不同种类的响应", "方法返回“收集到的响应”"]},
    {"category_id": "FP-DOC-EXC", "name": "文档规定的异常类型",
     "definition": "文档为某种失败情况点名了具体的异常类，实现必须抛出这个类，而不是相近的或通用的异常",
     "signals": ["文档写“若……则抛出 XxxException/XxxError”", "同一模块定义了多个含义相近的异常类"]},
    {"category_id": "FP-EXT-CALL", "name": "调用外部命令与第三方库的方式",
     "definition": "实现需要调用外部命令或第三方库的函数时，调用的形式（参数、关键字、执行方式）会影响行为",
     "signals": ["阶段需要执行外部程序（版本控制、压缩工具等）", "阶段需要用第三方网络或文件库下载、读取数据"]},
    {"category_id": "FP-INPUT-FORMS", "name": "输入形式的完整覆盖",
     "definition": "文档列出了同一入口可以接受的多种输入形式，实现需要全部支持，而不只是最简单的一种",
     "signals": ["文档写“可以是本地路径、URL 或缩写”之类的并列输入形式", "一个入口函数要先判断输入属于哪一类再分派"]},
]

REPAIR_CATEGORIES = [
    ("RP-E1", "未完成", "智能体超时或步数用尽、焦点文件缺失、代码中留有未实现的占位"),
    ("RP-E2", "导入与接口面", "contracts / cross_imports 阶段失败，ImportError / AttributeError，参数个数错误"),
    ("RP-E3", "主路径行为", "主路径行为不对（默认类别）"),
    ("RP-E4", "状态转换", "一串操作后对文档规定的状态转换的断言失败"),
    ("RP-E5", "错误路径", "期望抛出异常的断言未触发，或抛出的异常类型不对"),
    ("RP-E6", "边界与对象协议", "空输入、__eq__ / __hash__ / __iter__ / __repr__ 等"),
    ("RP-E7", "跨模块集成", "用例引用了两个以上模块的符号"),
    ("RP-E8", "性能", "单用例超时"),
    ("RP-E9", "分散或停滞", "三类以上同时出现，或同一类两次无进展"),
    ("RP-ANY", "任意类别", "适用于任何错误类别的通用修复模式"),
]

AUTHOR_CATEGORIES = [
    ("AU-MAIN_PATH", "主路径", "行为清单中类型为主路径的条目"),
    ("AU-BOUNDARY", "边界", "行为清单中类型为边界的条目"),
    ("AU-STATE_TRANSITION", "状态转换", "行为清单中类型为状态转换的条目"),
    ("AU-ERROR_PATH", "异常", "行为清单中类型为异常路径的条目"),
    ("AU-INTEGRATION", "组合", "行为清单中类型为组合 / 集成的条目"),
    ("AU-PROTOCOL", "协议", "行为清单中类型为协议的条目"),
]

SKILLS = {
    "first_pass": """# 首轮记忆：判断者的思考流程

你要为一个即将开始实现的里程碑，从类别目录中选出可能适用的“坑点类别”。程序会按你选出的类别召回过去的经验，交给实现者。

## 1. 先读什么
1. 里程碑的标题、目标、文件与验收标准；
2. 下方给出的相关文档段落（只包含提到本里程碑文件的段落）。

## 2. 怎么对照类别目录
1. 逐个类别读它的定义与判断信号；
2. 对每个类别，在里程碑描述和文档段落中找是否有对应的信号；有明确信号才选，不要凭领域印象选；
3. 同时从领域标签词表中选出描述本里程碑领域的标签（可以为空，必须来自词表）；
4. 至多选出规定数量的类别；没有适用的类别时，selected 为空列表。

## 3. 输出格式
只输出一个 JSON 对象：
{"domain_tags": [...], "selected": [{"category_id": "...", "reason": "一句话，指出对应的信号", "confidence": "high|medium|low"}], "skill_version": "<本文末尾 skill_version 行的值>"}

## 4. 禁止事项
- 不得编造类别：category_id 必须逐字来自类别目录；
- 不得输出类别目录以外的内容，不得输出 JSON 以外的文字；
- 不得复述或猜测任何测试、参考实现的内容。
""",
    "repair": """# 修复记忆：判断者的思考流程

现有的错误分类规则没能给这次持续失败定出类别。你要根据失败用例和里程碑描述，从修复类别目录中选出最可能的错误类别，程序会据此召回修复模式。

## 1. 先读什么
1. 里程碑目标与文件；
2. 持续失败的用例名（只有名称，没有源码）。

## 2. 怎么对照类别目录
1. 逐个类别读定义；
2. 从用例名与里程碑描述判断失败属于哪一类；无法区分时选 RP-E3（主路径行为，默认类别）；
3. 至多选出规定数量的类别。

## 3. 输出格式
只输出一个 JSON 对象：
{"domain_tags": [...], "selected": [{"category_id": "...", "reason": "...", "confidence": "high|medium|low"}], "skill_version": "<本文末尾 skill_version 行的值>"}

## 4. 禁止事项
- 不得编造类别，不得输出类别目录以外的内容，不得输出 JSON 以外的文字。
""",
    "author": """# 出题者记忆：说明

出题者记忆的类别由程序确定，不经过判断者：程序读取本里程碑行为清单中每条描述的类型（主路径、边界、状态转换、异常、组合、协议），
按这些类型召回出题经验，注入出题者提示词。若该里程碑没有行为清单，程序先生成清单；生成失败则报错停止。

若将来需要判断者，流程同其他记忆库：
1. 先读里程碑目标、验收标准与行为清单；
2. 逐个类别核对清单条目的类型；
3. 只输出 {"domain_tags": [...], "selected": [...], "skill_version": "..."}；
4. 不得编造类别，不得输出类别目录以外的内容。
""",
}


def canary(bank: str) -> str:
    return f"CANARY-{ {'first_pass': 'FP', 'repair': 'RP', 'author': 'AU'}[bank] }-{secrets.token_hex(8)}".replace(" ", "")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def repair_patterns() -> list[dict]:
    out = []
    ro = ("contract_critic", "spec_auditor", "behaviour_critic")
    for row in load_repair_table(REPAIR_TABLE_PATH):
        d = row.to_dict()
        classes = [c for c in (row.error_classes or ()) if c != "*"]
        cats = [f"RP-{c}" for c in classes] or ["RP-ANY"]
        role = next((e.role for e in row.slot_edits if getattr(e, "role", "") in ro), None)
        out.append({
            "pattern_id": row.row_id, "category_id": cats[0], "category_ids": cats,
            "action": {"instruction": instruction_text(row) or None, "add_readonly_role": role,
                       "template": row.target_template, "budget_delta": d.get("budget_delta")},
            "intent": row.intent, "row": d, "state": row.state,
            "evidence": {"migrated_from": "configs/playbook_v2/repair.yaml", "legacy_id": row.legacy_id or None},
            "origin": "migrated:playbook_v2", "memory_version_added": 1,
        })
    return out


def author_rules() -> list[dict]:
    from orchestra.control.author.rules_doc import (
        RULES_DOC_ROOT,
        current_version_dir,
        load_rules_doc,
    )

    try:
        doc = load_rules_doc(current_version_dir(ROOT / RULES_DOC_ROOT))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for i, r in enumerate(getattr(doc, "rules", []) or [], 1):
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        kind = str(d.get("kind") or d.get("category") or "main_path")
        out.append({"rule_id": f"AU-R-{i:03d}", "category_id": f"AU-{kind.upper()}", "trigger": d.get("trigger"),
                    "rule": d.get("rule"), "example": d.get("example"), "state": d.get("state") or "trial",
                    "evidence": {"migrated_from": "configs/author/rules_doc"}, "origin": "migrated:rules_doc",
                    "memory_version_added": 1})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT / "memory"))
    ap.add_argument("--with-draft-categories", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    root = Path(a.root)
    if (root / "VERSION").exists() and not a.force:
        print(f"{root} exists (version {current_version(root)}); use --force to recreate")
        return 1
    if root.exists():
        for p in root.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
        shutil.rmtree(root)
    for b in BANKS:
        (root / b).mkdir(parents=True)
    (root / "transfer").mkdir()
    write_yaml(root / "domain_tags.yaml", {"tags": DOMAIN_TAGS, "note": "maintained by people; the evolver may not change it"})

    canaries = {b: (canary(b), canary(b)) for b in BANKS}
    fp_cats = [{**c, "state": "active", "origin": "human:spec 2026-10-09 §5.5 draft"} for c in FIRST_PASS_DRAFT] if a.with_draft_categories else []
    rp_cats = [{"category_id": i, "name": n, "definition": d, "signals": [], "state": "active", "origin": "migrated:error_classes"} for i, n, d in REPAIR_CATEGORIES]
    au_cats = [{"category_id": i, "name": n, "definition": d, "signals": [], "state": "active", "origin": "migrated:inventory kinds"} for i, n, d in AUTHOR_CATEGORIES]
    retired_fp = {"pitfall_id": "P-0000", "category_id": (fp_cats[0]["category_id"] if fp_cats else "FP-RETIRED"),
                  "symptom": f"retired canary entry {canaries['first_pass'][1]}", "cause": "never recalled", "lesson": "never recalled",
                  "applies_when": {}, "evidence": {"source_records": [], "fixes_observed": 0, "milestones_observed": 0},
                  "state": "retired", "origin": "human:canary", "memory_version_added": 1}
    if not fp_cats:
        fp_cats = [{"category_id": "FP-RETIRED", "name": "canary holder", "definition": "holds the retired canary entry only", "signals": [], "state": "retired", "origin": "human:canary"}]
    elif a.with_draft_categories:
        pass
    retired_rp = {"pattern_id": "RP-CANARY", "category_id": "RP-ANY", "category_ids": ["RP-ANY"],
                  "action": {"instruction": f"retired canary entry {canaries['repair'][1]}", "add_readonly_role": None, "template": None, "budget_delta": None},
                  "intent": "canary", "row": None, "state": "retired", "evidence": {}, "origin": "human:canary", "memory_version_added": 1}
    retired_au = {"rule_id": "AU-R-000", "category_id": "AU-MAIN_PATH", "trigger": "never", "rule": f"retired canary entry {canaries['author'][1]}",
                  "state": "retired", "evidence": {}, "origin": "human:canary", "memory_version_added": 1}

    files = {
        ("first_pass", "categories"): fp_cats, ("first_pass", "pitfalls"): [retired_fp], ("first_pass", "patterns"): [],
        ("repair", "categories"): rp_cats, ("repair", "patterns"): repair_patterns() + [retired_rp],
        ("author", "categories"): au_cats, ("author", "rules"): author_rules() + [retired_au],
    }
    train = ["bplustree", "cookiecutter", "deprecated", "djangorestframework-simplejwt", "imapclient", "python-hl7", "voluptuous", "zxcvbn"]
    ids = build_identifiers(train)
    report = {}
    rejected_all: list[dict] = []
    for (bank, name), entries in files.items():
        cat_ids = [c["category_id"] for c in files[(bank, "categories")]]
        verdicts = validate_entries(bank, name, entries, category_ids=cat_ids, domain_tags=DOMAIN_TAGS, identifiers=ids,
                                    sources={}, test_tasks=[], substring=True)
        bad = [v for v in verdicts if not v.ok]
        report[f"{bank}/{name}"] = {"entries": len(entries), "rejected": {v.entry_id: v.reasons for v in bad}}
        keep_ids = {v.entry_id for v in verdicts if v.ok}
        id_field = {"categories": "category_id", "pitfalls": "pitfall_id", "patterns": "pattern_id", "rules": "rule_id"}[name]
        kept = [e for e in entries if str(e.get(id_field)) in keep_ids]
        write_yaml(root / bank / f"{name}.yaml", kept)
        rejected_all += [{"bank": bank, "file": name, "entry": e, "reasons": next(v.reasons for v in verdicts if v.entry_id == str(e.get(id_field))),
                          "stage": "migration", "date": now()} for e in entries if str(e.get(id_field)) not in keep_ids]
    for b in BANKS:
        (root / b / "SKILL.md").write_text(SKILLS[b], encoding="utf-8")
        write_yaml(root / b / "CHANGELOG.yaml", [{
            "version": 1, "date": now(), "origin": "memory_init", "reason": "store created; tables migrated",
            "canary": canaries[b][0], "validation": {k: v for k, v in report.items() if k.startswith(b)},
        }])
    for name in ("pending_pitfalls", "sources", "author_pending"):
        write_yaml(root / "transfer" / f"{name}.yaml", [])
    write_yaml(root / "transfer" / "rejected.yaml", rejected_all)
    (root / "VERSION").write_text("1\n", encoding="utf-8")
    take_snapshot(root, 1)
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
