"""Make real-world SQL parseable.

Airflow / dbt SQL is littered with Jinja: ``{{ params.x }}`` expressions,
``{% if %}`` control blocks, ``{# comments #}``. sqlglot can't parse those, so we
substitute them with safe placeholders before handing the text to the parser.

This is intentionally lightweight and best-effort: the goal is to keep the
*structure* (tables, joins, filters) analyzable, not to render the template.
A ``{{ params.target_schema }}.tbl`` becomes ``jinja_params_target_schema.tbl``,
so the real table name survives and the templated schema reads as a qualifier.
"""
from __future__ import annotations

import re

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_JINJA_BLOCK = re.compile(r"\{%.*?%\}", re.S)
_JINJA_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)


def _placeholder(match: re.Match[str]) -> str:
    inner = match.group(1).strip()
    ident = re.sub(r"\W+", "_", inner).strip("_").lower()
    return f"jinja_{ident or 'expr'}"


def strip_templating(sql: str) -> str:
    """Replace Jinja constructs with parser-safe placeholders."""
    sql = _JINJA_COMMENT.sub("", sql)
    sql = _JINJA_BLOCK.sub("", sql)  # control flow has no SQL value; drop it
    sql = _JINJA_EXPR.sub(_placeholder, sql)
    return sql


def has_templating(sql: str) -> bool:
    return bool(
        _JINJA_EXPR.search(sql) or _JINJA_BLOCK.search(sql) or _JINJA_COMMENT.search(sql)
    )
