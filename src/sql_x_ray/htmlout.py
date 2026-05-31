"""IR -> a single, self-contained, interactive HTML page.

No web server: one HTML file with the data-flow diagram (Mermaid) and the parsed
facts embedded. Clicking a node in the diagram highlights it and shows that node's
SQL, sources, operations, and outputs in a side panel. Mermaid loads from a CDN,
so viewing needs network the first time; everything else is inlined.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from html import escape
from importlib import resources

from .graph import _safe_id, to_mermaid
from .ir import QueryNode, SqlModel

_CDN_URL = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"


@lru_cache(maxsize=1)
def _mermaid_js() -> str:
    """The vendored Mermaid UMD build (sets a global ``mermaid``)."""
    path = resources.files("sql_x_ray") / "vendor" / "mermaid.min.js"
    return path.read_text(encoding="utf-8")


def _safe_inline_js(js: str) -> str:
    # Prevent a literal </script> inside the bundle from closing our tag.
    return re.sub(r"</(script)", r"<\\/\1", js, flags=re.IGNORECASE)

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>%%TITLE%%</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 -apple-system, system-ui, sans-serif;
  background: #0f1117; color: #e6e6e6; }
header { padding: 14px 20px; border-bottom: 1px solid #232734;
  background: #151926; position: sticky; top: 0; z-index: 5; }
header h1 { margin: 0; font-size: 16px; letter-spacing: .3px; }
header .sub { color: #8b93a7; font-size: 12px; margin-top: 2px; }
.layout { display: flex; gap: 0; align-items: stretch; }
main { flex: 1 1 auto; padding: 16px 20px; min-width: 0; }
aside { flex: 0 0 420px; max-width: 46vw; border-left: 1px solid #232734;
  background: #121521; padding: 16px 18px; position: sticky; top: 57px;
  height: calc(100vh - 57px); overflow: auto; }
.stmt { margin-bottom: 26px; }
.stmt-head { font-size: 13px; color: #9aa4bd; text-transform: uppercase;
  letter-spacing: .6px; margin: 0 0 8px; }
.stmt-head b { color: #7aa2ff; }
.card { background: #151926; border: 1px solid #232734; border-radius: 10px;
  padding: 12px; overflow: auto; }
.mermaid { margin: 0; }
.hint { color: #8b93a7; }
h2.detail-title { margin: 0 0 4px; font-size: 15px; }
.badge { display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 999px;
  background: #1d2233; color: #9fb0ff; border: 1px solid #2b3350; margin-left: 6px; }
.k { color: #8b93a7; font-size: 12px; text-transform: uppercase; letter-spacing: .5px;
  margin: 14px 0 4px; }
ul.ops { margin: 0; padding-left: 16px; }
ul.ops li { margin: 2px 0; }
ul.ops .kind { color: #7aa2ff; }
code, pre.sql { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre.sql { background: #0b0e15; border: 1px solid #232734; border-radius: 8px;
  padding: 12px; white-space: pre-wrap; word-break: break-word; font-size: 12.5px;
  color: #cdd6f4; }
.tag { color: #c3e88d; }
.muted { color: #6b7280; }
footer { padding: 10px 20px; color: #6b7280; font-size: 12px;
  border-top: 1px solid #232734; }
</style>
</head>
<body>
<header>
  <h1>SQL X-Ray</h1>
  <div class="sub">%%SUB%%</div>
</header>
<div class="layout">
  <main>%%BODY%%</main>
  <aside id="detail"><div class="hint">%%HINT%%</div></aside>
</div>
<footer>%%FOOTER%%</footer>
%%SCRIPTS%%
</body>
</html>
"""

# Shared client logic. Works whether `mermaid` is a global (inline build) or a
# module import (CDN). %%DATA%% / %%LABELS%% are filled in last.
_APP_JS = """const SXR = %%DATA%%;
const L = %%LABELS%%;
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
window.sxrSelect = function(stmt, name){
  const node = (SXR[stmt] && SXR[stmt].nodes[name]) || null;
  const el = document.getElementById('detail');
  if(!node){ el.innerHTML = '<div class="hint">'+L.no_details+'</div>'; return; }
  let h = '<h2 class="detail-title">'+esc(name)+'<span class="badge">'+esc(node.role)+'</span></h2>';
  const src = [];
  if(node.sources.tables.length) src.push(L.tables+': '+node.sources.tables.map(esc).join(', '));
  if(node.sources.steps.length) src.push(L.steps+': '+node.sources.steps.map(esc).join(', '));
  h += '<div class="muted">'+L.reads_from+' '+(src.length?src.join(' &middot; '):L.none)+'</div>';
  if(node.ops.length){
    h += '<div class="k">'+L.operations+'</div><ul class="ops">';
    for(const op of node.ops){ h += '<li><span class="kind">'+esc(op.kind)+'</span>: '+esc(op.detail||op.brief)+'</li>'; }
    h += '</ul>';
  }
  if(node.outputs.length){
    h += '<div class="k">'+L.outputs+' ('+node.outputs.length+')</div><div class="tag">'+node.outputs.map(esc).join(', ')+'</div>';
  }
  if(node.sql){ h += '<div class="k">'+L.sql+'</div><pre class="sql">'+esc(node.sql)+'</pre>'; }
  el.innerHTML = h;
};
mermaid.initialize({ startOnLoad: true, securityLevel: 'loose', theme: 'dark',
  flowchart: { htmlLabels: true, curve: 'basis' } });
"""


