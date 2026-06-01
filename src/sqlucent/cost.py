"""Schema-driven scan-cost estimate.

BigQuery (on-demand) bills by *bytes scanned* = for every **referenced column**,
its per-row width times the **rows scanned**. Because storage is columnar, a query
only pays for the columns it touches and the partitions it reads — so two levers
dominate cost: column pruning (`SELECT *` vs explicit columns) and partition
pruning (a `WHERE` on the partition column).

Given a schema (column types), this turns the boolean "missing filter" check into a
real number: bytes/row per table, the share of each row you actually scan, and —
when row counts are supplied via `.sqlucent.toml` — absolute bytes and dollars.

Widths follow BigQuery's data-type sizes. Variable-width types (STRING/BYTES/JSON)
use a configurable assumed average, so absolute numbers are estimates, not invoices.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from .config import Config
from .ir import SqlModel
from .lineage import _alias_map, _query_sql

Schema = dict[str, dict[str, str]]

# Fixed-width BigQuery logical types (bytes).
_FIXED = {
    "INT64": 8, "INT": 8, "INTEGER": 8, "SMALLINT": 8, "BIGINT": 8,
    "TINYINT": 8, "BYTEINT": 8,
    "FLOAT64": 8, "FLOAT": 8,
    "NUMERIC": 16, "DECIMAL": 16,
    "BIGNUMERIC": 32, "BIGDECIMAL": 32,
    "BOOL": 1, "BOOLEAN": 1,
    "DATE": 8, "DATETIME": 8, "TIME": 8, "TIMESTAMP": 8,
    "INTERVAL": 16, "GEOGRAPHY": 16,
}
_VARIABLE = {"STRING", "TEXT", "VARCHAR", "CHAR", "BYTES"}


def column_width(type_str: str, string_bytes: int = 16) -> tuple[int, bool]:
    """(bytes, known) for one column type. `known` is False for fuzzy types."""
    base = re.split(r"[(<\s]", (type_str or "").strip().upper(), 1)[0]
    if base in _FIXED:
        return _FIXED[base], True
    if base in _VARIABLE:
        return string_bytes, True  # deliberate assumption (configurable)
    if base == "JSON":
        return string_bytes * 4, True
    if base in ("ARRAY", "STRUCT", "RECORD"):
        return string_bytes, False  # repeated/nested — width unknown
    return 8, False


@dataclass
class TableCost:
    table: str
    scanned_columns: list[str]
    total_columns: int
    bytes_per_row: int          # over scanned columns
    full_bytes_per_row: int     # over all columns in the schema
    rows: int | None
    partition_pruned: bool
    est_bytes: int | None
    has_unknown: bool = False


@dataclass
class CostEstimate:
    tables: list[TableCost] = field(default_factory=list)
    total_bytes: int | None = None
    price_per_tb: float = 6.25
    est_cost_usd: float | None = None
    notes: list[str] = field(default_factory=list)


def _filter_columns(root: exp.Expression) -> set[str]:
    """Column names that appear in WHERE / JOIN ON (i.e. could prune partitions)."""
    cols: set[str] = set()
    for where in root.find_all(exp.Where):
        cols |= {c.name.lower() for c in where.find_all(exp.Column)}
    for join in root.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            cols |= {c.name.lower() for c in on.find_all(exp.Column)}
    return cols


def _referenced_columns(
    sql: str, schema: Schema, dialect: str
) -> tuple[dict[str, set[str]], bool]:
    """Map each physical table (present in `schema`) to the columns it scans.

    Uses sqlglot's qualifier (with the schema) to expand `SELECT *` and attach a
    source table to every column. Returns (refs, had_unresolved_star).
    """
    alias_map = _alias_map(sql, dialect)
    root: exp.Expression | None = None
    try:
        root = qualify(sqlglot.parse_one(sql, read=dialect), schema=schema, dialect=dialect)
    except Exception:
        try:
            root = sqlglot.parse_one(sql, read=dialect)
        except Exception:
            return {}, False

    physical = {alias_map.get(t.name, t.name) for t in root.find_all(exp.Table)}
    single = next(iter(physical & set(schema)), None) if len(physical) == 1 else None

    refs: dict[str, set[str]] = defaultdict(set)
    for col in root.find_all(exp.Column):
        real = alias_map.get(col.table) if col.table else single
        if real and real in schema and col.name in schema[real]:
            refs[real].add(col.name)

    # A surviving `*` outside an aggregate means a table the schema didn't cover.
    star = any(
        not s.find_ancestor(exp.AggFunc)
        for s in root.find_all(exp.Star)
    )
    return refs, star


def estimate_cost(
    model: SqlModel, schema: Schema, config: Config | None = None
) -> CostEstimate:
    """Estimate bytes scanned for the query body, broken down per source table."""
    config = config or Config()
    est = CostEstimate(price_per_tb=config.price_per_tb)

    sql = _query_sql(model)
    if sql is None:
        est.notes.append("no scannable SELECT body (e.g. bare DELETE/UPDATE) — skipped")
        return est

    refs, unresolved_star = _referenced_columns(sql, schema, model.dialect)
    if unresolved_star:
        est.notes.append("a `SELECT *` referenced a table missing from the schema — estimate is a lower bound")
    if not refs:
        est.notes.append("no schema-known columns scanned (metadata-only, e.g. COUNT(*), or unknown tables)")

    try:
        filter_cols = _filter_columns(sqlglot.parse_one(sql, read=model.dialect))
    except Exception:
        filter_cols = set()

    total = 0
    all_known_have_rows = True
    saw_string = False
    for table in sorted(refs):
        cols = sorted(refs[table])
        schema_cols = schema[table]
        bpr = 0
        full = 0
        has_unknown = False
        for c, ty in schema_cols.items():
            w, known = column_width(ty, config.string_bytes)
            full += w
            if not known:
                has_unknown = True
            if re.split(r"[(<\s]", (ty or "").strip().upper(), 1)[0] in _VARIABLE:
                saw_string = True
            if c in refs[table]:
                bpr += w

        rows = config.table_rows.get(table)
        partitioned = table in config.partitions
        filtered = partitioned and config.partitions[table].lower() in filter_cols
        scanned_rows = rows
        if rows is not None and partitioned and filtered:
            scanned_rows = int(rows * config.partition_selectivity)
        est_bytes = scanned_rows * bpr if scanned_rows is not None else None
        if est_bytes is None:
            all_known_have_rows = False
        else:
            total += est_bytes

        est.tables.append(
            TableCost(
                table=table,
                scanned_columns=cols,
                total_columns=len(schema_cols),
                bytes_per_row=bpr,
                full_bytes_per_row=full,
                rows=rows,
                partition_pruned=filtered,
                est_bytes=est_bytes,
                has_unknown=has_unknown,
            )
        )

    if est.tables and all_known_have_rows:
        est.total_bytes = total
        est.est_cost_usd = total / 1e12 * config.price_per_tb
    elif est.tables:
        est.notes.append("set [cost.table_rows] in .sqlucent.toml for absolute byte/$ estimates")
    if saw_string:
        est.notes.append(f"STRING/BYTES assumed {config.string_bytes} B/value (set [cost] string_bytes)")
    return est


def human_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} PB"


def format_cost(est: CostEstimate) -> str:
    """Human-readable estimate report."""
    lines: list[str] = []
    for t in est.tables:
        if t.scanned_columns:
            share = (t.bytes_per_row / t.full_bytes_per_row * 100) if t.full_bytes_per_row else 0
            cols = f"{len(t.scanned_columns)}/{t.total_columns} cols"
            row = (
                f"  {t.table}: {cols}, {t.bytes_per_row} B/row "
                f"({share:.0f}% of {t.full_bytes_per_row} B full row)"
            )
        else:
            row = f"  {t.table}: 0 cols scanned (metadata-only)"
        if t.rows is not None:
            row += f" × {t.rows:,} rows"
            if t.partition_pruned:
                row += " (partition-pruned)"
            row += f" = {human_bytes(t.est_bytes)}"
        if t.has_unknown:
            row += "  [has nested/unknown types]"
        lines.append(row)

    if est.total_bytes is not None:
        lines.append("")
        lines.append(
            f"  TOTAL ≈ {human_bytes(est.total_bytes)} scanned"
            f"  →  ${est.est_cost_usd:,.2f} at ${est.price_per_tb:g}/TB"
        )
    for note in est.notes:
        lines.append(f"  · {note}")
    return "\n".join(lines) if lines else "  (nothing to estimate)"
