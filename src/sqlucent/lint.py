"""Deterministic risk lint.

Rules are pure pattern matches over the parsed AST — no LLM, no guessing. Each
finding names a rule, a severity, the node (CTE/result) it occurred in, and a
human message. This is the kind of check that slots into CI as a gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import sqlglot
from sqlglot import exp

from .config import Config
from .ir import SqlModel

SEVERITIES = ("low", "medium", "high")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class Finding:
    rule: str
    severity: str
    node: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def _scopes(root: exp.Expression) -> list[tuple[str, exp.Expression]]:
    """(name, select) pairs: each CTE plus the final query, each WITH detached."""
    # Unwrap INSERT/CREATE ... AS SELECT so we lint the underlying query.
    if isinstance(root, (exp.Insert, exp.Create)):
        inner = root.expression
        if not isinstance(inner, exp.Query):
            return []
        root = inner

    scopes: list[tuple[str, exp.Expression]] = []
    for cte in getattr(root, "ctes", []) or []:
        scopes.append((cte.alias, cte.this))
    final = root.copy()
    with_node = final.find(exp.With)
    if with_node is not None:
        with_node.pop()
    scopes.append(("result", final))
    return scopes


def _scope_tables(sel: exp.Select) -> list[exp.Table]:
    """Tables referenced directly in this scope (not nested subqueries)."""
    return [t for t in sel.find_all(exp.Table) if t.find_ancestor(exp.Select) is sel]


def _lint_select(name: str, sel: exp.Expression, config: Config) -> list[Finding]:
    findings: list[Finding] = []
    if not isinstance(sel, exp.Select):
        return findings

    # 1. Cartesian join: a condition-less join between real tables. Covers comma
    # joins and explicit CROSS JOIN (sqlglot normalizes a dangling JOIN to either),
    # but not CROSS JOIN UNNEST(...) or LATERAL, which are legitimately conditionless.
    for join in sel.args.get("joins") or []:
        has_cond = join.args.get("on") is not None or join.args.get("using")
        side = (join.side or "").upper()
        if not has_cond and isinstance(join.this, exp.Table) and side != "LATERAL":
            target = join.this.name
            findings.append(
                Finding(
                    "cartesian-join",
                    "high",
                    name,
                    f"join on `{target}` has no ON/USING — likely an accidental "
                    "cross join (row explosion).",
                )
            )

    # 2. SELECT * widening (top-level projection only).
    for proj in sel.expressions:
        if isinstance(proj, exp.Star) or (
            isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)
        ):
            findings.append(
                Finding(
                    "select-star",
                    "medium",
                    name,
                    "`SELECT *` pulls every column — brittle to schema changes and "
                    "wider scans than needed.",
                )
            )
            break

    # 3. HAVING that could be a WHERE (no aggregate in the condition).
    having = sel.args.get("having")
    if having is not None and not list(having.find_all(exp.AggFunc)):
        findings.append(
            Finding(
                "having-without-aggregate",
                "medium",
                name,
                "HAVING has no aggregate — it filters rows, not groups, so it "
                "belongs in WHERE (filter earlier = cheaper).",
            )
        )

    # 4. Redundant DISTINCT alongside GROUP BY.
    if sel.args.get("distinct") and sel.args.get("group"):
        findings.append(
            Finding(
                "distinct-with-group-by",
                "low",
                name,
                "DISTINCT together with GROUP BY is redundant — GROUP BY already "
                "produces distinct groups.",
            )
        )

    # 5. BigQuery cost: a declared partitioned table scanned without a filter on
    # its partition column == full-table scan == $$$. Only fires for tables that
    # the project declares in `[cost.partitions]`.
    if config.require_partition_filter and config.partitions:
        where = sel.args.get("where")
        filtered_cols = (
            {c.name.lower() for c in where.find_all(exp.Column)} if where else set()
        )
        for tbl in _scope_tables(sel):
            part_col = config.partitions.get(tbl.name)
            if part_col and part_col.lower() not in filtered_cols:
                findings.append(
                    Finding(
                        "partition-filter-missing",
                        "high",
                        name,
                        f"`{tbl.name}` is partitioned on `{part_col}` but the scan "
                        f"has no WHERE filter on `{part_col}` — this reads every "
                        "partition (full-table scan, expensive in BigQuery).",
                    )
                )

    return findings


def lint(model: SqlModel, config: Config | None = None) -> list[Finding]:
    """Risk findings for one statement, ordered by descending severity.

    `config` (from `.sqlucent.toml`) toggles rules, overrides severities, and
    enables the partition-filter cost rule. Defaults to an empty config.
    """
    config = config or Config()
    if not model.statement_sql:
        return []
    try:
        root = sqlglot.parse_one(model.statement_sql, read=model.dialect)
    except Exception:
        return []

    findings: list[Finding] = []

    # Statement-level: an UPDATE/DELETE with no WHERE rewrites/deletes every row.
    if isinstance(root, (exp.Update, exp.Delete)) and root.args.get("where") is None:
        verb = "UPDATE" if isinstance(root, exp.Update) else "DELETE"
        findings.append(
            Finding(
                "full-table-write",
                "high",
                "result",
                f"{verb} has no WHERE clause — it affects every row in the table.",
            )
        )

    for name, sel in _scopes(root):
        findings.extend(_lint_select(name, sel, config))
    findings = config.apply(findings)
    findings.sort(key=lambda f: -_SEV_RANK.get(f.severity, 0))
    return findings


def max_severity(findings: list[Finding]) -> str | None:
    if not findings:
        return None
    return max(findings, key=lambda f: _SEV_RANK.get(f.severity, 0)).severity


def meets_threshold(findings: list[Finding], threshold: str) -> bool:
    """True if any finding is at or above `threshold` severity."""
    bar = _SEV_RANK.get(threshold, 0)
    return any(_SEV_RANK.get(f.severity, 0) >= bar for f in findings)
