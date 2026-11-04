"""Canonical JSON, stable digests, and deterministic seed derivation."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def stable_digest(rule_version: str, kind: str, value: Any, length: int = 16) -> str:
    payload = canonical_json({"rule_version": rule_version, "kind": kind, "value": value})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def derive_seed(rule_version: str, run_seed: int, *parts: Any) -> int:
    digest = stable_digest(rule_version, "seed", [int(run_seed), *parts], length=16)
    return int(digest, 16) & 0x7FFFFFFF

