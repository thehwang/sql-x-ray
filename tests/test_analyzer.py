from pathlib import Path

from sqlucent import (
    analyze,
    analyze_project,
    analyze_script,
    build_factsheet,
    build_schema_from_ddl,
    column_impact,
    column_lineage,
    downstream_tables,
    lint,
    meets_threshold,
    project_mermaid,
    to_html,
    to_mermaid,
    walkthrough,
)
from sqlucent import Config, column_width, estimate_cost, load_config
from sqlucent.preprocess import strip_templating

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "top_users.sql"


def _model():
    return analyze(EXAMPLE.read_text(), dialect="bigquery")


def test_nodes_and_order():
    model = _model()
    names = [n.name for n in model.nodes]
    assert names == ["active", "paid", "ranked", "result"]
    assert model.final is not None and model.final.name == "result"
    assert [n.name for n in model.ctes] == ["active", "paid", "ranked"]


def test_sources_split_tables_vs_ctes():
    model = _model()
    by_name = {n.name: n for n in model.nodes}

    assert by_name["active"].source_tables == ["events"]
    assert by_name["active"].source_ctes == []

    assert by_name["paid"].source_tables == ["orders"]

    # ranked joins CTE `paid` with base table `users`
    assert by_name["ranked"].source_ctes == ["paid"]
    assert by_name["ranked"].source_tables == ["users"]

    # final reads from CTEs ranked + active, no base tables
    assert set(by_name["result"].source_ctes) == {"ranked", "active"}
    assert by_name["result"].source_tables == []


def test_operations_detected():
    model = _model()
    by_name = {n.name: n for n in model.nodes}

    assert "filter" in by_name["active"].op_kinds()
    assert "group" in by_name["active"].op_kinds()
    assert "aggregate" in by_name["active"].op_kinds()

    assert "having" in by_name["paid"].op_kinds()
    assert "window" in by_name["ranked"].op_kinds()
    assert "join" in by_name["ranked"].op_kinds()
    assert "filter" in by_name["result"].op_kinds()


def test_renderers_run():
    model = _model()
    mer = to_mermaid(model)
    assert mer.startswith("flowchart TD")
    # base tables appear as declared cylinder nodes
    assert 't_events[("events")]' in mer
    assert "-->" in mer

    wt = walkthrough(model)
    assert "final result" in wt
    assert "`active`" in wt


# --- real-world robustness: Jinja templating + multi-statement + DELETE/INSERT ---

AIRFLOW_SQL = """
-- {"dag_id": "{{ dag.dag_id }}", "task_id": "{{ task.task_id }}"}
DELETE FROM {{ params.target_schema }}.agg_activity
WHERE conv_date_id >= 20240101;

INSERT INTO {{ params.target_schema }}.agg_activity
SELECT campaign_id, SUM(revenue)
FROM {{ params.target_schema }}.f_activity
WHERE conv_date_id >= 20240101
GROUP BY campaign_id;
"""


def test_strip_templating():
    out = strip_templating("SELECT * FROM {{ params.target_schema }}.t")
    assert "{{" not in out and "}}" not in out
    assert "jinja_params_target_schema.t" in out


def test_multi_statement_template():
    models = analyze_script(AIRFLOW_SQL, dialect="bigquery")
    assert len(models) == 2

    delete, insert = models
    assert delete.statement_kind == "DELETE"
    assert delete.target_table == "agg_activity"
    assert delete.templated is True
    assert "filter" in delete.nodes[0].op_kinds()

    assert insert.statement_kind == "INSERT"
    assert insert.target_table == "agg_activity"
    assert insert.final.source_tables == ["f_activity"]
    assert "aggregate" in insert.final.op_kinds()
    assert "group" in insert.final.op_kinds()

    # graph shows the write target as a sink
    mer = to_mermaid(insert)
    assert "INSERT" in mer and "agg_activity" in mer
    assert "==>" in mer


WIDE_JOIN_SQL = """
SELECT f.id, MIN(f.x)
FROM facts f
LEFT JOIN d1 ON f.a = d1.a
LEFT JOIN d2 ON f.b = d2.b
LEFT JOIN d3 ON f.c = d3.c
LEFT JOIN d4 ON f.e = d4.e
LEFT JOIN d5 ON f.g = d5.g
LEFT JOIN d6 ON f.h = d6.h
LEFT JOIN d7 ON f.i = d7.i
GROUP BY c1, c2, c3, c4, c5, c6, c7, c8, c9, c10
"""


