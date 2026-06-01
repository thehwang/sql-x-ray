"""Project-level (cross-file) table lineage.

Point this at a directory of ``.sql`` files (e.g. an Airflow/dbt SQL folder) and
it builds a table-level DAG: for every write statement it records which tables
feed which, across files. The result answers "where does this table come from,
and what depends on it" for a whole repository — no warehouse connection needed.

Edges are derived per *statement* (a statement's source tables -> its write
target), then merged across the project. Table names are matched by their
unqualified name, so templated/qualified references line up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .analyzer import analyze_script
from .ir import SqlModel

# Statement kinds that write to a target table.
_WRITE_KINDS = {"INSERT", "CREATE", "UPDATE", "MERGE", "DELETE"}


@dataclass
class Edge:
    src: str
    dst: str
    files: list[str] = field(default_factory=list)


@dataclass
class StatementRef:
    """A single write statement, retained so impact analysis can inspect it
    (e.g. which columns it references) without re-parsing the project."""

    file: str
    kind: str
    target: str | None
    reads: list[str] = field(default_factory=list)
    sql: str = ""


@dataclass
class FileSummary:
    path: str
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ProjectGraph:
    edges: list[Edge] = field(default_factory=list)
    files: list[FileSummary] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    statements: list[StatementRef] = field(default_factory=list)

    @property
    def written(self) -> set[str]:
        return {e.dst for e in self.edges} | {
            w for f in self.files for w in f.writes
        }

    @property
    def read(self) -> set[str]:
        return {e.src for e in self.edges} | {
            r for f in self.files for r in f.reads
        }

    def roots(self) -> list[str]:
        """Tables read but never written anywhere — the project's source inputs."""
        return sorted(self.read - self.written)

    def sinks(self) -> list[str]:
        """Tables written but never read — the project's terminal outputs."""
        return sorted(self.written - self.read)

    def intermediate(self) -> list[str]:
        return sorted(self.read & self.written)

    def to_dict(self) -> dict:
        return {
            "tables": self.tables,
            "edges": [{"src": e.src, "dst": e.dst, "files": e.files} for e in self.edges],
            "roots": self.roots(),
            "sinks": self.sinks(),
            "intermediate": self.intermediate(),
            "files": [
                {"path": f.path, "reads": f.reads, "writes": f.writes, "error": f.error}
                for f in self.files
            ],
        }


def _model_reads_writes(model: SqlModel) -> tuple[set[str], str | None]:
    target = model.target_table if model.statement_kind in _WRITE_KINDS else None
    reads: set[str] = set()
    for node in model.nodes:
        for t in node.source_tables:
            if t and t != target:
                reads.add(t)
    return reads, target


def analyze_project(
    root: str | Path, dialect: str = "bigquery", pattern: str = "**/*.sql"
) -> ProjectGraph:
    """Build a table-level lineage DAG from every SQL file under `root`."""
    root = Path(root)
    files = sorted(root.glob(pattern)) if root.is_dir() else [root]

    edge_index: dict[tuple[str, str], Edge] = {}
    summaries: list[FileSummary] = []
    table_set: set[str] = set()
    statements: list[StatementRef] = []

    for path in files:
        rel = str(path.relative_to(root)) if root.is_dir() else path.name
        summary = FileSummary(path=rel)
        try:
            models = analyze_script(path.read_text(encoding="utf-8"), dialect=dialect)
        except Exception as exc:  # never let one bad file sink the whole scan
            summary.error = f"{type(exc).__name__}: {exc}"
            summaries.append(summary)
            continue

        for model in models:
            reads, target = _model_reads_writes(model)
            if target or reads:
                statements.append(
                    StatementRef(
                        file=rel,
                        kind=model.statement_kind,
                        target=target,
                        reads=sorted(reads),
                        sql=model.statement_sql,
                    )
                )
            for r in reads:
                table_set.add(r)
                if r not in summary.reads:
                    summary.reads.append(r)
            if target:
                table_set.add(target)
                if target not in summary.writes:
                    summary.writes.append(target)
                for r in reads:
                    if r == target:
                        continue  # skip self-loops (read-modify-write same table)
                    key = (r, target)
                    edge = edge_index.get(key)
                    if edge is None:
                        edge = Edge(src=r, dst=target)
                        edge_index[key] = edge
                    if rel not in edge.files:
                        edge.files.append(rel)
        summaries.append(summary)

    edges = sorted(edge_index.values(), key=lambda e: (e.src, e.dst))
    return ProjectGraph(
        edges=edges, files=summaries, tables=sorted(table_set), statements=statements
    )


def _safe_id(name: str) -> str:
    return re.sub(r"\W", "_", name) or "n"


def project_mermaid(graph: ProjectGraph) -> str:
    """Render the table-level DAG. Roots (source inputs) are cylinders, terminal
    outputs are rounded, intermediate tables are rectangles."""
    roots = set(graph.roots())
    sinks = set(graph.sinks())

    lines = ["flowchart LR"]
    for tbl in graph.tables:
        tid = f"t_{_safe_id(tbl)}"
        if tbl in roots:
            lines.append(f'  {tid}[("{tbl}")]')
        elif tbl in sinks:
            lines.append(f'  {tid}(["{tbl}"])')
        else:
            lines.append(f'  {tid}["{tbl}"]')
    for e in graph.edges:
        lines.append(f"  t_{_safe_id(e.src)} --> t_{_safe_id(e.dst)}")
    return "\n".join(lines)


def project_summary(graph: ProjectGraph) -> str:
    n_files = len(graph.files)
    n_err = sum(1 for f in graph.files if f.error)
    roots, sinks, mids = graph.roots(), graph.sinks(), graph.intermediate()
    lines = [
        f"Scanned {n_files} file(s), {len(graph.tables)} table(s), "
        f"{len(graph.edges)} edge(s)" + (f", {n_err} parse error(s)" if n_err else "") + ".",
        f"  source inputs ({len(roots)}): " + (", ".join(roots) if roots else "—"),
        f"  intermediate ({len(mids)}): " + (", ".join(mids) if mids else "—"),
        f"  terminal outputs ({len(sinks)}): " + (", ".join(sinks) if sinks else "—"),
    ]
    if n_err:
        lines.append("  files with parse errors:")
        for f in graph.files:
            if f.error:
                lines.append(f"    - {f.path}: {f.error}")
    return "\n".join(lines)
