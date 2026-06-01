"""SQLucent — see through any SQL.

Public API:
    analyze(sql, dialect="bigquery") -> SqlModel
    to_mermaid(model) -> str
    walkthrough(model) -> str
"""
from .ir import SqlModel, QueryNode, Operation
from .analyzer import analyze, analyze_script
from .graph import to_mermaid
from .htmlout import to_html
from .walkthrough import walkthrough
from .narrate import narrate, build_factsheet, NarrationUnavailable
from .lint import lint, Finding, max_severity, meets_threshold
from .lineage import column_lineage, ColumnLineage
from .schema import build_schema_from_ddl, load_schema
from .project import analyze_project, project_mermaid, project_summary, ProjectGraph

__version__ = "0.1.0"

__all__ = [
    "SqlModel",
    "QueryNode",
    "Operation",
    "analyze",
    "analyze_script",
    "to_mermaid",
    "to_html",
    "walkthrough",
    "narrate",
    "build_factsheet",
    "NarrationUnavailable",
    "lint",
    "Finding",
    "max_severity",
    "meets_threshold",
    "column_lineage",
    "ColumnLineage",
    "build_schema_from_ddl",
    "load_schema",
    "analyze_project",
    "project_mermaid",
    "project_summary",
    "ProjectGraph",
    "__version__",
]