def _scripts(inline: bool) -> str:
    if inline:
        bundle = _safe_inline_js(_mermaid_js())
        return f"<script>{bundle}</script>\n<script>\n{_APP_JS}</script>"
    return (
        f'<script type="module">\nimport mermaid from \'{_CDN_URL}\';\n'
        f"{_APP_JS}</script>"
    )


# UI chrome labels per language. SQL identifiers themselves are never translated.
# Unknown languages fall back to English.
_LABELS = {
    "en": {
        "sub_click": "click any node to inspect its SQL",
        "hint": "Click a node in the diagram to inspect its SQL, sources, "
        "operations and outputs.",
        "footer": "Generated by SQL X-Ray &middot; data-flow via Mermaid",
        "statement": "Statement",
        "statements_label": "statement(s)",
        "reads_from": "reads from",
        "operations": "Operations",
        "outputs": "Outputs",
        "sql": "SQL",
        "tables": "tables",
        "steps": "steps",
        "none": "(none)",
        "no_details": "No details.",
        "final_select": "final SELECT",
        "cte": "CTE",
    },
    "zh": {
        "sub_click": "点击任意节点查看其 SQL",
        "hint": "点击图中的节点，查看其 SQL、来源、操作与输出。",
        "footer": "由 SQL X-Ray 生成 &middot; 数据流图基于 Mermaid",
        "statement": "语句",
        "statements_label": "条语句",
        "reads_from": "读取自",
        "operations": "操作",
        "outputs": "输出",
        "sql": "SQL",
        "tables": "表",
        "steps": "步骤",
        "none": "（无）",
        "no_details": "无详情。",
        "final_select": "最终 SELECT",
        "cte": "CTE",
    },
}

_LANG_ALIASES = {
    "zh": "zh",
    "zh-cn": "zh",
    "zh_cn": "zh",
    "chinese": "zh",
    "中文": "zh",
    "简体中文": "zh",
    "en": "en",
    "english": "en",
}


def _resolve_lang(lang: str | None) -> str:
    if not lang:
        return "en"
    return _LANG_ALIASES.get(lang.strip().lower(), "en")


def _mermaid_with_clicks(model: SqlModel, idx: int) -> str:
    src = to_mermaid(model)
    lines = [src]
    for node in model.nodes:
        nid = f"n_{_safe_id(node.name)}"
        lines.append(f'  click {nid} call sxrSelect({idx}, "{node.name}")')
    return "\n".join(lines)


def _node_data(node: QueryNode, labels: dict) -> dict:
    return {
        "role": labels["final_select"] if node.is_final else labels["cte"],
        "sources": {"tables": node.source_tables, "steps": node.source_ctes},
        "ops": [
            {"kind": op.kind, "detail": op.detail, "brief": op.brief}
            for op in node.operations
        ],
        "outputs": node.output_columns,
        "sql": node.sql_text,
    }


def _statement_data(model: SqlModel, labels: dict) -> dict:
    return {
        "kind": model.statement_kind,
        "target": model.target_table,
        "templated": model.templated,
        "nodes": {n.name: _node_data(n, labels) for n in model.nodes},
    }


def to_html(
    models: list[SqlModel],
    title: str = "SQL X-Ray",
    lang: str | None = None,
    inline: bool = True,
) -> str:
    labels = _LABELS[_resolve_lang(lang)]
    data = [_statement_data(m, labels) for m in models]

    body_parts: list[str] = []
    multi = len(models) > 1
    for i, model in enumerate(models):
        kind = f"<b>{escape(model.statement_kind)}</b>"
        target = (
            f" &rarr; {escape(model.target_table)}" if model.target_table else ""
        )
        if multi:
            head = f"{labels['statement']} {i + 1}: {kind}{target}"
        else:
            head = f"{kind}{target}"
        diagram = escape(_mermaid_with_clicks(model, i))
        body_parts.append(
            f'<section class="stmt"><div class="stmt-head">{head}</div>'
            f'<div class="card"><pre class="mermaid">{diagram}</pre></div></section>'
        )

    kinds = ", ".join(m.statement_kind for m in models)
    sub = (
        f"{len(models)} {labels['statements_label']}: {escape(kinds)} "
        f"&middot; {labels['sub_click']}"
    )

    # Only the labels the client JS needs.
    js_labels = {
        k: labels[k]
        for k in (
            "reads_from",
            "operations",
            "outputs",
            "sql",
            "tables",
            "steps",
            "none",
            "no_details",
        )
    }

    return (
        _PAGE.replace("%%SCRIPTS%%", _scripts(inline))
        .replace("%%TITLE%%", escape(title))
        .replace("%%SUB%%", sub)
        .replace("%%HINT%%", labels["hint"])
        .replace("%%FOOTER%%", labels["footer"])
        .replace("%%BODY%%", "\n".join(body_parts))
        .replace("%%DATA%%", json.dumps(data))
        .replace("%%LABELS%%", json.dumps(js_labels, ensure_ascii=False))
    )
