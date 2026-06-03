# Changelog

All notable changes to SQLucent are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-06-03

The first release with the full deterministic core. Everything below was added
on top of the 0.1.0 MVP.

### Added
- **Interactive HTML (`--html`)** — single self-contained page with the Mermaid
  data-flow diagram; click a node to highlight it and inspect its SQL, sources,
  operations, and outputs. Mermaid is inlined by default for true offline use
  (~3 MB); `--cdn` swaps to a CDN import for a tiny (~8 KB) file. `--lang`
  localizes the UI chrome (English/Chinese built in).
- **Column-level lineage (`--lineage [COLUMN]`)** — trace each final output
  column back to its source column(s) through every CTE/subquery via
  `sqlglot.lineage`. With `--schema`, `SELECT *` is expanded and traced to base
  tables.
- **Schema binding (`--schema PATH`)** — accepts DDL (`CREATE TABLE ...`) or a
  `{table: {column: type}}` JSON file; powers precise `SELECT *` lineage and the
  cost estimate.
- **Risk lint (`--lint`)** — deterministic AST checks: `full-table-write` (high),
  `cartesian-join` (high), `select-star` (medium), `having-without-aggregate`
  (medium), `distinct-with-group-by` (low), and config-gated
  `partition-filter-missing` (high). `--fail-on {low,medium,high}` turns lint
  into a CI gate; `--write-baseline`/`--baseline` grandfather existing findings.
- **Config (`.sqlucent.toml`)** — auto-discovered (or `--config PATH`): disable
  rules, override severities, and declare partitioned tables / row counts.
- **BigQuery scan-cost estimate (`--cost --schema`)** — bytes scanned =
  referenced columns × rows, with column pruning and partition selectivity; emits
  absolute bytes and dollars when row counts are configured.
- **Local-LLM narration (`--narrate`, `--lang`)** — Ollama rephrases a
  deterministic fact sheet built from the IR (never the raw SQL), with graceful
  fallback to the template walkthrough. `--model`/`SXR_OLLAMA_MODEL`,
  `SXR_LANG`.
- **Project-level table lineage** — point `sqlucent` at a directory to build a
  cross-file table DAG across INSERT/CREATE/UPDATE/MERGE/DELETE, classifying
  tables as source/intermediate/terminal.
- **Impact analysis (`--impact TABLE[.COLUMN]`)** — direct + transitive
  downstream of a table (with first-hop files); best-effort column impact.
- **UPDATE / MERGE data flow** — write target modeled as a sink, sources from
  `FROM`/`USING`, written columns from `SET`/`WHEN` branches.
- **Real-world robustness** — Jinja preprocessing, multi-statement scripts,
  lenient parsing that degrades unsupported statements instead of failing.

### Changed
- Renamed the project to **SQLucent** (PyPI/import name `sqlucent`; CLI `sqlucent`
  with short alias `sxr`; GitHub repo remains `sql-x-ray`).
- Walkthrough now collapses wide joins and long column lists, with `-v/--verbose`
  to expand full `ON` clauses and column lists.

## [0.1.0] - 2026-05-31

### Added
- Initial release: parse SQL via sqlglot → semantic IR → data-flow Mermaid graph,
  per-CTE responsibilities, and a template-based plain-language walkthrough. No
  LLM and no schema required.
- GitHub Actions CI and PyPI publishing via Trusted Publishing (OIDC).

[0.2.0]: https://github.com/thehwang/sql-x-ray/releases/tag/v0.2.0
[0.1.0]: https://github.com/thehwang/sql-x-ray/releases/tag/v0.1.0
