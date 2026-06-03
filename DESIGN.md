# SQLucent — Design Doc (v0 draft, for review)

> See through any SQL. Paste a gnarly nested-CTE query and get back an X-ray:
> a data-flow diagram, per-CTE responsibilities, column-level lineage, a
> plain-language walkthrough, and risk hints — **without running the query**.

**Status:** 0.2.0 shipped (full deterministic core; see §7). Decisions locked (see §0).
**Display name:** SQLucent · **PyPI/import name:** `sqlucent` · **GitHub repo:** `sql-x-ray` (CLI binary: `sqlucent`, short alias `sxr`).

---

## 0. Decisions (locked)

| # | Decision |
|---|---|
| Tech stack | **Python + sqlglot** (Path A) — fastest path to a trustworthy semantic core; lineage/schema binding come free from sqlglot. |
| Primary dialect | **BigQuery** (then Postgres / Snowflake). |
| v0.1 scope | Parse → IR → **data-flow Mermaid graph + CTE responsibility + template walkthrough**. No LLM, no schema required. |
| Diagram | **Mermaid first** (free render on GitHub/Markdown/Notion). |
| LLM (v0.2) | **Ollama / local-first**, with optional API backend later. |

---

## 1. Problem

Reading someone else's 300-line nested-CTE SQL is a 20-minute archaeology dig.
Existing options don't answer *"what does this query actually do, and how does
data flow through it?"*:

| Tool | What it gives | Gap |
|---|---|---|
| `EXPLAIN` / query plan | *physical* execution (how the DB runs it) | not *semantic*, no plain language, no data-flow picture |
| dbt docs / lineage tools | lineage **between tables/models** | doesn't explain logic **inside** one query; needs whole-project setup |
| Ask ChatGPT directly | rough gist | **misses tables, invents joins, untrustworthy on long SQL** — it never truly parses |

**SQLucent's wedge** = *parser-guaranteed structure* + *data-flow diagram* +
*plain-language narration* + *risk lint*, all from a single query, no project wiring.

---

## 2. Core principle: deterministic core, probabilistic shell

The whole credibility of the tool rests on this separation:

- **Deterministic core** (parse → IR → graph → lineage → lint): 100% derived from
  a real SQL parser/optimizer. Testable, cacheable, never hallucinates.
- **Probabilistic shell** (natural-language narration): an LLM sits at the *outer
  edge* and only *rephrases the already-structured facts* into prose. It never
  has to "understand" the SQL, which is what makes it trustworthy.

If the LLM is unavailable, the tool still produces the diagram, lineage, and a
template-based ("from X, grouped by Y, computing Z") narration.

---

## 3. Architecture: parse once → IR hub → fan-out

The pipeline is **not** a 7-step straight line. After parsing we converge on a
**semantic model (IR)**; every downstream feature is a *consumer* of that IR
(mostly parallel), and natural-language narration consumes the IR **plus** the
other derived artifacts. Rendering is the last, thin layer.

```
① Input SQL + dialect detection (or --dialect)
② Parse → AST
③ [optional] Schema binding (DDL file / live DB / dbt manifest)
④ Semantic Model (IR)  ◀── the hub; everything below reads this
   ├─ CTE responsibility ID      (deterministic + templates)
   ├─ Data-flow graph            (deterministic → Mermaid/Graphviz)
   ├─ Column-level lineage       (precise w/ schema; degraded without)
   └─ Risk lint                  (deterministic rules on IR)
⑤ Natural-language explanation  (LLM; consumes ④ + all artifacts above)
⑥ Render: CLI text / single-file HTML / JSON
```

### Why the two additions vs. a naive linear pipeline
- **IR as a hub** keeps the deterministic parts isolated from the LLM. Each
  consumer (graph, lineage, lint) is independently testable.
- **Explicit schema binding** is *required* for column lineage and `SELECT *`
  expansion — you cannot resolve `SELECT *` or disambiguate same-named columns
  from SQL text alone. With schema → precise; without → graceful degradation
  (table-level lineage still works, column-level marked "inferred, may be partial").

---

## 4. Interface strategy: engine-first, CLI primary, single-file HTML for visuals

Two kinds of output → two natural homes. We do **not** pick one; we layer:

