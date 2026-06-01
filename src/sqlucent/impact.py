"""Impact analysis on the project DAG.

Answers "if I change this table (or column), what's affected?" — the blast radius.

- **Table impact** is exact: a breadth-first walk of the table-level DAG gives the
  direct consumers and the full transitive downstream set.
- **Column impact** is best-effort (no schema needed): it finds the write
  statements that read the table and reference the column by name, plus those that
  pull it implicitly via ``SELECT *``. Without a schema, an unqualified column in a
  multi-table query can't always be attributed to one table, so results are flagged
  as "explicit" or "via SELECT *" and treated as candidates.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .project import ProjectGraph


def direct_dependents(graph: ProjectGraph, table: str) -> list[str]:
    return sorted({e.dst for e in graph.edges if e.src == table})


def downstream_tables(graph: ProjectGraph, table: str) -> list[str]:
    """All tables transitively fed by `table` (BFS, excludes the table itself)."""
    adj: dict[str, list[str]] = {}
    for e in graph.edges:
        adj.setdefault(e.src, []).append(e.dst)

    seen: set[str] = set()
    order: list[str] = []
    queue = deque(adj.get(table, []))
    while queue:
        node = queue.popleft()
        if node in seen or node == table:
            continue
        seen.add(node)
        order.append(node)
        queue.extend(adj.get(node, []))
    return order


def upstream_tables(graph: ProjectGraph, table: str) -> list[str]:
    """All tables that transitively feed `table`."""
    radj: dict[str, list[str]] = {}
    for e in graph.edges:
        radj.setdefault(e.dst, []).append(e.src)

    seen: set[str] = set()
    order: list[str] = []
    queue = deque(radj.get(table, []))
    while queue:
        node = queue.popleft()
        if node in seen or node == table:
            continue
        seen.add(node)
        order.append(node)
        queue.extend(radj.get(node, []))
    return order


@dataclass
class ColumnHit:
    file: str
    kind: str
    target: str | None
    how: str  # "explicit" | "via SELECT *"


def column_impact(graph: ProjectGraph, table: str, column: str) -> list[ColumnHit]:
    """Write statements that read `table` and (may) use `table.column`."""
    hits: list[ColumnHit] = []
    for st in graph.statements:
        if table not in st.reads or not st.sql:
            continue
        try:
            root = sqlglot.parse_one(st.sql)
        except Exception:
            continue
        names = {c.name for c in root.find_all(exp.Column)}
        if column in names:
            how = "explicit"
        elif any(True for _ in root.find_all(exp.Star)):
            how = "via SELECT *"
        else:
            continue
        hits.append(ColumnHit(file=st.file, kind=st.kind, target=st.target, how=how))
    return hits


def impact_report(graph: ProjectGraph, target: str) -> str:
    """Human-readable blast radius for a `table` or `table.column` target."""
    table, _, column = target.partition(".")
    known = set(graph.tables)

    lines: list[str] = []
    if table not in known:
        lines.append(f"Note: table `{table}` not found among {len(known)} project tables.")

    if column:
        lines.append(f"Column impact for `{table}.{column}` (best-effort, no schema):")
        hits = column_impact(graph, table, column)
        if not hits:
            lines.append("  (no write statement reads this table referencing that column)")
        for h in hits:
            tgt = f"→ writes `{h.target}`" if h.target else "(read-only)"
            lines.append(f"  · {h.file} [{h.kind}] {tgt} — {h.how}")
        affected = sorted({h.target for h in hits if h.target})
        if affected:
            lines.append("  directly affected tables: " + ", ".join(affected))
        return "\n".join(lines)

    direct = direct_dependents(graph, table)
    down = downstream_tables(graph, table)
    lines.append(f"Table impact for `{table}`:")
    lines.append(f"  direct consumers ({len(direct)}): " + (", ".join(direct) if direct else "—"))
    lines.append(
        f"  transitive downstream ({len(down)}): " + (", ".join(down) if down else "—")
    )
    files = sorted({f for e in graph.edges if e.src == table for f in e.files})
    if files:
        lines.append("  first-hop files: " + ", ".join(files))
    return "\n".join(lines)
