"""AST -> IR.

This is the deterministic core. It uses sqlglot to parse, then walks each query
scope to extract sources and operations. No guessing, no LLM.
"""
from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from .ir import Operation, QueryNode, SqlModel
from .preprocess import has_templating, strip_templating

# We parse with ErrorLevel.IGNORE on purpose (unsupported statements degrade to
# UNSUPPORTED), so sqlglot's "falling back to Command" warnings are just noise.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

_MAX_DETAIL = 120


def _truncate(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 1] + "\u2026"


def _local_tables(select: exp.Expression) -> list[str]:
    """Table/CTE names referenced in *this* scope only.

    We skip tables that live inside a nested subquery (a deeper SELECT), so that a
    node's sources reflect what it directly reads rather than everything beneath it.
    """
    names: list[str] = []
    for tbl in select.find_all(exp.Table):
        # Is this table nested inside a deeper SELECT than `select`?
        ancestor = tbl.find_ancestor(exp.Select)
        if ancestor is not select:
            continue
        name = tbl.name
        if name and name not in names:
            names.append(name)
    return names


def _operations(select: exp.Expression) -> list[Operation]:
    ops: list[Operation] = []

    if isinstance(select, exp.Select) and select.args.get("distinct"):
        ops.append(Operation("distinct", "deduplicate rows", brief="distinct"))

    for join in select.args.get("joins") or []:
        side = (join.side or join.kind or "inner").upper()
        target = (
            join.this.name if isinstance(join.this, exp.Table) else join.this.sql()
        )
        on = join.args.get("on")
        detail = f"{side} JOIN {target}"
        if on is not None:
            detail += f" ON {on.sql()}"
        ops.append(Operation("join", detail, brief=target))

    where = select.args.get("where")
    if where is not None:
        ops.append(Operation("filter", where.this.sql()))

    group = select.args.get("group")
    if group is not None:
        cols = [e.sql() for e in group.expressions]
        ops.append(
            Operation("group", ", ".join(cols), brief=f"{len(cols)} column(s)")
        )

    # Aggregate functions present anywhere in the projection of this scope.
    agg_funcs = sorted(
        {
            f.sql_name()
            for f in select.find_all(exp.AggFunc)
            if f.find_ancestor(exp.Select) is select
        }
    )
    if agg_funcs:
        ops.append(Operation("aggregate", ", ".join(agg_funcs)))

    having = select.args.get("having")
    if having is not None:
        ops.append(Operation("having", having.this.sql()))

    windows = [
        w for w in select.find_all(exp.Window) if w.find_ancestor(exp.Select) is select
    ]
    for w in windows:
        func = w.this.sql_name() if hasattr(w.this, "sql_name") else "window"
        ops.append(Operation("window", w.sql(), brief=f"{func}() OVER (...)"))

    order = select.args.get("order")
    if order is not None:
        cols = [e.sql() for e in order.expressions]
        ops.append(Operation("order", ", ".join(cols), brief=f"{len(cols)} key(s)"))

    limit = select.args.get("limit")
    if limit is not None:
        ops.append(Operation("limit", limit.expression.sql()))

    return ops


def _output_columns(select: exp.Expression) -> list[str]:
    cols: list[str] = []
    for proj in getattr(select, "expressions", []) or []:
        cols.append(proj.alias_or_name or _truncate(proj.sql()))
    return cols


def _node_from_select(
    name: str, select: exp.Expression, cte_names: set[str], *, is_final: bool
) -> QueryNode:
    referenced = _local_tables(select)
    source_ctes = [n for n in referenced if n in cte_names]
    source_tables = [n for n in referenced if n not in cte_names]
    try:
        sql_text = select.sql(pretty=True)
    except Exception:
        sql_text = ""
    return QueryNode(
        name=name,
        is_final=is_final,
        source_tables=source_tables,
        source_ctes=source_ctes,
        operations=_operations(select),
        output_columns=_output_columns(select),
        sql_text=sql_text,
    )


def _set_assignments(stmt: exp.Expression) -> tuple[str, list[str]]:
    """(joined `col = expr` text, written column names) for a SET clause."""
    parts: list[str] = []
    cols: list[str] = []
    for e in stmt.args.get("expressions") or []:
        parts.append(e.sql())
        if isinstance(e, exp.EQ):
            left = e.this
            name = left.name if isinstance(left, exp.Column) else left.sql()
            if name and name not in cols:
                cols.append(name)
    return ", ".join(parts), cols


