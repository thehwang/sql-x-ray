"""IR -> fluent prose via a local LLM (Ollama).

The LLM sits at the *outer edge* of the pipeline. It never sees the raw SQL and is
never asked to understand it; it only rephrases an already-parsed, deterministic
fact sheet built from the IR. This keeps hallucination low: there are no tables or
columns for it to invent because we tell it exactly which ones exist.

No third-party dependency: we POST to the Ollama HTTP API with the stdlib. If
Ollama is unreachable or the model is missing, callers should fall back to the
template walkthrough.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .ir import QueryNode, SqlModel

DEFAULT_MODEL = os.environ.get("SXR_OLLAMA_MODEL", "llama3.2")
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_LANG = os.environ.get("SXR_LANG") or None

_DETAIL_CAP = 200

_SYSTEM = (
    "You are SQL X-Ray, a tool that explains SQL in plain English for engineers. "
    "You are given a STRUCTURED, ALREADY-PARSED summary of one SQL statement.\n\n"
    "STRICT RULES:\n"
    "- Use ONLY the facts provided. NEVER invent table names, column names, or "
    "operations that are not in the facts.\n"
    "- Do not speculate about business meaning beyond what names plainly suggest.\n"
    "- Be concise: one short numbered step per CTE/SELECT, in order.\n"
    "- Put table and step names in backticks.\n"
    "- End with a single line starting 'In short:' summarizing what the statement does.\n"
)


class NarrationUnavailable(RuntimeError):
    """Raised when the local LLM backend can't be reached or fails."""


def _clip(text: str, cap: int = _DETAIL_CAP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= cap else text[: cap - 1] + "\u2026"


def _node_facts(node: QueryNode) -> str:
    role = "final SELECT" if node.is_final else "CTE"
    lines = [f"- {node.name} ({role})"]
    srcs = []
    if node.source_tables:
        srcs.append("tables: " + ", ".join(node.source_tables))
    if node.source_ctes:
        srcs.append("steps: " + ", ".join(node.source_ctes))
    lines.append("    reads from: " + ("; ".join(srcs) if srcs else "(none)"))
    if node.operations:
        lines.append("    operations:")
        for op in node.operations:
            label = op.brief or _clip(op.detail, 80)
            lines.append(f"      - {op.kind}: {_clip(op.detail) if op.detail else label}")
    if node.output_columns:
        cols = ", ".join(node.output_columns)
        lines.append(f"    outputs ({len(node.output_columns)}): {_clip(cols, 240)}")
    return "\n".join(lines)


def build_factsheet(model: SqlModel) -> str:
    """A compact, deterministic text view of the IR — the ground truth for the LLM."""
    head = f"STATEMENT KIND: {model.statement_kind}"
    if model.target_table:
        head += f"\nWRITE TARGET: {model.target_table}"
    if model.templated:
        head += "\nNOTE: Jinja templating was substituted with `jinja_*` placeholders."
    body = "\n".join(_node_facts(n) for n in model.nodes)
    return f"{head}\nSTEPS:\n{body}"


def _ollama_generate(prompt: str, model_name: str, host: str, timeout: float) -> str:
    url = f"{host.rstrip('/')}/api/generate"
    payload = {
        "model": model_name,
        "prompt": prompt,
        "system": _SYSTEM,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else str(exc)
        raise NarrationUnavailable(
            f"Ollama returned HTTP {exc.code}. Is the model '{model_name}' pulled? "
            f"Try `ollama pull {model_name}`. ({_clip(detail, 120)})"
        ) from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        raise NarrationUnavailable(
            f"Cannot reach Ollama at {host}. Is it running (`ollama serve`)? ({exc})"
        ) from exc

    text = (parsed.get("response") or "").strip()
    if not text:
        raise NarrationUnavailable("Ollama returned an empty response.")
    return text


def narrate(
    model: SqlModel,
    model_name: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    timeout: float = 60.0,
    language: str | None = DEFAULT_LANG,
) -> str:
    """Return an LLM-written walkthrough, or raise NarrationUnavailable.

    ``language`` (e.g. "Chinese", "zh", "Japanese") makes the model write the prose
    in that language while keeping SQL identifiers unchanged. ``None`` uses English.
    """
    facts = build_factsheet(model)
    lang_instruction = ""
    if language:
        lang_instruction = (
            f"\n\nWrite your ENTIRE explanation in {language}. "
            "Keep all SQL identifiers (table, column, and step names) and the "
            "'In short:' marker exactly as given, untranslated."
        )
    prompt = f"FACTS:\n{facts}{lang_instruction}\n\nWrite the walkthrough now."
    return _ollama_generate(prompt, model_name, host, timeout)