def test_walkthrough_collapses_wide_joins_and_lists():
    model = analyze(WIDE_JOIN_SQL, dialect="bigquery")

    concise = walkthrough(model, verbose=False)
    # 7 joins collapse into a single summary line naming a few targets
    assert "joins 7 table(s):" in concise
    assert "d1" in concise and "+1 more" in concise  # 7 targets, first 6 shown
    # 10 group-by columns collapse to a count + preview
    assert "groups by 10 columns" in concise
    # full ON clauses are NOT in the concise output
    assert "f.a = d1.a" not in concise

    verbose = walkthrough(model, verbose=True)
    # verbose shows each join's ON clause and the full column list
    assert "f.a = d1.a" in verbose
    assert "joins via" in verbose
    assert "c10" in verbose


def test_factsheet_is_grounded():
    model = analyze(EXAMPLE.read_text(), dialect="bigquery")
    facts = build_factsheet(model)
    # The factsheet is the ground truth handed to the LLM: it must name the real
    # tables/steps and statement kind, so the model has no need to invent any.
    assert "STATEMENT KIND: SELECT" in facts
    for tbl in ("events", "orders", "users"):
        assert tbl in facts
    for step in ("active", "paid", "ranked"):
        assert step in facts
    assert "window" in facts


def test_node_sql_text_populated():
    model = analyze(EXAMPLE.read_text(), dialect="bigquery")
    by_name = {n.name: n for n in model.nodes}
    assert "MAX" in by_name["active"].sql_text
    assert "ROW_NUMBER" in by_name["ranked"].sql_text


def test_html_is_self_contained_and_interactive():
    models = analyze_script(EXAMPLE.read_text(), dialect="bigquery")
    page = to_html(models)
    assert page.startswith("<!doctype html>")
    assert "mermaid" in page
    # click wiring + embedded data so the page works with no server
    assert "sxrSelect" in page
    assert "flowchart TD" in page
    assert '"ranked"' in page  # node data embedded as JSON
    assert "reads from" in page  # default English UI chrome


def test_html_inline_vs_cdn():
    models = analyze_script(EXAMPLE.read_text(), dialect="bigquery")

    inline = to_html(models, inline=True)
    cdn = to_html(models, inline=False)

    # Inline default = fully offline: the Mermaid bundle is embedded, no CDN URL.
    assert "globalThis" in inline  # marker from the vendored Mermaid bundle
    assert "cdn.jsdelivr.net" not in inline
    assert len(inline) > 1_000_000

    # CDN mode = tiny file that imports Mermaid from the network.
    assert "cdn.jsdelivr.net" in cdn
    assert "import mermaid from" in cdn
    assert len(cdn) < 100_000


# --- v0.4: column lineage + risk lint ---


def test_column_lineage_traces_to_source_columns():
    model = analyze(EXAMPLE.read_text(), dialect="bigquery")
    rows = {r.column: r.sources for r in column_lineage(model)}
    assert rows["last_login"] == ["events.login_at"]
    assert rows["total"] == ["orders.amount"]
    # single-column query
    single = column_lineage(model, column="total")
    assert single[0].sources == ["orders.amount"]


RISKY_SQL = """
SELECT *
FROM a
JOIN b
WHERE a.id = b.id
"""

HAVING_SQL = """
SELECT user_id, COUNT(*) c
FROM events
GROUP BY user_id
HAVING user_id > 100
"""


def test_lint_detects_cross_join_and_select_star():
    model = analyze(RISKY_SQL, dialect="bigquery")
    rules = {f.rule for f in lint(model)}
    assert "cartesian-join" in rules
    assert "select-star" in rules
    assert meets_threshold(lint(model), "high")


def test_lint_having_pushdown():
    model = analyze(HAVING_SQL, dialect="bigquery")
    rules = {f.rule for f in lint(model)}
    assert "having-without-aggregate" in rules


def test_lint_clean_query_has_no_findings():
    model = analyze(EXAMPLE.read_text(), dialect="bigquery")
    assert lint(model) == []


# --- UPDATE / MERGE data flow ---

MERGE_SQL = """
MERGE dim_users AS T
USING (SELECT id, val FROM stg_a UNION ALL SELECT id, val FROM stg_b) AS S
ON T.id = S.id
WHEN MATCHED THEN UPDATE SET val = S.val, updated_at = CURRENT_DATE()
WHEN NOT MATCHED THEN INSERT (id, val) VALUES (S.id, S.val)
"""

UPDATE_FROM_SQL = """
UPDATE tgt T SET T.flag = true FROM dim_x S WHERE T.id = S.id
"""


def test_merge_models_source_and_branches():
    model = analyze(MERGE_SQL, dialect="bigquery")
    assert model.statement_kind == "MERGE"
    assert model.target_table == "dim_users"
    # The USING subquery becomes its own source node reading the base tables.
    source = next(n for n in model.nodes if not n.is_final)
    assert set(source.source_tables) == {"stg_a", "stg_b"}
    # The merge node carries the match key and one op per WHEN branch.
    merge = model.final
    kinds = merge.op_kinds()
    assert "match" in kinds
    assert kinds.count("merge-when") == 2
    assert "updated_at" in merge.output_columns
    # Graph routes the merge into the target as a write sink.
    mermaid = to_mermaid(model)
    assert "==>|MERGE|" in mermaid
    assert "dim_users" in mermaid