```
┌─ Core Engine (library) ─ parse / IR / graph / lineage / lint ─┐
        ↓                    ↓                         ↓
      CLI                single-file HTML        (later) VS Code ext
  text / JSON / Mermaid   interactive graph,       right-click
  pipes, CI, exit codes   click node→highlight SQL  "explain this"
```

- **CLI is the primary entry** (matches the `pq` single-binary, terminal-first DNA).
  - `sqlucent q.sql` → walkthrough + risks + **Mermaid** (renders for free on
    GitHub/Notion/Markdown → zero-cost visualization).
  - `sqlucent q.sql --json` → for CI / other tooling; exit non-zero on risk gate.
- **`--html` emits a self-contained HTML file** (NOT a web server): interactive
  data-flow graph with bidirectional highlight (click a node → highlight its SQL).
  Shareable, no hosting, no backend. This is the "UI-grade" payoff without the
  burden of building a web app.
- **VS Code extension** deferred to v0.4 — SQL lives in the editor; it's just
  another thin shell over the engine.

---

## 5. Feature catalog (what the IR powers)

1. **CTE responsibility ID** — for each CTE/subquery: source tables, the
   operation (filter / aggregate / join / window / rank), one-line purpose.
2. **Data-flow graph** — DAG of tables → CTEs → final SELECT; each edge labeled
   with the operation performed.
3. **Column-level lineage** — trace a final column back to its source column(s)
   and the transforms applied (e.g. `total ← SUM(orders.amount)`). Precise only
   with schema binding.
4. **Natural-language walkthrough** — numbered, plain-language steps; ends with a
   one-sentence "what this query is for".
5. **Risk lint (deterministic)** — examples:
   - cartesian / accidental cross join
   - `SELECT *` widening
   - filter not on a partition/index column (where schema known)
   - `HAVING` condition that could be pushed down to `WHERE`
   - implicit type coercion in join keys
   Each rule is an IR pattern match — explainable, no LLM.

---

## 6. Tech stack — **the key open decision (need your call)**

The hardest part of the deterministic core is parse + IR + schema binding +
column lineage across dialects. Two paths:

| | Path A: **Python + sqlglot** (recommended for MVP) | Path B: **Rust** (consistent with `pq`) |
|---|---|---|
| Parser/IR | `sqlglot` — multi-dialect parser, AST, optimizer | `sqlparser-rs` — parser only |
| Column lineage | `sqlglot.lineage` built-in ✅ | must build yourself / via DataFusion |
| Schema binding | `sqlglot.optimizer` built-in ✅ | DIY |
| Dialects (BQ/Snowflake/PG/Spark) | excellent ✅ | partial |
| Single static binary (your `pq` DNA) | needs PyApp/PyInstaller ⚠️ | native ✅ |
| Time-to-great-MVP | **fast** (the hard semantic work is done) | slower (rebuild lineage) |

**My recommendation:** start **Path A (Python + sqlglot)** to validate the hard
semantic core fast — `sqlglot` already solves steps ③④ and column lineage, which
is exactly where the value and difficulty are. Distribute via `pipx` first, then
PyApp/PyInstaller for a binary. Revisit Rust only if perf/distribution demands it.

