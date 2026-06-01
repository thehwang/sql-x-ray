"""SQLucent command-line interface.

Default (no flags): print the walkthrough followed by a Mermaid data-flow graph.
Use --json for tooling/CI, or --mermaid / --walkthrough to print just one section.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .analyzer import analyze_script
from .config import fingerprint, load_baseline, load_config, write_baseline
from .graph import to_mermaid
from .htmlout import to_html
from .lineage import column_lineage
from .lint import lint, meets_threshold
from .project import analyze_project, project_mermaid, project_summary
from .impact import impact_report
from .schema import load_schema
from .narrate import DEFAULT_LANG, DEFAULT_MODEL, NarrationUnavailable, narrate
from .walkthrough import walkthrough


def _read_sql(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _run_project(args) -> int:
    graph = analyze_project(args.file, dialect=args.dialect)
    if not graph.files:
        print(f"error: no .sql files under {args.file!r}", file=sys.stderr)
        return 1
    if args.impact:
        print(impact_report(graph, args.impact))
        return 0
    if args.json:
        print(json.dumps(graph.to_dict(), indent=2))
        return 0
    if args.mermaid:
        print(project_mermaid(graph))
        return 0
    print(project_summary(graph))
    print("\nTable data-flow (Mermaid)\n-------------------------")
    print("```mermaid")
    print(project_mermaid(graph))
    print("```")
    return 0


_SEV_ICON = {"high": "✗", "medium": "⚠", "low": "·"}


def _run_lint(models, args, config) -> int:
    multi = len(models) > 1
    src = "<stdin>" if args.file == "-" else args.file

    # First pass: compute findings per statement (already config-filtered).
    per_stmt = [lint(model, config) for model in models]

    # --write-baseline: snapshot current findings and exit (no gating).
    if args.write_baseline:
        fps = [fingerprint(src, f) for findings in per_stmt for f in findings]
        write_baseline(args.write_baseline, fps)
        print(f"wrote baseline with {len(set(fps))} finding(s) to {args.write_baseline}")
        return 0

    suppress = load_baseline(args.baseline) if args.baseline else set()

    all_findings = []
    suppressed = 0
    for i, (model, findings) in enumerate(zip(models, per_stmt), 1):
        if suppress:
            kept = [f for f in findings if fingerprint(src, f) not in suppress]
            suppressed += len(findings) - len(kept)
            findings = kept
        all_findings.extend(findings)
        if multi:
            print(f"### Statement {i} — {model.statement_kind}")
        if not findings:
            print("  ✓ no risks found")
        for f in findings:
            icon = _SEV_ICON.get(f.severity, "·")
            print(f"  {icon} [{f.severity}] {f.rule} ({f.node}): {f.message}")
        if multi:
            print()

    counts = {s: sum(1 for f in all_findings if f.severity == s) for s in ("high", "medium", "low")}
    summary = (
        f"{len(all_findings)} finding(s): "
        f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low"
    )
    if suppressed:
        summary += f" ({suppressed} baselined)"
    print(summary)
    if args.fail_on and meets_threshold(all_findings, args.fail_on):
        return 1
    return 0


def _run_lineage(models, args) -> int:
    column = None if args.lineage == "*ALL*" else args.lineage
    schema = None
    if args.schema:
        try:
            schema = load_schema(args.schema, dialect=args.dialect)
        except (OSError, ValueError) as exc:
            print(f"[warning] could not load schema {args.schema!r}: {exc}", file=sys.stderr)
    multi = len(models) > 1
    for i, model in enumerate(models, 1):
        if multi:
            print(f"### Statement {i} — {model.statement_kind}")
        rows = column_lineage(model, column=column, schema=schema)
        if not rows:
            print("  (no traceable output columns)")
        for row in rows:
            if row.sources:
                print(f"  {row.column}  ←  {', '.join(row.sources)}")
            else:
                print(f"  {row.column}  ←  ({row.note})")
        if multi:
            print()
    return 0


def _walkthrough_text(model, args) -> str:
    """Walkthrough text: LLM prose when --narrate works, else the template."""
    if args.narrate or args.lang:  # --lang implies --narrate
        try:
            return narrate(model, model_name=args.model, language=args.lang)
        except NarrationUnavailable as exc:
            print(
                f"warning: --narrate fell back to template ({exc})",
                file=sys.stderr,
            )
    return walkthrough(model, verbose=args.verbose)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqlucent",
        description="See through any SQL: data-flow graph + plain-language walkthrough.",
    )
    parser.add_argument(
        "file",
        help="a .sql file, '-' for stdin, or a DIRECTORY for cross-file table lineage",
    )
    parser.add_argument(
        "--dialect", default="bigquery", help="SQL dialect (default: bigquery)"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show full join ON clauses, full column lists, etc.",
    )
    parser.add_argument(
        "--narrate",
        "--llm",
        dest="narrate",
        action="store_true",
        help="use a local LLM (Ollama) to write fluent prose; falls back to the "
        "template if Ollama is unavailable",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Ollama model for --narrate (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--lang",
        "--language",
        dest="lang",
        default=DEFAULT_LANG,
        help="language for --narrate output, e.g. 'Chinese', 'zh', 'Japanese' "
        "(default: English; or set SXR_LANG). Implies --narrate.",
    )
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="emit the IR as JSON")
    out.add_argument("--mermaid", action="store_true", help="emit only the Mermaid graph")
    out.add_argument(
        "--walkthrough", action="store_true", help="emit only the walkthrough"
    )
    out.add_argument(
        "--html",
        action="store_true",
        help="emit a self-contained interactive HTML page (redirect to a file)",
    )
    out.add_argument(
        "--lint",
        action="store_true",
        help="run risk checks (cross joins, SELECT *, HAVING pushdown, ...)",
    )
    out.add_argument(
        "--lineage",
        nargs="?",
        const="*ALL*",
        metavar="COLUMN",
        help="show column-level lineage; optionally for a single COLUMN",
    )
    parser.add_argument(
        "--cdn",
        action="store_true",
        help="with --html, load Mermaid from a CDN for a tiny file (default inlines "
        "Mermaid for a fully offline page)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to .sqlucent.toml (default: auto-discover near the file/cwd)",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="with --lint, suppress findings recorded in this baseline JSON",
    )
    parser.add_argument(
        "--write-baseline",
        default=None,
        metavar="PATH",
        help="with --lint, write current findings to PATH as a baseline and exit",
    )
    parser.add_argument(
        "--fail-on",
        choices=("low", "medium", "high"),
        default=None,
        help="with --lint, exit non-zero if any finding is at or above this severity",
    )
    parser.add_argument(
        "--schema",
        metavar="PATH",
        default=None,
        help="with --lineage, a DDL (.sql CREATE TABLE) or .json schema file so "
        "SELECT * expands into real columns and lineage is precise",
    )
    parser.add_argument(
        "--impact",
        metavar="TABLE[.COLUMN]",
        default=None,
        help="with a directory, show the blast radius if TABLE (or TABLE.COLUMN) changes",
    )
    parser.add_argument("--version", action="version", version=f"sqlucent {__version__}")
    args = parser.parse_args(argv)

    # Directory input → project-level (cross-file) table lineage.
    if args.file != "-" and os.path.isdir(args.file):
        return _run_project(args)

    try:
        sql = _read_sql(args.file)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    try:
        models = analyze_script(sql, dialect=args.dialect)
    except Exception as exc:  # sqlglot raises a variety of parse errors
        print(f"error: failed to parse SQL ({args.dialect}): {exc}", file=sys.stderr)
        return 1

    if not models:
        print("error: no statements found", file=sys.stderr)
        return 1

    if args.lint:
        start = "." if args.file == "-" else os.path.dirname(os.path.abspath(args.file))
        config = load_config(args.config, start=start)
        return _run_lint(models, args, config)
    if args.lineage:
        return _run_lineage(models, args)
    if args.json:
        print(json.dumps([m.to_dict() for m in models], indent=2))
        return 0
    if args.html:
        print(
            to_html(
                models,
                title=f"SQLucent — {args.file}",
                lang=args.lang,
                inline=not args.cdn,
            )
        )
        return 0
    if args.mermaid:
        print("\n\n".join(to_mermaid(m) for m in models))
        return 0
    if args.walkthrough:
        print("\n\n".join(_walkthrough_text(m, args) for m in models))
        return 0

    print("SQLucent\n" + "=" * 40)
    multi = len(models) > 1
    for i, model in enumerate(models, 1):
        if multi:
            print(f"\n### Statement {i} — {model.statement_kind}")
        print("\nWalkthrough\n-----------")
        print(_walkthrough_text(model, args))
        print("\nData-flow graph (Mermaid)\n-------------------------")
        print("```mermaid")
        print(to_mermaid(model))
        print("```")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