def test_update_from_tracks_source_and_written_columns():
    model = analyze(UPDATE_FROM_SQL, dialect="bigquery")
    assert model.statement_kind == "UPDATE"
    assert model.target_table == "tgt"
    node = model.final
    assert node.source_tables == ["dim_x"]  # the FROM source, not the target
    assert node.output_columns == ["flag"]
    assert "set" in node.op_kinds() and "filter" in node.op_kinds()


def test_lint_flags_update_without_where():
    findings = lint(analyze("UPDATE tgt SET active = false", dialect="bigquery"))
    assert any(f.rule == "full-table-write" and f.severity == "high" for f in findings)
    # A scoped UPDATE is clean.
    assert lint(analyze(UPDATE_FROM_SQL, dialect="bigquery")) == []


# --- config: disable / severity override / cost partition rule ---

PART_CFG = Config(partitions={"events": "event_date"})


def test_partition_filter_missing_fires_without_filter():
    model = analyze("SELECT user_id FROM events", dialect="bigquery")
    findings = lint(model, PART_CFG)
    assert any(f.rule == "partition-filter-missing" and f.severity == "high" for f in findings)


def test_partition_filter_present_is_clean():
    model = analyze(
        "SELECT user_id FROM events WHERE event_date = '2026-01-01'", dialect="bigquery"
    )
    findings = lint(model, PART_CFG)
    assert not any(f.rule == "partition-filter-missing" for f in findings)


def test_partition_rule_silent_without_config():
    model = analyze("SELECT user_id FROM events", dialect="bigquery")
    assert not any(f.rule == "partition-filter-missing" for f in lint(model))


def test_config_disable_and_severity_override():
    sql = "SELECT * FROM users CROSS JOIN orders"
    base = lint(analyze(sql, dialect="bigquery"))
    assert any(f.rule == "select-star" for f in base)
    cfg = Config(disabled={"cartesian-join"}, severity={"select-star": "low"})
    tuned = lint(analyze(sql, dialect="bigquery"), cfg)
    assert not any(f.rule == "cartesian-join" for f in tuned)
    assert all(f.severity == "low" for f in tuned if f.rule == "select-star")


def test_load_config_from_toml(tmp_path):
    (tmp_path / ".sqlucent.toml").write_text(
        "[rules]\ndisable = ['select-star']\n[cost.partitions]\nevents = 'event_date'\n"
        "[cost.table_rows]\nevents = 1000\n",
        encoding="utf-8",
    )
    cfg = load_config(start=tmp_path)
    assert "select-star" in cfg.disabled
    assert cfg.partitions == {"events": "event_date"}
    assert cfg.table_rows == {"events": 1000}


# --- schema-driven cost estimate ---

COST_SCHEMA = {
    "orders": {"order_id": "INT64", "user_id": "INT64", "amount": "FLOAT64", "ts": "TIMESTAMP"},
}


def test_column_width_types():
    assert column_width("INT64")[0] == 8
    assert column_width("BOOL")[0] == 1
    assert column_width("NUMERIC")[0] == 16
    assert column_width("STRING", string_bytes=20)[0] == 20
    assert column_width("ARRAY<INT64>")[1] is False  # nested → unknown


def test_cost_column_pruning_ratio():
    model = analyze("SELECT order_id, amount FROM orders", dialect="bigquery")
    est = estimate_cost(model, COST_SCHEMA)
    (t,) = est.tables
    assert t.bytes_per_row == 16  # two INT64/FLOAT64 cols
    assert t.full_bytes_per_row == 32
    assert sorted(t.scanned_columns) == ["amount", "order_id"]


def test_cost_filter_columns_are_scanned():
    # ts only appears in WHERE but is still read → counts toward bytes scanned.
    model = analyze(
        "SELECT order_id FROM orders WHERE ts >= '2026-01-01'", dialect="bigquery"
    )
    est = estimate_cost(model, COST_SCHEMA)
    (t,) = est.tables
    assert "ts" in t.scanned_columns


def test_cost_absolute_bytes_and_partition_pruning():
    cfg = Config(
        partitions={"orders": "ts"},
        table_rows={"orders": 1_000_000},
        partition_selectivity=0.1,
    )
    no_filter = estimate_cost(
        analyze("SELECT amount FROM orders", dialect="bigquery"), COST_SCHEMA, cfg
    )
    pruned = estimate_cost(
        analyze("SELECT amount FROM orders WHERE ts >= '2026-01-01'", dialect="bigquery"),
        COST_SCHEMA,
        cfg,
    )
    assert no_filter.total_bytes is not None
    # The pruned scan reads ~10% the rows despite the extra ts column → far fewer bytes.
    assert pruned.total_bytes < no_filter.total_bytes
    assert pruned.tables[0].partition_pruned is True


