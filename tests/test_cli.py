"""End-to-end tests for the `sqlucent` CLI entry point (`sqlucent.cli.main`).

These exercise the actual argument parsing, dispatch, stdout/stderr, and exit
codes a user (or CI) sees — complementing the library-level tests in
test_analyzer.py.
"""
import io
import json
from pathlib import Path

import pytest

from sqlucent.cli import main

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_SQL = str(EXAMPLES / "top_users.sql")
SCHEMA_SQL = str(EXAMPLES / "schema.sql")


def run(argv, monkeypatch=None, stdin=None):
    """Invoke the CLI; optionally feed stdin. Returns (exit_code)."""
    if stdin is not None:
        assert monkeypatch is not None
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main(argv)


# --- default + single-section outputs ---


def test_default_run_prints_walkthrough_and_graph(capsys):
    code = run([EXAMPLE_SQL])
    out = capsys.readouterr().out
    assert code == 0
    assert "SQLucent" in out
    assert "Walkthrough" in out
    assert "```mermaid" in out
    assert "flowchart TD" in out


def test_mermaid_only(capsys):
    code = run([EXAMPLE_SQL, "--mermaid"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.lstrip().startswith("flowchart TD")
    assert "```mermaid" not in out  # raw graph, no markdown fence


def test_walkthrough_only(capsys):
    code = run([EXAMPLE_SQL, "--walkthrough"])
    out = capsys.readouterr().out
    assert code == 0
    assert "`active`" in out
    assert "flowchart TD" not in out


def test_json_is_valid_and_lists_statements(capsys):
    code = run([EXAMPLE_SQL, "--json"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert isinstance(payload, list) and len(payload) == 1
    assert payload[0]["statement_kind"] == "SELECT"


def test_html_is_self_contained(capsys):
    code = run([EXAMPLE_SQL, "--html", "--cdn"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.lstrip().startswith("<!doctype html>")
    assert "cdn.jsdelivr.net" in out  # --cdn honored


# --- stdin ---


def test_reads_from_stdin(capsys, monkeypatch):
    code = run(["-", "--mermaid"], monkeypatch=monkeypatch, stdin="SELECT a FROM t")
    out = capsys.readouterr().out
    assert code == 0
    assert "flowchart TD" in out
    assert 't_t[("t")]' in out


# --- lint + exit codes ---

RISKY = "SELECT * FROM a JOIN b WHERE a.id = b.id"


def test_lint_reports_findings(capsys, monkeypatch):
    code = run(["-", "--lint"], monkeypatch=monkeypatch, stdin=RISKY)
    out = capsys.readouterr().out
    assert code == 0  # no --fail-on, so reporting only
    assert "select-star" in out
    assert "cartesian-join" in out
    assert "finding(s)" in out


def test_lint_fail_on_high_exits_nonzero(capsys, monkeypatch):
    code = run(["-", "--lint", "--fail-on", "high"], monkeypatch=monkeypatch, stdin=RISKY)
    assert code == 1


def test_lint_clean_query_exits_zero(capsys, monkeypatch):
    # A genuinely clean query via stdin (config discovery starts at cwd = repo
    # root, which has no .sqlucent.toml, so no partition rule fires).
    code = run(
        ["-", "--lint", "--fail-on", "high"],
        monkeypatch=monkeypatch,
        stdin="SELECT a, b FROM t WHERE a > 1",
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "no risks found" in out


def test_lint_picks_up_example_config(capsys):
    # examples/.sqlucent.toml declares events/orders as partitioned; the example
    # scans them without a partition filter → partition-filter-missing (high).
    code = run([EXAMPLE_SQL, "--lint", "--fail-on", "high"])
    out = capsys.readouterr().out
    assert code == 1
    assert "partition-filter-missing" in out


# --- lineage ---


def test_lineage_traces_columns(capsys):
    code = run([EXAMPLE_SQL, "--lineage"])
    out = capsys.readouterr().out
    assert code == 0
    assert "total" in out and "orders.amount" in out
    assert "←" in out


# --- cost requires schema ---


def test_cost_without_schema_errors(capsys):
    code = run([EXAMPLE_SQL, "--cost"])
    err = capsys.readouterr().err
    assert code == 2
    assert "--cost needs --schema" in err


def test_cost_with_schema(capsys, monkeypatch):
    sql = "SELECT user_id, amount FROM orders"
    code = run(["-", "--cost", "--schema", SCHEMA_SQL], monkeypatch=monkeypatch, stdin=sql)
    out = capsys.readouterr().out
    assert code == 0
    assert "orders" in out


# --- project (directory) mode ---


def test_directory_input_runs_project_mode(capsys):
    code = run([str(EXAMPLES / "pipeline")])
    out = capsys.readouterr().out
    assert code == 0
    assert "flowchart LR" in out  # project DAG orientation


def test_impact_on_directory(capsys):
    # stg_orders is produced by 01 and consumed downstream → has a blast radius.
    code = run([str(EXAMPLES / "pipeline"), "--impact", "stg_orders"])
    out = capsys.readouterr().out
    assert code == 0
    assert "stg_orders" in out


# --- error paths ---


def test_missing_file_exits_2(capsys):
    code = run(["/no/such/file_doesnotexist.sql"])
    err = capsys.readouterr().err
    assert code == 2
    assert "cannot read" in err


def test_empty_input_reports_no_statements(capsys, monkeypatch):
    code = run(["-"], monkeypatch=monkeypatch, stdin="   \n  ")
    err = capsys.readouterr().err
    assert code == 1
    assert "no statements" in err


def test_version_flag(capsys):
    from sqlucent import __version__

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out
