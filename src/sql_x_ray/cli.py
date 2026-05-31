"""SQL X-Ray command-line interface.

Default (no flags): print the walkthrough followed by a Mermaid data-flow graph.
Use --json for tooling/CI, or --mermaid / --walkthrough to print just one section.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .analyzer import analyze_script
from .graph import to_mermaid
from .htmlout import to_html
from .lineage import column_lineage
from .lint import lint, meets_threshold
from .narrate import DEFAULT_LANG, DEFAULT_MODEL, NarrationUnavailable, narrate
from .walkthrough import walkthrough


def _read_sql(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


_SEV_ICON = {"high": "✗", "medium": "⚠", "low": "·"}


def _run_lint(models, args) -> int:
    multi = len(models) > 1
    all_findings = []
    for i, model in enumerate(models, 1):
        findings = lint(model)
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
    print(
        f"{len(all_findings)} finding(s): "
        f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low"
    )
    if args.fail_on and meets_threshold(all_findings, args.fail_on):
        return 1
    return 0


def _run_lineage(models, args) -> int:
    column = None if args.lineage == "*ALL*" else args.lineage
    multi = len(models) > 1
    for i, model in enumerate(models, 1):
        if multi:
            print(f"### Statement {i} — {model.statement_kind}")
        rows = column_lineage(model, column=column)
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
        prog="sqlx-ray",
        description="See through any SQL: data-flow graph + plain-language walkthrough.",
    )
    parser.add_argument("file", help="path to a .sql file, or '-' to read stdin")
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
        "--fail-on",
        choices=("low", "medium", "high"),
        default=None,
        help="with --lint, exit non-zero if any finding is at or above this severity",
    )
    parser.add_argument("--version", action="version", version=f"sql-x-ray {__version__}")
    args = parser.parse_args(argv)

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
        return _run_lint(models, args)
    if args.lineage:
        return _run_lineage(models, args)
    if args.json:
        print(json.dumps([m.to_dict() for m in models], indent=2))
        return 0
    if args.html:
        print(
            to_html(
                models,
                title=f"SQL X-Ray — {args.file}",
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

    print("SQL X-Ray\n" + "=" * 40)
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
