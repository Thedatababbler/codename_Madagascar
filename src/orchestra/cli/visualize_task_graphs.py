"""Visualize TaskPlan overall DAG and each subtask local multi-agent graph.

Outputs (under --output):
  - index.html                 interactive gallery
  - overall_task_graph.svg     subtask dependency + communication DAG
  - overall_task_graph.mmd     Mermaid source
  - subtasks/<id>/graph.svg    agent-only projection of local IR graph
  - subtasks/<id>/graph.mmd
  - subtasks/<id>/summary.json
  - problem_snapshot.json      formal LCB problem metadata (when available)

Local subgraphs hide harness / transform / selector nodes; edges are
collapsed so only agent → agent dataflow remains.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.schemas import TaskPlan
from orchestra.ir.graph import OrchestraGraph, load_graph

KIND_COLOR = {
    "agent": "#1f6feb",
    "subtask": "#0b4f6c",
    "communication": "#cf222e",
}


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"expected mapping: {path}")
    return raw


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _agent_count(graph: OrchestraGraph) -> int:
    return sum(1 for n in graph.nodes if n.node_kind.value == "agent")


def _layout_levels(
    nodes: list[str], edges: list[tuple[str, str]]
) -> dict[str, tuple[float, float]]:
    """Simple layered layout (no external graph library)."""
    preds: dict[str, set[str]] = defaultdict(set)
    succs: dict[str, set[str]] = defaultdict(set)
    for src, dst in edges:
        if src in nodes and dst in nodes:
            preds[dst].add(src)
            succs[src].add(dst)
    level: dict[str, int] = {}
    remaining = set(nodes)
    current = 0
    # Kahn-like layering by iterative frontier.
    while remaining:
        frontier = sorted(
            n for n in remaining if all(p not in remaining for p in preds[n])
        )
        if not frontier:
            frontier = sorted(remaining)
        for n in frontier:
            level[n] = current
            remaining.remove(n)
        current += 1
    by_level: dict[int, list[str]] = defaultdict(list)
    for n, lv in level.items():
        by_level[lv].append(n)
    coords: dict[str, tuple[float, float]] = {}
    x_gap, y_gap = 220.0, 110.0
    for lv, members in sorted(by_level.items()):
        members = sorted(members)
        width = (len(members) - 1) * x_gap
        for i, n in enumerate(members):
            x = i * x_gap - width / 2
            y = lv * y_gap
            coords[n] = (x, y)
    return coords


def _svg_graph(
    *,
    title: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    width: int = 960,
    height: int = 720,
) -> str:
    ids = [n["id"] for n in nodes]
    edge_pairs = [(e["source"], e["target"]) for e in edges]
    coords = _layout_levels(ids, edge_pairs)
    if not coords:
        safe = html.escape(title)
        return (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            f'<text x="20" y="40">{safe}</text></svg>'
        )
    xs = [c[0] for c in coords.values()]
    ys = [c[1] for c in coords.values()]
    pad = 80
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    scale_x = 1.0
    scale_y = 1.0
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    usable_w = width - 2 * pad
    usable_h = height - 2 * pad - 40
    scale_x = usable_w / span_x if span_x else 1.0
    scale_y = usable_h / span_y if span_y else 1.0

    def map_point(x: float, y: float) -> tuple[float, float]:
        return (
            pad + (x - min_x) * scale_x,
            pad + 40 + (y - min_y) * scale_y,
        )

    node_meta = {n["id"]: n for n in nodes}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f6f8fa"/>',
        f'<text x="{pad}" y="36" font-family="ui-sans-serif,system-ui" font-size="20" '
        f'font-weight="700" fill="#24292f">{html.escape(title)}</text>',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#57606a"/></marker></defs>',
    ]
    for e in edges:
        x1, y1 = map_point(*coords[e["source"]])
        x2, y2 = map_point(*coords[e["target"]])
        # shorten toward node boxes
        dx, dy = x2 - x1, y2 - y1
        dist = math.hypot(dx, dy) or 1.0
        shrink = 28.0
        x1s = x1 + dx / dist * shrink
        y1s = y1 + dy / dist * shrink
        x2s = x2 - dx / dist * shrink
        y2s = y2 - dy / dist * shrink
        stroke = "#cf222e" if e.get("kind") == "communication" else "#57606a"
        dash = ' stroke-dasharray="6 4"' if e.get("kind") == "communication" else ""
        label = html.escape(str(e.get("label") or ""))
        parts.append(
            f'<line x1="{x1s:.1f}" y1="{y1s:.1f}" x2="{x2s:.1f}" y2="{y2s:.1f}" '
            f'stroke="{stroke}" stroke-width="2" marker-end="url(#arrow)"{dash}/>'
        )
        if label:
            mx, my = (x1s + x2s) / 2, (y1s + y2s) / 2
            parts.append(
                f'<text x="{mx:.1f}" y="{my-6:.1f}" text-anchor="middle" '
                f'font-size="11" fill="#57606a" font-family="ui-sans-serif,system-ui">'
                f"{label}</text>"
            )
    for nid, (x, y) in coords.items():
        cx, cy = map_point(x, y)
        meta = node_meta[nid]
        color = KIND_COLOR.get(str(meta.get("kind")), "#6e7781")
        label = html.escape(str(meta.get("label") or nid))
        sub = html.escape(str(meta.get("subtitle") or ""))
        parts.append(
            f'<rect x="{cx-70:.1f}" y="{cy-24:.1f}" rx="10" ry="10" width="140" height="48" '
            f'fill="#ffffff" stroke="{color}" stroke-width="2.5"/>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{cy-2:.1f}" text-anchor="middle" font-size="12" '
            f'font-weight="650" fill="#24292f" font-family="ui-sans-serif,system-ui">{label}</text>'
        )
        if sub:
            parts.append(
                f'<text x="{cx:.1f}" y="{cy+14:.1f}" text-anchor="middle" font-size="10" '
                f'fill="#57606a" font-family="ui-sans-serif,system-ui">{sub}</text>'
            )
    # legend
    lx, ly = pad, height - 28
    for i, (kind, color) in enumerate(KIND_COLOR.items()):
        x = lx + i * 140
        parts.append(
            f'<rect x="{x}" y="{ly-10}" width="12" height="12" fill="{color}"/>'
            f'<text x="{x+18}" y="{ly}" font-size="11" fill="#24292f" '
            f'font-family="ui-sans-serif,system-ui">{kind}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _mermaid_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    lines = ["flowchart TD"]
    for n in nodes:
        nid = n["id"].replace("-", "_")
        label = str(n.get("label") or n["id"]).replace('"', "'")
        kind = str(n.get("kind") or "")
        shape_l, shape_r = ("[", "]")
        if kind == "agent":
            shape_l, shape_r = ("([", "])")
        elif kind == "harness":
            shape_l, shape_r = ("{{", "}}")
        elif kind == "communication":
            shape_l, shape_r = (">", "]")
        lines.append(f'  {nid}{shape_l}"{label}"{shape_r}')
    for e in edges:
        s = e["source"].replace("-", "_")
        t = e["target"].replace("-", "_")
        label = str(e.get("label") or "").replace('"', "'")
        if e.get("kind") == "communication":
            arrow = "-.->" if not label else f"-. {label} .->"
        else:
            arrow = "-->" if not label else f"-- {label} -->"
        lines.append(f"  {s} {arrow} {t}")
    return "\n".join(lines) + "\n"


def _agent_only_payload(graph: OrchestraGraph) -> tuple[list[dict], list[dict]]:
    """Project IR graph to agents only; collapse paths through non-agent nodes."""
    kind_by_id = {n.node_id: n.node_kind.value for n in graph.nodes}
    agents = {nid for nid, kind in kind_by_id.items() if kind == "agent"}
    adj: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for e in graph.edges:
        cond = None
        if e.condition is not None:
            cond = f"{e.condition.source_field}:{e.condition.operator}"
        adj[e.source_node].append((e.destination_node, cond))

    # BFS from each agent through non-agents to reach next agents.
    projected: dict[tuple[str, str], str | None] = {}
    for start in agents:
        stack = [(nbr, label) for nbr, label in adj.get(start, [])]
        seen_non_agent: set[str] = set()
        while stack:
            node, label = stack.pop()
            if node in agents:
                key = (start, node)
                if key not in projected or (projected[key] is None and label):
                    projected[key] = label
                continue
            if node in seen_non_agent:
                continue
            seen_non_agent.add(node)
            for nbr, nbr_label in adj.get(node, []):
                stack.append((nbr, label or nbr_label))

    nodes = []
    for n in graph.nodes:
        if n.node_kind.value != "agent":
            continue
        backend = getattr(getattr(n, "backend", None), "type", None)
        contract = getattr(n, "contract_id", None)
        nodes.append(
            {
                "id": n.node_id,
                "label": n.node_id,
                "kind": "agent",
                "subtitle": contract or backend or "agent",
            }
        )
    edges = [
        {
            "source": src,
            "target": dst,
            "label": label or "",
            "kind": "edge",
        }
        for (src, dst), label in sorted(projected.items())
    ]
    return nodes, edges


def _overall_payload(plan: TaskPlan) -> tuple[list[dict], list[dict]]:
    nodes = []
    for s in plan.subtasks:
        graph = load_graph(s.local_graph_template)
        agents = _agent_count(graph)
        nodes.append(
            {
                "id": s.subtask_id,
                "label": s.subtask_id,
                "kind": "subtask",
                "subtitle": f"{agents} agents · {Path(s.local_graph_template).name}",
            }
        )
    edges = []
    for s in plan.subtasks:
        for dep in s.dependencies:
            edges.append(
                {
                    "source": dep,
                    "target": s.subtask_id,
                    "label": "depends",
                    "kind": "dependency",
                }
            )
    for contract in plan.communication_plan.payload_contracts:
        edges.append(
            {
                "source": contract.source_subtask_id,
                "target": contract.target_subtask_id,
                "label": contract.payload_id,
                "kind": "communication",
            }
        )
        # ensure communication marker node not required; edge kind is enough
    return nodes, edges


def _index_html(
    *,
    title: str,
    problem: dict[str, Any] | None,
    overall_svg: str,
    subtask_cards: list[dict[str, str]],
) -> str:
    problem_block = ""
    if problem:
        statement = html.escape(str(problem.get("statement") or "")[:2500])
        problem_block = f"""
        <section class="card">
          <h2>Formal problem</h2>
          <p><b>{html.escape(str(problem.get('question_id')))}</b> —
             {html.escape(str(problem.get('title')))}
             ({html.escape(str(problem.get('difficulty')))} /
              {html.escape(str(problem.get('platform')))})</p>
          <pre class="statement">{statement}</pre>
        </section>"""
    cards = "\n".join(
        f"""
        <section class="card">
          <h2>{html.escape(c['title'])}</h2>
          <p>{html.escape(c['subtitle'])}</p>
          <div class="svg-wrap">{c['svg']}</div>
          <p><a href="{html.escape(c['svg_href'])}">SVG</a> ·
             <a href="{html.escape(c['mmd_href'])}">Mermaid</a> ·
             <a href="{html.escape(c['json_href'])}">JSON</a></p>
        </section>"""
        for c in subtask_cards
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{
      margin:0; font-family: ui-sans-serif, system-ui, sans-serif;
      background:#eef2f6; color:#24292f;
    }}
    header {{
      padding:28px 32px;
      background:linear-gradient(120deg,#0b4f6c,#1f6feb);
      color:white;
    }}
    main {{ padding:24px 32px 64px; display:grid; gap:20px; }}
    .card {{
      background:white; border-radius:16px; padding:18px 18px 10px;
      box-shadow:0 8px 24px rgba(15,23,42,.08);
    }}
    .svg-wrap {{
      overflow:auto; border:1px solid #d0d7de; border-radius:12px;
      background:#f6f8fa;
    }}
    pre.statement {{
      white-space:pre-wrap; background:#f6f8fa; padding:12px;
      border-radius:10px; max-height:280px; overflow:auto;
    }}
    a {{ color:#0969da; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <p>Overall TaskPlan DAG + per-subtask agent-only graphs (harness/transform/selector hidden).</p>
  </header>
  <main>
    {problem_block}
    <section class="card">
      <h2>Overall task graph</h2>
      <div class="svg-wrap">{overall_svg}</div>
      <p><a href="overall_task_graph.svg">SVG</a> · <a href="overall_task_graph.mmd">Mermaid</a></p>
    </section>
    {cards}
  </main>
</body>
</html>
"""


