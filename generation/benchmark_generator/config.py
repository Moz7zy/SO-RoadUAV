"""Config loading and path guards for benchmark_v1 generator prep."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"
FORBIDDEN_OUTPUT_PARTS = {
    "CarlaProject",
    "CarlaDataset",
    "UAVLanesSimple_v0",
    "CurvelanesCARLA",
    "toy",
    "diagnostic",
}


class ConfigError(ValueError):
    """Raised when a Phase D config violates static safety policy."""


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_path_within(path: Path, allowed_roots: Iterable[Path], label: str) -> Path:
    resolved = path.expanduser().resolve()
    roots = [root.expanduser().resolve() for root in allowed_roots]
    if not any(is_relative_to(resolved, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ConfigError(f"{label} path must stay under allowed roots [{allowed}], got {resolved}")
    return resolved


def forbid_legacy_source_path(path: Path, label: str) -> None:
    text = str(path)
    parts = set(path.parts)
    for forbidden in FORBIDDEN_OUTPUT_PARTS:
        if forbidden in parts or forbidden in text:
            raise ConfigError(f"{label} must not reference legacy/forbidden source path: {path}")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)