(Open to Path B if keeping the single-binary `pq` identity matters more to you
than MVP speed — but we'd be reimplementing lineage that sqlglot gives for free.)

---

## 7. Roadmap

> Headings below are the **released SemVer versions** (as published to PyPI and
> recorded in [`CHANGELOG.md`](CHANGELOG.md)). Feature work that was tracked under
> internal milestone labels during development (e.g. "narration", "HTML",
> "lineage+lint") all landed together in the **0.2.0** release; they are grouped
> here by feature area, not by those interim labels, so the history matches the
> versions a user can actually install.

### 0.1.0 — MVP (shipped)

- **Parse → IR → render.** Path A. `sqlucent q.sql` → parse via sqlglot → IR →
  data-flow graph (Mermaid) + template-based step walkthrough + CTE responsibility
  ID. **Zero LLM, zero schema required.** This alone beats `git blame`-of-SQL.

### 0.2.0 — full deterministic core (shipped)

Everything below shipped in 0.2.0, grouped by feature area:

- **Real-world robustness.** Jinja preprocessing (`{{ }}`/`{% %}`/`{# #}` → safe
  placeholders) for Airflow/dbt SQL; multi-statement scripts; INSERT /
  CREATE-TABLE-AS / DELETE handled, with write-target shown as a graph sink.
  Lenient parsing (`ErrorLevel.IGNORE`) degrades unsupported statements (COPY/UNLOAD)
  to `UNSUPPORTED` instead of failing the file; empty `;` statements skipped.
  Walkthrough collapses wide joins/group-bys and long column lists, with
  `-v/--verbose` to expand full ON clauses + lists (`Operation` stores full
  `detail` + a short `brief`; truncation is render-time).
  **Validated on a real corpus: 121/121 datamart SQL files parse without crashing**
  (INSERT 103, DELETE 66, CREATE 34, DROP 30, UPDATE 27, SELECT 27, ...).
- **Local LLM narration.** `--narrate/--llm` calls Ollama via the stdlib HTTP API
  (no new dependency). The LLM only rephrases a deterministic *fact sheet* built
  from the IR (`build_factsheet`) — it never sees raw SQL and is told never to
  invent tables/columns, so hallucination stays low. Graceful fallback to the
  template if Ollama is unreachable or the model isn't pulled. `--model` selects
  the model (default `llama3.2`, override via `SXR_OLLAMA_MODEL`; verified live on
  `qwen2.5:3b`). `--lang/--language` (or `SXR_LANG`) makes the model narrate in any
  language (verified live in Chinese) while keeping SQL identifiers untranslated;
  it implies `--narrate`. stdin/pipe + `--json` supported.
- **Interactive HTML.** `--html` emits a single self-contained page (no server):
  Mermaid data-flow diagram with `click node → side panel` showing that node's SQL,
  sources, operations, and outputs. Per-node SQL captured into the IR
  (`QueryNode.sql_text`). Verified in-browser on the 15-table datamart load.
  `--lang` localizes the page's UI chrome (English/Chinese built in, extensible;
  unknown languages fall back to English); SQL identifiers are never translated.
  Mermaid is **inlined by default** (vendored UMD build at `vendor/mermaid.min.js`)
  so the page is genuinely offline/no-server/no-CDN (~3 MB); `--cdn` swaps to a
  CDN import for a ~8 KB file. Offline rendering + interactivity verified in-browser
  with zero external requests.
- **Column lineage + risk lint.** `--lineage [COLUMN]` traces each final output
  column back to its source column(s) through every CTE/subquery via
  `sqlglot.lineage` (no schema needed for explicit references; `SELECT *` and bare
  expressions reported as unresolvable-by-name). `--lint` runs deterministic
  AST-pattern checks — `cartesian-join` (high; condition-less join between tables,
  covering comma joins and `CROSS JOIN`, but not `UNNEST`/lateral), `select-star`
  (medium), `having-without-aggregate` (medium; belongs in WHERE), and
  `distinct-with-group-by` (low). `--fail-on {low,medium,high}` turns lint into a
  CI gate (exit 1 at/above the threshold). Both unwrap `INSERT/CREATE ... AS SELECT`
  to analyze the underlying query. Full statement SQL is captured into the IR
  (`SqlModel.statement_sql`) so lint/lineage re-parse cleanly. Verified clean on the
  example + 121-file corpus and firing on crafted risky SQL.
- **UPDATE/MERGE data flow.** `UPDATE` models its write target as a sink, the
  `FROM`/correlated tables as sources, and the `SET` assignments as written columns
  (`output_columns`). `MERGE` turns its `USING` subquery into its own source node
  (reading the base tables), then the merge node carries the `ON` match key plus one
  op per `WHEN` branch (`MATCHED → UPDATE`, `NOT MATCHED → INSERT`, etc.) and the
  written columns. New lint rule `full-table-write` (high) flags an `UPDATE`/`DELETE`
  with no `WHERE`. Verified on a real templated MERGE + multiple `UPDATE ... FROM`.
