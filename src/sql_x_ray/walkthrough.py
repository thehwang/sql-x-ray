"""IR -> plain-language, template-based walkthrough.

This is the deterministic (no-LLM) narration that ships in v0.1. In v0.2 the same
IR is handed to a local LLM (Ollama) to turn these facts into fluent prose; the
templates remain the offline fallback and the ground truth the LLM must not stray
from.
"""
from __future__ import annotations

from .ir import QueryNode, SqlModel

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
_DETAIL_LIMIT = 100
_INLINE_LIST_MAX = 6
_PREVIEW = 3


def _step_marker(i: int) -> str:
    return _CIRCLED[i] if i < len(_CIRCLED) else f"({i + 1})"


def _clip(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _sources_phrase(node: QueryNode) -> str:
    parts: list[str] = []
    if node.source_tables:
        parts.append("table(s) " + ", ".join(f"`{t}`" for t in node.source_tables))
    if node.source_ctes:
        parts.append("step(s) " + ", ".join(f"`{c}`" for c in node.source_ctes))
    if not parts:
        return "a literal/constant source"
    return " and ".join(parts)


def _join_phrases(joins: list, verbose: bool) -> list[str]:
    if not joins:
        return []
    if verbose:
        return [f"joins via {op.detail}" for op in joins]
    if len(joins) == 1:
        return [f"joins via {_clip(joins[0].detail)}"]
    targets = [op.brief for op in joins]
    shown = ", ".join(targets[:_INLINE_LIST_MAX])
    extra = len(targets) - _INLINE_LIST_MAX
    if extra > 0:
        shown += f", +{extra} more"
    return [f"joins {len(joins)} table(s): {shown}"]


def _list_phrase(verb: str, op, verbose: bool) -> str:
    cols = [c.strip() for c in op.detail.split(",")]
    n = len(cols)
    if verbose or n <= _INLINE_LIST_MAX:
        return f"{verb} {op.detail}"
    preview = ", ".join(cols[:_PREVIEW])
    return f"{verb} {n} columns (e.g. {preview}, \u2026)"


def _ops_phrases(node: QueryNode, verbose: bool) -> list[str]:
    phrases: list[str] = []
    joins = [op for op in node.operations if op.kind == "join"]
    phrases.extend(_join_phrases(joins, verbose))

    for op in node.operations:
        detail = op.detail if verbose else _clip(op.detail)
        if op.kind == "join":
            continue
        elif op.kind == "filter":
            phrases.append(f"keeps only rows where {detail}")
        elif op.kind == "group":
            phrases.append(_list_phrase("groups by", op, verbose))
        elif op.kind == "aggregate":
            phrases.append(f"computes {op.detail}")
        elif op.kind == "having":
            phrases.append(f"keeps groups where {detail}")
        elif op.kind == "window":
            text = op.detail if verbose else (op.brief or _clip(op.detail))
            phrases.append(f"computes a window function: {text}")
        elif op.kind == "order":
            phrases.append(_list_phrase("orders by", op, verbose))
        elif op.kind == "limit":
            phrases.append(f"limits to {op.detail} row(s)")
        elif op.kind == "distinct":
            phrases.append("removes duplicate rows")
    return phrases


def _describe(node: QueryNode, marker: str, verbose: bool) -> str:
    head = "final result" if node.is_final else f"`{node.name}`"
    lines = [f"{marker} {head} — reads from {_sources_phrase(node)}."]
    for phrase in _ops_phrases(node, verbose):
        lines.append(f"     · {phrase}")
    if node.output_columns:
        n = len(node.output_columns)
        if verbose or n <= _INLINE_LIST_MAX:
            cols = ", ".join(f"`{c}`" for c in node.output_columns)
            lines.append(f"     · outputs {n} column(s): {cols}")
        else:
            preview = ", ".join(f"`{c}`" for c in node.output_columns[:_PREVIEW])
            lines.append(f"     · outputs {n} columns (e.g. {preview}, \u2026)")
    return "\n".join(lines)


def _header(model: SqlModel) -> str:
    kind = model.statement_kind
    tmpl = " (Jinja templating detected and substituted)" if model.templated else ""

    if kind == "UNSUPPORTED":
        return (
            "Unsupported / unparseable statement for this dialect "
            "(e.g. COPY/UNLOAD/EXPORT or vendor-specific DDL) — skipped." + tmpl
        )

    if kind == "DELETE":
        tgt = f"`{model.target_table}`" if model.target_table else "a table"
        return f"DELETE statement on {tgt}.{tmpl}"

    cte_count = len(model.ctes)
    n_steps = len(model.nodes)
    base = (
        f"{kind} statement with {cte_count} intermediate step(s) (CTE) plus a final "
        f"SELECT, {n_steps} step(s) total."
    )
    if kind in ("INSERT", "CREATE") and model.target_table:
        verb = "inserts into" if kind == "INSERT" else "creates"
        base += f" Final result {verb} `{model.target_table}`."
    return base + tmpl


def walkthrough(model: SqlModel, verbose: bool = False) -> str:
    blocks: list[str] = [_header(model)]
    for i, node in enumerate(model.nodes):
        blocks.append(_describe(node, _step_marker(i), verbose))
    return "\n\n".join(blocks)
