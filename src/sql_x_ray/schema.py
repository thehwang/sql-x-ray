"""Optional schema binding.

Feeding column types lets the lineage engine expand ``SELECT *`` into real
columns and resolve unqualified references — turning "can't trace by name" into
precise per-column lineage. The schema is a plain ``{table: {column: type}}``
mapping, built either from DDL (``CREATE TABLE`` statements) or a JSON file.
"""
from __future__ import annotations

import json

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from .preprocess import has_templating, strip_templating

Schema = dict[str, dict[str, str]]


def build_schema_from_ddl(ddl: str, dialect: str = "bigquery") -> Schema:
    """Extract ``{table: {column: type}}`` from CREATE TABLE statements.

    CTAS / column-less creates are skipped. Tables are keyed by their (unqualified)
    name so they match how queries usually reference them.
    """
    clean = strip_templating(ddl) if has_templating(ddl) else ddl
    schema: Schema = {}
    for stmt in sqlglot.parse(clean, read=dialect, error_level=ErrorLevel.IGNORE):
        if not isinstance(stmt, exp.Create):
            continue
        target = stmt.this
        if not isinstance(target, exp.Schema):
            continue  # e.g. CREATE ... AS SELECT (no explicit columns)
        table = target.this
        name = table.name if isinstance(table, exp.Table) else None
        if not name:
            continue
        cols: dict[str, str] = {}
        for cdef in target.find_all(exp.ColumnDef):
            kind = cdef.args.get("kind")
            cols[cdef.name] = kind.sql(dialect=dialect) if kind is not None else "UNKNOWN"
        if cols:
            schema[name] = cols
    return schema


def load_schema(path: str, dialect: str = "bigquery") -> Schema:
    """Load a schema from a ``.json`` mapping or a ``.sql`` DDL file."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".json"):
        data = json.loads(text)
        return {str(t): {str(c): str(ty) for c, ty in cols.items()} for t, cols in data.items()}
    return build_schema_from_ddl(text, dialect=dialect)