def _other_tables(stmt: exp.Expression, exclude: str | None) -> list[str]:
    """Base tables referenced by `stmt`, minus the write target itself."""
    names: list[str] = []
    for t in stmt.find_all(exp.Table):
        if t.name and t.name != exclude and t.name not in names:
            names.append(t.name)
    return names


def _table_name(expr: object) -> str | None:
    # sqlglot's Command fallback stores its payload as a plain string, so guard
    # against anything that isn't a parsed expression.
    if not isinstance(expr, exp.Expression):
        return None
    if isinstance(expr, exp.Table):
        return expr.name
    tbl = expr.find(exp.Table)
    return tbl.name if tbl else _truncate(expr.sql())


def _query_nodes(query: exp.Expression, cte_names: set[str]) -> list[QueryNode]:
    """Nodes for a query-bearing expression (SELECT / UNION), including its CTEs."""
    nodes: list[QueryNode] = []
    for cte in getattr(query, "ctes", []) or []:
        nodes.append(_node_from_select(cte.alias, cte.this, cte_names, is_final=False))

    final = query.copy()
    with_node = final.find(exp.With)
    if with_node is not None:
        with_node.pop()
    nodes.append(_node_from_select("result", final, cte_names, is_final=True))
    return nodes


def _analyze_update(stmt: exp.Expression, dialect: str, templated: bool) -> SqlModel:
    target = _table_name(stmt.this)
    detail, written = _set_assignments(stmt)
    sources = _other_tables(stmt, exclude=target)

    ops: list[Operation] = []
    where = stmt.args.get("where")
    if where is not None:
        ops.append(Operation("filter", where.this.sql()))
    if detail:
        ops.append(Operation("set", detail, brief=f"{len(written)} column(s)"))

    try:
        sql_text = stmt.sql(pretty=True)
    except Exception:
        sql_text = ""
    node = QueryNode(
        name="update",
        is_final=True,
        source_tables=sources,
        operations=ops,
        output_columns=written,
        sql_text=sql_text,
    )
    return SqlModel(
        dialect=dialect,
        nodes=[node],
        statement_kind="UPDATE",
        target_table=target,
        templated=templated,
    )


def _merge_source_node(using: exp.Expression) -> tuple[QueryNode | None, str | None]:
    """A node for the MERGE source. Returns (node, table_name): a derived source
    becomes its own node; a plain table is just a name the merge node reads."""
    if isinstance(using, exp.Table):
        return None, using.name

    alias = using.alias or "source"
    tables: list[str] = []
    for t in using.find_all(exp.Table):
        if t.name and t.name not in tables:
            tables.append(t.name)
    inner = using.this if isinstance(using, exp.Subquery) else using
    ops = _operations(inner) if isinstance(inner, exp.Select) else []
    try:
        sql_text = using.sql(pretty=True)
    except Exception:
        sql_text = ""
    node = QueryNode(
        name=alias,
        is_final=False,
        source_tables=tables,
        operations=ops,
        sql_text=sql_text,
    )
    return node, None


def _when_op(when: exp.Expression) -> tuple[Operation, list[str]]:
    wsql = " ".join(when.sql().split())
    if " THEN " in wsql:
        head, tail = wsql.split(" THEN ", 1)
        action = tail.split()[0].upper() if tail.split() else "?"
    else:
        head, action = wsql, "?"
    cond = head.replace("WHEN ", "", 1)
    brief = f"{cond} \u2192 {action}"

    written: list[str] = []
    then = when.args.get("then")
    if isinstance(then, exp.Update):
        _, written = _set_assignments(then)
    return Operation("merge-when", wsql, brief=brief), written


def _analyze_merge(stmt: exp.Expression, dialect: str, templated: bool) -> SqlModel:
    target = _table_name(stmt.this)
    nodes: list[QueryNode] = []

    src_node, src_table = (None, None)
    using = stmt.args.get("using")
    if using is not None:
        src_node, src_table = _merge_source_node(using)
    if src_node is not None:
        nodes.append(src_node)

    merge_ops: list[Operation] = []
    on = stmt.args.get("on")
    if on is not None:
        merge_ops.append(Operation("match", on.sql(), brief="match key"))

    written_all: list[str] = []
    whens = stmt.args.get("whens")
    for w in getattr(whens, "expressions", []) or []:
        op, written = _when_op(w)
        merge_ops.append(op)
        for c in written:
            if c not in written_all:
                written_all.append(c)

    try:
        sql_text = stmt.sql(pretty=True)
    except Exception:
        sql_text = ""
    merge_node = QueryNode(
        name="merge",
        is_final=True,
        source_tables=[src_table] if src_table else [],
        source_ctes=[src_node.name] if src_node is not None else [],
        operations=merge_ops,
        output_columns=written_all,
        sql_text=sql_text,
    )
    nodes.append(merge_node)
    return SqlModel(
        dialect=dialect,
        nodes=nodes,
        statement_kind="MERGE",
        target_table=target,
        templated=templated,
    )


