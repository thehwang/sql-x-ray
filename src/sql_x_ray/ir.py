"""Semantic model (IR) for SQL X-Ray.

The IR is the hub of the pipeline: it is produced once by the analyzer and then
consumed independently by the graph generator, the walkthrough generator, and
(later) lineage and lint. Keeping it a plain, serializable data structure is what
lets the deterministic core stay testable and the LLM stay at the outer edge.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Operation:
    """A single relational operation a query node performs.

    ``detail`` is the full, untruncated text (e.g. a join's ON clause or the whole
    GROUP BY list); ``brief`` is a short label used in non-verbose rendering (e.g.
    the joined table name, or "16 columns"). Truncation happens at render time so
    ``--verbose`` can show everything.
    """

    kind: str  # filter | aggregate | group | having | join | window | order | limit | distinct
    detail: str = ""
    brief: str = ""


@dataclass
class QueryNode:
    """One CTE, or the final SELECT, in the query.

    ``source_tables`` are base (physical) tables this node reads from;
    ``source_ctes`` are other nodes in the same query it depends on. Together they
    form the edges of the data-flow DAG.
    """

    name: str
    is_final: bool = False
    source_tables: list[str] = field(default_factory=list)
    source_ctes: list[str] = field(default_factory=list)
    operations: list[Operation] = field(default_factory=list)
    output_columns: list[str] = field(default_factory=list)
    sql_text: str = ""  # the SQL of this node, for interactive highlighting

    def op_kinds(self) -> list[str]:
        return [op.kind for op in self.operations]


@dataclass
class SqlModel:
    """One statement as a list of nodes in definition order (final node last).

    A script (multiple statements) is a list of ``SqlModel``. ``statement_kind`` is
    SELECT / INSERT / DELETE / CREATE / etc.; ``target_table`` is the table a
    write statement (INSERT/CREATE/DELETE/UPDATE/MERGE) lands in.
    """

    dialect: str
    nodes: list[QueryNode] = field(default_factory=list)
    statement_kind: str = "SELECT"
    target_table: str | None = None
    templated: bool = False
    statement_sql: str = ""  # full statement SQL (templating stripped), for lint/lineage

    @property
    def final(self) -> QueryNode | None:
        return next((n for n in self.nodes if n.is_final), None)

    @property
    def ctes(self) -> list[QueryNode]:
        return [n for n in self.nodes if not n.is_final]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
