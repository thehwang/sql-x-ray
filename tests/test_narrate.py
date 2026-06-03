"""Tests for the local-LLM narration path and its graceful fallback.

We never require a running Ollama in CI: the unreachable-backend path is the
contract that matters (it must raise `NarrationUnavailable`, and the CLI must
fall back to the deterministic template and warn on stderr).
"""
import importlib
from pathlib import Path

import pytest

from sqlucent import analyze, build_factsheet
from sqlucent.narrate import NarrationUnavailable, narrate
from sqlucent.cli import main

# `sqlucent.narrate` resolves to the re-exported *function* (via __init__), not the
# submodule, so grab the module object explicitly for monkeypatching its globals.
NARRATE_MOD = importlib.import_module("sqlucent.narrate")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "top_users.sql"


def _model():
    return analyze(EXAMPLE.read_text(), dialect="bigquery")


def test_narrate_raises_when_backend_unreachable():
    # Point at a port nothing is listening on; connection is refused immediately.
    with pytest.raises(NarrationUnavailable):
        narrate(_model(), host="http://localhost:9", timeout=1.0)


def test_narrate_is_built_from_factsheet(monkeypatch):
    # Prove the prompt is assembled from the deterministic fact sheet (the IR
    # summary), not from the raw statement text — that grounding is the safety
    # claim. The fact sheet may include extracted op expressions, but the model
    # is never asked to parse SQL itself.
    captured = {}

    def fake_generate(prompt, model_name, host, timeout):
        captured["prompt"] = prompt
        return "narrated prose"

    monkeypatch.setattr(NARRATE_MOD, "_ollama_generate", fake_generate)
    model = _model()
    out = narrate(model)
    assert out == "narrated prose"
    prompt = captured["prompt"]
    assert build_factsheet(model) in prompt  # the prompt is the factsheet verbatim
    assert "STATEMENT KIND: SELECT" in prompt
    assert "ranked" in prompt  # step names are in the factsheet


def test_narrate_language_instruction(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        NARRATE_MOD,
        "_ollama_generate",
        lambda prompt, *a, **k: captured.setdefault("p", prompt) or "x",
    )
    narrate(_model(), language="Chinese")
    assert "in Chinese" in captured["p"]


# --- CLI fallback behavior ---


def test_cli_narrate_falls_back_to_template(capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise NarrationUnavailable("Ollama not running")

    # The CLI imports `narrate` into its own namespace.
    monkeypatch.setattr("sqlucent.cli.narrate", boom)

    code = main([str(EXAMPLE), "--narrate", "--walkthrough"])
    captured = capsys.readouterr()
    assert code == 0
    # Falls back to the deterministic template (still useful output)...
    assert "`active`" in captured.out
    # ...and tells the user why, on stderr.
    assert "fell back to template" in captured.err
    assert "Ollama not running" in captured.err


def test_cli_lang_implies_narrate_and_falls_back(capsys, monkeypatch):
    monkeypatch.setattr(
        "sqlucent.cli.narrate",
        lambda *a, **k: (_ for _ in ()).throw(NarrationUnavailable("down")),
    )
    # --lang with no --narrate should still attempt narration, then fall back.
    code = main([str(EXAMPLE), "--lang", "Chinese", "--walkthrough"])
    captured = capsys.readouterr()
    assert code == 0
    assert "fell back to template" in captured.err