def test_cost_count_star_is_metadata_only():
    est = estimate_cost(analyze("SELECT COUNT(*) FROM orders", dialect="bigquery"), COST_SCHEMA)
    assert est.tables == []


# --- schema binding: precise SELECT * lineage ---

DDL = """
CREATE TABLE users (user_id INT64, name STRING, email STRING);
CREATE TABLE orders (order_id INT64, user_id INT64, amount FLOAT64);
"""

STAR_SQL = "SELECT * FROM users u JOIN orders o ON u.user_id = o.user_id"


def test_build_schema_from_ddl():
    schema = build_schema_from_ddl(DDL, dialect="bigquery")
    assert set(schema["users"]) == {"user_id", "name", "email"}
    assert set(schema["orders"]) == {"order_id", "user_id", "amount"}


def test_select_star_untraceable_without_schema():
    model = analyze(STAR_SQL, dialect="bigquery")
    assert column_lineage(model) == []  # '*' can't be traced by name


def test_select_star_traceable_with_schema():
    model = analyze(STAR_SQL, dialect="bigquery")
    schema = build_schema_from_ddl(DDL, dialect="bigquery")
    rows = {r.column: r.sources for r in column_lineage(model, schema=schema)}
    # Stars expanded into real columns, resolved to base tables (alias -> table).
    assert rows["name"] == ["users.name"]
    assert rows["amount"] == ["orders.amount"]
    assert rows["email"] == ["users.email"]


# --- project-level (cross-file) table lineage ---


def _write_pipeline(tmp_path):
    (tmp_path / "a.sql").write_text(
        "CREATE TABLE stg_orders AS SELECT * FROM raw_orders WHERE status='paid';"
    )
    (tmp_path / "b.sql").write_text(
        "INSERT INTO fact_rev SELECT o.id, u.name FROM stg_orders o "
        "JOIN dim_users u ON o.uid = u.id;"
    )
    return tmp_path


def test_analyze_project_builds_cross_file_dag(tmp_path):
    graph = analyze_project(_write_pipeline(tmp_path), dialect="bigquery")
    edges = {(e.src, e.dst) for e in graph.edges}
    assert ("raw_orders", "stg_orders") in edges
    assert ("stg_orders", "fact_rev") in edges  # edge spans two files
    assert ("dim_users", "fact_rev") in edges
    assert "raw_orders" in graph.roots()
    assert "fact_rev" in graph.sinks()
    assert "stg_orders" in graph.intermediate()


def test_project_mermaid_renders_nodes_and_edges(tmp_path):
    graph = analyze_project(_write_pipeline(tmp_path), dialect="bigquery")
    mermaid = project_mermaid(graph)
    assert mermaid.startswith("flowchart LR")
    assert 't_raw_orders[("raw_orders")]' in mermaid  # root = cylinder
    assert 't_fact_rev(["fact_rev"])' in mermaid  # sink = rounded
    assert "t_stg_orders --> t_fact_rev" in mermaid


def test_impact_downstream_is_transitive(tmp_path):
    graph = analyze_project(_write_pipeline(tmp_path), dialect="bigquery")
    # raw_orders -> stg_orders -> fact_rev : transitive must include both.
    down = downstream_tables(graph, "raw_orders")
    assert "stg_orders" in down
    assert "fact_rev" in down
    # A leaf has no downstream.
    assert downstream_tables(graph, "fact_rev") == []


def test_column_impact_finds_referencing_writes(tmp_path):
    (tmp_path / "a.sql").write_text(
        "CREATE TABLE stg AS SELECT id, amount, status FROM raw;"
    )
    (tmp_path / "b.sql").write_text(
        "INSERT INTO rollup SELECT id, SUM(amount) AS total FROM stg GROUP BY id;"
    )
    graph = analyze_project(tmp_path, dialect="bigquery")
    hits = column_impact(graph, "stg", "amount")
    assert any(h.target == "rollup" and h.how == "explicit" for h in hits)
    # A column nobody references downstream has no hits.
    assert column_impact(graph, "stg", "status") == []


def test_html_localization():
    models = analyze_script(EXAMPLE.read_text(), dialect="bigquery")
    page = to_html(models, lang="Chinese")
    # UI chrome is localized...
    assert "读取自" in page
    assert "操作" in page
    assert "点击任意节点查看其 SQL" in page
    # ...but SQL identifiers are never translated
    assert "ranked" in page and "events" in page
