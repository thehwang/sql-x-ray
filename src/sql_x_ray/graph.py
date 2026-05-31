"""IR -> Mermaid data-flow graph.

Base tables render as cylinders, CTEs as rectangles, the final result as a
rounded/stadium node. Each node is tagged with a short summary of the operations
it performs, so the diagram answers "where does data come from and what happens to
it" at a glance.
"""
from __future__ import annotations

import re

from .ir import QueryNode, SqlModel

# Short, human tags for operation kinds shown inside node boxes.
_OP_TAG = {
    "filter": "filter",
    "aggregate": "aggregate",
    "group": "group-by",
    "having": "having",
    "join": "join",
    "window": "window",
    "order": "order",
    "limit": "limit",
    "distinct": "distinct",
}


def _safe_id(name: str) -> str:
    """Mermaid node ids must be identifier-safe."""
    ident = re.sub(r"\W", "_", name)
    return ident or "n"


def _summary(node: QueryNode) -> str:
    seen: list[str] = []
    for kind in node.op_kinds():
        tag = _OP_TAG.get(kind, kind)
        if tag not in seen:
            seen.append(tag)
    return " + ".join(seen)


def _label(node: QueryNode) -> str:
    summary = _summary(node)
    text = node.name if not summary else f"{node.name}<br/><i>{summary}</i>"
    return text.replace('"', "'")


def to_mermaid(model: SqlModel) -> str:
    lines = ["flowchart TD"]

    # Declare base tables (cylinders), deduplicated across the whole query.
    table_ids: dict[str, str] = {}
    for node in model.nodes:
        for tbl in node.source_tables:
            if tbl not in table_ids:
                tid = f"t_{_safe_id(tbl)}"
                table_ids[tbl] = tid
                lines.append(f'  {tid}[("{tbl}")]')

    # Declare CTE and result nodes.
    node_ids: dict[str, str] = {}
    for node in model.nodes:
        nid = f"n_{_safe_id(node.name)}"
        node_ids[node.name] = nid
        label = _label(node)
        if node.is_final:
            lines.append(f'  {nid}(["{label}"])')
        else:
            lines.append(f'  {nid}["{label}"]')

    # Edges: base tables and upstream CTEs flow into each node.
    for node in model.nodes:
        dst = node_ids[node.name]
        for tbl in node.source_tables:
            lines.append(f"  {table_ids[tbl]} --> {dst}")
        for cte in node.source_ctes:
            src = node_ids.get(cte)
            if src:
                lines.append(f"  {src} --> {dst}")

    # Write target (INSERT/CREATE): the final node flows into a sink table.
    if model.target_table and model.statement_kind in ("INSERT", "CREATE"):
        sink = f"w_{_safe_id(model.target_table)}"
        verb = "INSERT" if model.statement_kind == "INSERT" else "CREATE"
        lines.append(f'  {sink}[("{model.target_table}<br/><i>{verb}</i>")]')
        final = model.final
        if final is not None:
            lines.append(f"  {node_ids[final.name]} ==>|{verb}| {sink}")

    return "\n".join(lines)
