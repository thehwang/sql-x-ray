"""Lint configuration (`.sqlucent.toml`) and baseline handling.

The config lets a project tune the linter without code changes:

```toml
[rules]
disable = ["distinct-with-group-by"]   # turn rules off

[rules.severity]
select-star = "low"                     # override a rule's severity

[cost]
require_partition_filter = true
[cost.partitions]                       # declare partitioned tables + their column
events = "event_date"
orders = "ts"
```

A *baseline* (a JSON file of current findings) lets teams adopt the linter on a
legacy codebase: existing findings are grandfathered in and only new ones surface.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # 3.10 fallback
    import tomli as tomllib  # type: ignore

CONFIG_NAME = ".sqlucent.toml"


@dataclass
class Config:
    disabled: set[str] = field(default_factory=set)
    severity: dict[str, str] = field(default_factory=dict)
    partitions: dict[str, str] = field(default_factory=dict)
    require_partition_filter: bool = True

    def apply(self, findings: list) -> list:
        """Drop disabled rules and apply severity overrides to a finding list."""
        out = []
        for f in findings:
            if f.rule in self.disabled:
                continue
            if f.rule in self.severity:
                f.severity = self.severity[f.rule]
            out.append(f)
        return out


def find_config(start: str | Path = ".") -> Path | None:
    """Locate `.sqlucent.toml` in `start` or any parent directory."""
    here = Path(start).resolve()
    for d in (here, *here.parents):
        candidate = d / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: str | Path | None = None, start: str | Path = ".") -> Config:
    """Load config from `path`, or auto-discover `.sqlucent.toml` from `start`."""
    p = Path(path) if path else find_config(start)
    if p is None or not Path(p).is_file():
        return Config()
    with open(p, "rb") as fh:
        data = tomllib.load(fh)

    rules = data.get("rules", {}) or {}
    cost = data.get("cost", {}) or {}
    severity = {str(k): str(v) for k, v in (rules.get("severity", {}) or {}).items()}
    partitions = {str(k): str(v) for k, v in (cost.get("partitions", {}) or {}).items()}
    return Config(
        disabled=set(rules.get("disable", []) or []),
        severity=severity,
        partitions=partitions,
        require_partition_filter=bool(cost.get("require_partition_filter", True)),
    )


# --- baseline ---------------------------------------------------------------


def fingerprint(file: str, finding) -> str:
    return f"{file}::{finding.rule}::{finding.node}::{finding.message}"


def load_baseline(path: str | Path) -> set[str]:
    p = Path(path)
    if not p.is_file():
        return set()
    data = json.loads(p.read_text(encoding="utf-8"))
    return set(data.get("fingerprints", []))


def write_baseline(path: str | Path, fingerprints: list[str]) -> None:
    payload = {"version": 1, "fingerprints": sorted(set(fingerprints))}
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