def _analyze_statement(stmt: exp.Expression, dialect: str, templated: bool) -> SqlModel:
    # INSERT INTO target [SELECT ...] / CREATE TABLE target AS SELECT ...
    if isinstance(stmt, (exp.Insert, exp.Create)):
        kind = "INSERT" if isinstance(stmt, exp.Insert) else "CREATE"
        target = _table_name(stmt.this)
        query = stmt.expression
        if isinstance(query, exp.Query):
            cte_names = {c.alias for c in getattr(query, "ctes", []) or []}
            nodes = _query_nodes(query, cte_names)
        else:
            nodes = [QueryNode(name="result", is_final=True)]
        return SqlModel(
            dialect=dialect,
            nodes=nodes,
            statement_kind=kind,
            target_table=target,
            templated=templated,
        )

    # DELETE FROM target WHERE ...
    if isinstance(stmt, exp.Delete):
        target = _table_name(stmt.this)
        try:
            del_sql = stmt.sql(pretty=True)
        except Exception:
            del_sql = ""
        node = QueryNode(
            name="delete",
            is_final=True,
            source_tables=[target] if target else [],
            operations=_operations(stmt),
            sql_text=del_sql,
        )
        return SqlModel(
            dialect=dialect,
            nodes=[node],
            statement_kind="DELETE",
            target_table=target,
            templated=templated,
        )

    # UPDATE target SET ... [FROM source] WHERE ...
    if isinstance(stmt, exp.Update):
        return _analyze_update(stmt, dialect, templated)

    # MERGE target USING source ON ... WHEN MATCHED/NOT MATCHED THEN ...
    if isinstance(stmt, exp.Merge):
        return _analyze_merge(stmt, dialect, templated)

    # Plain query: SELECT / UNION / ...
    if isinstance(stmt, exp.Query):
        cte_names = {c.alias for c in getattr(stmt, "ctes", []) or []}
        nodes = _query_nodes(stmt, cte_names)
        return SqlModel(
            dialect=dialect, nodes=nodes, statement_kind="SELECT", templated=templated
        )

    # Statements sqlglot couldn't fully parse fall back to a generic Command;
    # surface them as UNSUPPORTED rather than pretending we understood them.
    if isinstance(stmt, exp.Command):
        return SqlModel(
            dialect=dialect,
            nodes=[],
            statement_kind="UNSUPPORTED",
            templated=templated,
        )

    # Fallback for anything else (UPDATE, MERGE, other DDL...): don't crash.
    kind = type(stmt).__name__.upper()
    return SqlModel(
        dialect=dialect,
        nodes=[QueryNode(name="result", is_final=True)],
        statement_kind=kind,
        target_table=_table_name(stmt.args.get("this")),
        templated=templated,
    )


def analyze_script(sql: str, dialect: str = "bigquery") -> list[SqlModel]:
    """Parse a (possibly multi-statement, possibly templated) script into models.

    Uses a lenient error level so one unsupported statement (e.g. Redshift
    ``COPY ... INTO``) degrades to UNSUPPORTED instead of failing the whole file.
    """
    templated = has_templating(sql)
    clean = strip_templating(sql) if templated else sql
    statements = sqlglot.parse(clean, read=dialect, error_level=ErrorLevel.IGNORE)
    models: list[SqlModel] = []
    for stmt in statements:
        if stmt is None or isinstance(stmt, exp.Semicolon):
            continue
        model = _analyze_statement(stmt, dialect, templated)
        try:
            model.statement_sql = stmt.sql(pretty=True)
        except Exception:
            model.statement_sql = ""
        models.append(model)
    return models


def analyze(sql: str, dialect: str = "bigquery") -> SqlModel:
    """Parse `sql` and return the semantic model of its first statement."""
    models = analyze_script(sql, dialect)
    if not models:
        return SqlModel(dialect=dialect, nodes=[])
    return models[0]
