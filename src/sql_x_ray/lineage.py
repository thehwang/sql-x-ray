"""Column-level lineage.

Traces each final output column back to the source columns it derives from, using
sqlglot's lineage engine (which follows the column through CTEs and subqueries).
Without a schema, only explicit column references resolve — `SELECT *` and some
unaliased expressions can't be traced by name. Pass a schema (see `schema.py`) to
expand `SELECT *` into real columns and trace each precisely.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as _sqlglot_lineage
from sqlglot.optimizer.qualify import qualify

from .ir import SqlModel

Schema = dict[str, dict[str, str]]


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


def _alias_map(sql: str, dialect: str) -> dict[str, str]:
    """Map each table alias (and bare name) to its base table name, so lineage
    leaves like `e.user_id` can be reported as `events.user_id`."""
    out: dict[str, str] = {}
    try:
        root = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return out
    for tbl in root.find_all(exp.Table):
        if tbl.name:
            out[tbl.name] = tbl.name
            if tbl.alias:
                out[tbl.alias] = tbl.name
    return out


def _resolve(name: str, alias_map: dict[str, str]) -> str:
    if "." in name:
        head, rest = name.split(".", 1)
        base = alias_map.get(head)
        if base and base != head:
            return f"{base}.{rest}"
    return name


def _expanded_targets(sql: str, schema: Schema, dialect: str) -> list[str] | None:
    """With a schema, expand `SELECT *` into concrete output column names."""
    try:
        qualified = qualify(sqlglot.parse_one(sql, read=dialect), schema=schema, dialect=dialect)
    except Exception:
        return None
    names = [s.alias_or_name for s in qualified.selects if s.alias_or_name]
    return names or None


def _trace(
    column: str, sql: str, dialect: str, schema: Schema | None, alias_map: dict[str, str]
) -> ColumnLineage:
    try:
        node = _sqlglot_lineage(column, sql, schema=schema, dialect=dialect)
    except Exception as exc:
        return ColumnLineage(column, [], note=f"unresolved ({type(exc).__name__})")

    sources: list[str] = []
    for leaf in (n for n in node.walk() if not n.downstream):
        nm = _resolve(leaf.name, alias_map) if leaf.name else ""
        if nm and nm != column and nm not in sources:
            sources.append(nm)
    if not sources:
        return ColumnLineage(column, [], note="no source column (literal/derived)")
    return ColumnLineage(column, sources)


def column_lineage(
    model: SqlModel, column: str | None = None, schema: Schema | None = None
) -> list[ColumnLineage]:
    """Lineage for one column, or for all final output columns when `column` is None.

    Pass `schema` ({table: {column: type}}) to expand `SELECT *` and resolve
    unqualified columns precisely.
    """
    sql = _query_sql(model)
    if sql is None:
        return []

    alias_map = _alias_map(sql, model.dialect)
    final = model.final

    if column is not None:
        targets: list[str] = [column]
    elif schema is not None:
        targets = _expanded_targets(sql, schema, model.dialect) or []
        if not targets and final is not None:
            targets = [c for c in final.output_columns if c and c != "*" and "(" not in c]
    elif final is not None:
        # Only name-like outputs can be traced by name; skip '*' and bare expressions.
        targets = [c for c in final.output_columns if c and c != "*" and "(" not in c]
    else:
        targets = []

    rows: list[ColumnLineage] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for col in targets:
        row = _trace(col, sql, model.dialect, schema, alias_map)
        key = (row.column, tuple(row.sources))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows
