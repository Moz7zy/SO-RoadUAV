"""Stable V3 identifiers with an explicit canonical field order."""

from __future__ import annotations

import re
from typing import Any

from benchmark_generator.common.identity import derive_seed as derive_common_seed
from benchmark_generator.common.identity import stable_digest

from .constants import (
    ID_RULE_VERSION,
    INVENTORY_ID_RULE_VERSION,
    PROTOCOL_VERSION,
    SITE_INTERFACE_RULE_VERSION,
)


SAMPLE_ID_FIELDS = [
    "protocol_version",
    "map_id",
    "site_id",
    "anchor_id",
    "observation_recipe_id",
    "world_seed",
    "sensor_seed",
]


def v3_digest(kind: str, value: Any, length: int = 16) -> str:
    return stable_digest(ID_RULE_VERSION, kind, value, length)


def derive_seed(run_seed: int, *parts: Any) -> int:
    return derive_common_seed(ID_RULE_VERSION, run_seed, *parts)


def canonical_sample_identity(values: dict[str, Any]) -> list[list[Any]]:
    missing = [field for field in SAMPLE_ID_FIELDS if field not in values]
    if missing:
        raise ValueError(f"V3 sample identity missing canonical fields: {missing}")
    return [[field, values[field]] for field in SAMPLE_ID_FIELDS]


def make_sample_id(
    map_id: str,
    site_id: str,
    anchor_id: str,
    observation_recipe_id: str,
    world_seed: int,
    sensor_seed: int,
    protocol_version: str = PROTOCOL_VERSION,
    layout_fingerprint: str | None = None,
) -> str:
    identity = canonical_sample_identity({
        "protocol_version": protocol_version,
        "map_id": map_id,
        "site_id": site_id,
        "anchor_id": anchor_id,
        "observation_recipe_id": observation_recipe_id,
        "world_seed": int(world_seed),
        "sensor_seed": int(sensor_seed),
    })
    if layout_fingerprint is not None:
        identity = [*identity, ["layout_fingerprint", str(layout_fingerprint)]]
    return "v3_" + v3_digest("sample", identity)


def make_recipe_id(recipe_family: str, payload: dict[str, Any]) -> str:
    return "recipe_" + v3_digest("observation_recipe", {"family": recipe_family, "payload": payload})


def make_control_bundle_id(anchor_id: str) -> str:
    return "bundle_" + v3_digest("control_bundle", anchor_id)


def make_candidate_site_id(map_id: str, source_refs: list[str], classification: dict[str, Any]) -> str:
    payload = {
        "rule_version": SITE_INTERFACE_RULE_VERSION,
        "map_id": map_id,
        "source_refs": sorted(str(item) for item in source_refs),
        "classification": classification,
    }
    return "site_" + v3_digest("candidate_site", payload)


def make_anchor_id(site_id: str, anchor_index: int, locator: dict[str, Any]) -> str:
    payload = {
        "rule_version": SITE_INTERFACE_RULE_VERSION,
        "site_id": site_id,
        "anchor_index": int(anchor_index),
        "locator": locator,
    }
    return "anchor_" + v3_digest("candidate_anchor", payload)


def make_road_unit_id(map_id: str, road_id: str, section_id: int) -> str:
    payload = {"map_id": map_id, "road_id": str(road_id), "section_id": int(section_id)}
    return "roadunit_" + stable_digest(INVENTORY_ID_RULE_VERSION, "road_unit", payload)


def make_inventory_site_id(map_id: str, feature_kind: str, source_refs: list[str]) -> str:
    payload = {
        "map_id": map_id,
        "feature_kind": feature_kind,
        "source_refs": sorted(str(item) for item in source_refs),
    }
    return "site_" + stable_digest(INVENTORY_ID_RULE_VERSION, "candidate_site", payload)


def make_leakage_group_id(map_id: str, feature_kind: str, source_refs: list[str]) -> str:
    payload = {
        "map_id": map_id,
        "feature_kind": feature_kind,
        "source_refs": sorted(str(item) for item in source_refs),
    }
    return "leakage_" + stable_digest(INVENTORY_ID_RULE_VERSION, "leakage_group", payload)


def make_inventory_anchor_id(site_id: str, locator: dict[str, Any]) -> str:
    stable_locator = {
        "road_id": str(locator["road_id"]),
        "section_id": int(locator["section_id"]),
        "s_m": round(float(locator["s_m"]), 3),
        "target_direction": str(locator["target_direction"]),
        "placement_basis": str(locator["placement_basis"]),
    }
    return "anchor_" + stable_digest(
        INVENTORY_ID_RULE_VERSION,
        "candidate_anchor",
        {"site_id": site_id, "locator": stable_locator},
    )


def make_preview_id(site_id: str, anchor_id: str) -> str:
    return "preview_" + stable_digest(
        INVENTORY_ID_RULE_VERSION,
        "preview",
        {"site_id": site_id, "anchor_id": anchor_id},
    )


def make_review_decision_id(site_id: str, action: str, timestamp: str, reviewer: str) -> str:
    return "decision_" + stable_digest(
        INVENTORY_ID_RULE_VERSION,
        "site_review_decision",
        {"site_id": site_id, "action": action, "timestamp": timestamp, "reviewer": reviewer},
    )


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")


def make_display_name(map_id: str, structural_cell: str, site_id: str, anchor_id: str, family: str, sample_id: str) -> str:
    return "_".join([
        slug(map_id),
        slug(structural_cell),
        slug(site_id)[-8:],
        slug(anchor_id)[-8:],
        slug(family),
        sample_id[-8:],
    ])