def _load_problem_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    bench = config.get("benchmark") or {}
    data_dir = bench.get("data_dir")
    task_id = bench.get("task_id")
    if not data_dir or not task_id:
        return None
    try:
        from orchestra.adapters.livecodebench.loader import LiveCodeBenchLoader

        loader = LiveCodeBenchLoader(
            data_dir=data_dir,
            release_version=str(bench.get("release_version") or "release_v6"),
        )
        tasks = loader.load(task_ids={str(task_id)})
        if not tasks:
            return None
        problem = tasks[0].problem
        return {
            "question_id": problem.question_id,
            "title": problem.title,
            "difficulty": problem.difficulty,
            "platform": problem.platform,
            "contest_date": problem.contest_date,
            "statement": problem.statement,
            "starter_code": problem.starter_code,
            "public_examples": [e.model_dump(mode="json") for e in problem.public_examples],
            "function_name": problem.function_name,
            "source": "livecodebench",
            "release_version": str(bench.get("release_version") or "release_v6"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "task_id": task_id}


def visualize(
    *,
    plan_path: Path,
    config_path: Path | None,
    output_dir: Path,
) -> dict[str, Any]:
    plan_raw = _load_yaml(plan_path)
    candidate = TaskPlan.model_validate(plan_raw)
    default_graph = candidate.subtasks[0].local_graph_template
    plan = TaskDecomposer(
        enabled=True,
        default_graph_template=default_graph,
        keystone_harness_id=candidate.subtasks[0].keystone_harness_id,
        require_graph_files=True,
    ).decompose(
        task_id=candidate.task_id,
        objective=candidate.subtasks[0].objective,
        candidate_plan=candidate,
        metadata={"plan_config": str(plan_path)},
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    config = _load_yaml(config_path) if config_path else {}
    problem = _load_problem_snapshot(config) if config else None
    if problem:
        _write(
            output_dir / "problem_snapshot.json",
            json.dumps(problem, indent=2, ensure_ascii=False) + "\n",
        )

    overall_nodes, overall_edges = _overall_payload(plan)
    overall_svg = _svg_graph(
        title=f"Overall TaskPlan — {plan.task_id}",
        nodes=overall_nodes,
        edges=overall_edges,
        width=1100,
        height=520,
    )
    _write(output_dir / "overall_task_graph.svg", overall_svg)
    _write(output_dir / "overall_task_graph.mmd", _mermaid_graph(overall_nodes, overall_edges))

    cards: list[dict[str, str]] = []
    subtask_summaries = []
    for sub in plan.subtasks:
        graph = load_graph(sub.local_graph_template)
        agents = _agent_count(graph)
        if agents < 3:
            raise ValueError(
                f"subtask {sub.subtask_id} graph has {agents} agents; need ≥ 3"
            )
        nodes, edges = _agent_only_payload(graph)
        svg = _svg_graph(
            title=f"{sub.subtask_id} — {graph.graph_id} ({agents} agents)",
            nodes=nodes,
            edges=edges,
            width=900,
            height=480,
        )
        sub_dir = output_dir / "subtasks" / sub.subtask_id
        _write(sub_dir / "graph.svg", svg)
        _write(sub_dir / "graph.mmd", _mermaid_graph(nodes, edges))
        summary = {
            "subtask_id": sub.subtask_id,
            "title": sub.title,
            "objective": sub.objective,
            "dependencies": list(sub.dependencies),
            "local_graph_template": sub.local_graph_template,
            "graph_id": graph.graph_id,
            "graph_hash": graph.content_hash,
            "agent_count": agents,
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "agents": [
                n.node_id for n in graph.nodes if n.node_kind.value == "agent"
            ],
        }
        _write(sub_dir / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        subtask_summaries.append(summary)
        cards.append(
            {
                "title": f"Subtask `{sub.subtask_id}`",
                "subtitle": (
                    f"{agents} agents · graph `{graph.graph_id}` · "
                    f"deps={list(sub.dependencies) or ['(none)']}"
                ),
                "svg": svg,
                "svg_href": f"subtasks/{sub.subtask_id}/graph.svg",
                "mmd_href": f"subtasks/{sub.subtask_id}/graph.mmd",
                "json_href": f"subtasks/{sub.subtask_id}/summary.json",
            }
        )

    index = _index_html(
        title=f"AdaMAS graph viz — {plan.task_id}",
        problem=problem if problem and "error" not in problem else problem,
        overall_svg=overall_svg,
        subtask_cards=cards,
    )
    _write(output_dir / "index.html", index)
    manifest = {
        "task_id": plan.task_id,
        "plan_path": str(plan_path),
        "subtask_count": len(plan.subtasks),
        "subtasks": subtask_summaries,
        "problem": {
            "question_id": (problem or {}).get("question_id"),
            "title": (problem or {}).get("title"),
            "source": (problem or {}).get("source"),
        }
        if problem
        else None,
        "outputs": {
            "index_html": str(output_dir / "index.html"),
            "overall_svg": str(output_dir / "overall_task_graph.svg"),
        },
    }
    _write(output_dir / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        default="configs/plans/lcb_abc309_a_three_subtasks.yaml",
    )
    parser.add_argument(
        "--config",
        default="configs/experiments/lcb_formal_three_subtask_mas.yaml",
        help="Experiment YAML used to resolve formal LCB problem snapshot",
    )
    parser.add_argument(
        "--output",
        default="outputs/lcb_formal_three_subtask_mas/graph_viz",
    )
    args = parser.parse_args()
    manifest = visualize(
        plan_path=Path(args.plan),
        config_path=Path(args.config) if args.config else None,
        output_dir=Path(args.output),
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"open {manifest['outputs']['index_html']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
