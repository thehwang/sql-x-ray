"""Column-level lineage.

Traces each final output column back to the source columns it derives from, using
sqlglot's lineage engine (which follows the column through CTEs and subqueries).
Works without a schema for explicit column references; `SELECT *` and some
unaliased expressions can't be resolved by name and are reported as such.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as _sqlglot_lineage

from .ir import SqlModel


@dataclass
class ColumnLineage:
    column: str
    sources: list[str]
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _query_sql(model: SqlModel) -> str | None:
    """The SELECT body to trace (unwrap INSERT/CREATE ... AS SELECT)."""
    if not model.statement_sql:
        return None
    try:
        root = sqlglot.parse_one(model.statement_sql, read=model.dialect)
    except Exception:
        return None
    if isinstance(root, (exp.Insert, exp.Create)):
        inner = root.expression
        return inner.sql(dialect=model.dialect) if isinstance(inner, exp.Query) else None
    if isinstance(root, exp.Query):
        return model.statement_sql
    return None


def _trace(column: str, sql: str, dialect: str) -> ColumnLineage:
    try:
        node = _sqlglot_lineage(column, sql, dialect=dialect)
    except Exception as exc:
        return ColumnLineage(column, [], note=f"unresolved ({type(exc).__name__})")

    leaves = [n for n in node.walk() if not n.downstream]
    sources: list[str] = []
    for leaf in leaves:
        nm = leaf.name
        if nm and nm != column and nm not in sources:
            sources.append(nm)
    if not sources:
        return ColumnLineage(column, [], note="no source column (literal/derived)")
    return ColumnLineage(column, sources)


def column_lineage(model: SqlModel, column: str | None = None) -> list[ColumnLineage]:
    """Lineage for one column, or for all final output columns when `column` is None."""
    sql = _query_sql(model)
    if sql is None:
        return []

    final = model.final
    if column is not None:
        targets = [column]
    elif final is not None:
        # Only name-like outputs can be traced by name; skip '*' and bare expressions.
        targets = [
            c for c in final.output_columns if c and c != "*" and "(" not in c
        ]
    else:
        targets = []

    return [_trace(col, sql, model.dialect) for col in targets]