- **Schema binding.** `--schema PATH` accepts DDL (`CREATE TABLE`) or a
  `{table: {column: type}}` JSON file. With a schema, `column_lineage` runs
  sqlglot's `qualify` to expand `SELECT *` into concrete output columns and traces
  each to its base table (table aliases are resolved back to table names). Without a
  schema, `*` is still reported as untraceable-by-name. README carries a live
  Mermaid demo plus an interactive-HTML screenshot (`docs/demo-html.png`).
- **Project-level table lineage.** Point `sqlucent` at a *directory* and it scans
  every `.sql` file and builds a cross-file table-level DAG. Edges are derived per
  statement (source tables → write target) across INSERT/CREATE/UPDATE/MERGE/DELETE,
  then merged project-wide. Tables are classified as source inputs (read-only),
  intermediate, or terminal outputs (write-only). Outputs: summary + Mermaid
  (`flowchart LR`), or `--json` for tooling. One bad file degrades to a recorded
  parse error instead of failing the scan. Verified on a 100+ file corpus (200+
  tables, 400+ edges, 0 errors). sqlglot's fallback warnings are silenced.
- **Impact analysis.** `--impact TABLE` walks the project DAG for the exact blast
  radius: direct consumers + full transitive downstream, plus the first-hop files.
  `--impact TABLE.COLUMN` is best-effort (no schema): write statements that read the
  table and reference the column explicitly or via `SELECT *`, with the tables they
  write. Backed by per-statement records retained on `ProjectGraph`. Verified on a
  real repo (`dim_campaign` → 30 direct / 47 transitive downstream).
- **BigQuery cost lint + config.** `.sqlucent.toml` (auto-discovered from the file's
  directory upward, or `--config PATH`) tunes the linter without code changes:
  `[rules] disable`, `[rules.severity]` overrides, and `[cost.partitions]` declaring
  partitioned tables → partition columns. New rule `partition-filter-missing` (high)
  fires when a declared partitioned table is scanned with no `WHERE` filter on its
  partition column (full-table scan = $$$ in BigQuery); it stays silent unless the
  table is declared, so no false positives. A **baseline** workflow
  (`--write-baseline` to snapshot, `--baseline` to suppress) grandfathers existing
  findings so teams can gate only *new* issues in CI. `tomli` is a dependency only
  on Python < 3.11 (stdlib `tomllib` otherwise).
- **Schema-driven scan-cost estimate.** `--cost --schema` makes the cost concrete
  instead of boolean. BigQuery bills bytes scanned = referenced columns × rows;
  columnar storage means you pay only for the columns you touch (incl. `WHERE`/`JOIN`
  columns; `COUNT(*)` is metadata-only) and the partitions you read. We qualify the
  query against the schema (expanding `SELECT *`), sum per-column byte widths from
  types, and report bytes/row and the share of each row scanned. With
  `[cost.table_rows]` (+ `partition_selectivity`, `price_per_tb`, `string_bytes`) it
  yields absolute bytes and dollars — e.g. a full scan at 1.2 TB / $8.00 collapses
  to 35.8 GB / $0.24 once a partition filter prunes it. Variable-width types use a
  configurable assumption, so totals are estimates.

### Planned (0.3.0+)

- **VS Code extension** — right-click "explain this" over a selected query.
- **dbt / warehouse schema auto-discovery** — pull column types from a dbt
  `manifest.json` or a live warehouse so lineage, `SELECT *` expansion, and cost
  become precise without a hand-written `--schema`.
- **Semantic SQL diff** — explain how two versions of a query differ in meaning,
  not just in text.

Known remaining rough edges (not yet addressed): Jinja inside string literals
yields odd placeholder literals (cosmetic).

Distribution mirrors `pq`: brew tap, releases, good `--help`, a tutorial doc.

---

## 8. Open questions for you (please confirm before I build)

1. **Tech stack: Path A (Python+sqlglot) or Path B (Rust)?** — I lean A for MVP speed.
2. **First dialect to target?** — guessing BigQuery (your day-to-day), then Postgres/Snowflake.
3. **MVP scope check:** is "parse → data-flow Mermaid + step walkthrough, no LLM,
   no schema" the right v0.1 line, with LLM + lineage deferred?
4. **Diagram format priority:** Mermaid first (free GitHub/Markdown render) — agree?
5. **LLM stance:** local-first (MLX/Ollama) to match your privacy bent, with an
   optional API backend? Or API-first for quality?
```
