"""Versioned canonical replay payloads for R4 scripted traffic.

CARLA actor runtime IDs are intentionally excluded.  Planned and actual
records use the same protocol fields and a 1 mm / 0.001 degree transform
quantization so harmless simulator serialization noise cannot create a false
mismatch while a real layout change remains visible.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from benchmark_generator.common.identity import canonical_json


REPLAY_CANONICAL_SCHEMA_VERSION = "benchmark_v3_r4_replay_canonical_0.3"
REPLAY_CANONICAL_RULE_VERSION = "benchmark_v3_r4_replay_canonical_payload_0.3"
REPLAY_TRANSFORM_PRECISION = 3
REPLAY_ROTATION_PRECISION = 1
REPLAY_ACTOR_FIELDS = (
    "actor_id",
    "vehicle_blueprint_id",
    "master_ordinal",
    "logical_lane_index",
    "lane_id",
    "frozen_lane_id",
    "waypoint_road_id",
    "waypoint_section_id",
    "waypoint_s_m",
    "transform",
)


def _number(value: Any) -> float:
    return round(float(value), REPLAY_TRANSFORM_PRECISION)


def _angle(value: Any) -> float:
    normalized = ((float(value) + 180.0) % 360.0) - 180.0
    rounded = round(normalized, REPLAY_ROTATION_PRECISION)
    return 0.0 if abs(rounded) < 0.5 * 10 ** (-REPLAY_ROTATION_PRECISION) else rounded


def canonical_transform(value: dict[str, Any], *, frame: str = "carla_unreal_frozen_waypoint_r4_v2") -> dict[str, Any]:
    location = value.get("location", {})
    rotation = value.get("rotation", {})
    return {
        "frame": str(value.get("frame", frame)),
        "location": {
            "x": _number(location["x"]),
            "y": _number(location["y"]),
            "z": _number(location["z"]),
        },
        "rotation": {
            "pitch": _angle(rotation["pitch"]),
            "yaw": _angle(rotation["yaw"]),
            "roll": _angle(rotation["roll"]),
        },
    }


def canonical_actor(actor: dict[str, Any], *, actual: bool = False) -> dict[str, Any]:
    transform_key = "resolved_transform" if actual else "transform"
    transform = actor.get(transform_key) or actor.get("transform")
    if not isinstance(transform, dict):
        raise ValueError(f"replay actor has no {transform_key}: {actor.get('actor_id')}")
    result: dict[str, Any] = {
        "actor_id": str(actor["actor_id"]),
        "vehicle_blueprint_id": str(actor["vehicle_blueprint_id"]),
        "master_ordinal": int(actor["master_ordinal"]),
        "logical_lane_index": int(actor["logical_lane_index"]),
        "lane_id": str(actor.get("lane_id", actor.get("frozen_lane_id", ""))),
        "frozen_lane_id": int(actor["frozen_lane_id"]) if actor.get("frozen_lane_id") is not None else None,
        "waypoint_road_id": int(actor["waypoint_road_id"]) if actor.get("waypoint_road_id") is not None else None,
        "waypoint_section_id": int(actor["waypoint_section_id"]) if actor.get("waypoint_section_id") is not None else None,
        "waypoint_s_m": _number(actor["waypoint_s_m"]) if actor.get("waypoint_s_m") is not None else None,
        "transform": canonical_transform(transform),
    }
    return {key: result[key] for key in REPLAY_ACTOR_FIELDS}


def canonical_payload(
    *,
    protocol_version: str,
    master_layout_hash: str,
    density: float,
    actors: Iterable[dict[str, Any]],
    actual: bool = False,
) -> dict[str, Any]:
    canonical_actors = [canonical_actor(actor, actual=actual) for actor in actors]
    canonical_actors.sort(key=lambda item: (item["logical_lane_index"], item["master_ordinal"], item["actor_id"]))
    return {
        "schema_version": REPLAY_CANONICAL_SCHEMA_VERSION,
        "rule_version": REPLAY_CANONICAL_RULE_VERSION,
        "protocol_version": str(protocol_version),
        "master_layout_hash": str(master_layout_hash),
        "density": float(density),
        "actor_sort": ["logical_lane_index", "master_ordinal", "actor_id"],
        "transform_precision": {"location_m": REPLAY_TRANSFORM_PRECISION, "rotation_deg": REPLAY_ROTATION_PRECISION},
        "actor_fields": list(REPLAY_ACTOR_FIELDS),
        "actors": canonical_actors,
    }


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_replay(
    *,
    protocol_version: str,
    master_layout_hash: str,
    density: float,
    planned_actors: Iterable[dict[str, Any]],
    actual_actors: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    planned_payload = canonical_payload(
        protocol_version=protocol_version,
        master_layout_hash=master_layout_hash,
        density=density,
        actors=planned_actors,
        actual=False,
    )
    result: dict[str, Any] = {
        "schema_version": REPLAY_CANONICAL_SCHEMA_VERSION,
        "rule_version": REPLAY_CANONICAL_RULE_VERSION,
        "planned_payload": planned_payload,
        "planned_hash": payload_hash(planned_payload),
    }
    if actual_actors is not None:
        actual_payload = canonical_payload(
            protocol_version=protocol_version,
            master_layout_hash=master_layout_hash,
            density=density,
            actors=actual_actors,
            actual=True,
        )
        actual_hash = payload_hash(actual_payload)
        result.update({
            "actual_payload": actual_payload,
            "actual_hash": actual_hash,
            "match": result["planned_hash"] == actual_hash,
            "mismatch_diagnostics": replay_mismatch_diagnostics(planned_payload, actual_payload),
        })
    else:
        result.update({"actual_payload": None, "actual_hash": None, "match": None, "mismatch_diagnostics": None})
    return result


def replay_mismatch_diagnostics(planned: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    planned_by_id = {item["actor_id"]: item for item in planned.get("actors", [])}
    actual_by_id = {item["actor_id"]: item for item in actual.get("actors", [])}
    missing = sorted(set(planned_by_id) - set(actual_by_id))
    unexpected = sorted(set(actual_by_id) - set(planned_by_id))
    field_mismatches: list[dict[str, Any]] = []
    for actor_id in sorted(set(planned_by_id) & set(actual_by_id)):
        if planned_by_id[actor_id] != actual_by_id[actor_id]:
            field_mismatches.append({
                "actor_id": actor_id,
                "planned": planned_by_id[actor_id],
                "actual": actual_by_id[actor_id],
            })
    return {
        "status": "match" if not missing and not unexpected and not field_mismatches else "mismatch",
        "missing_actor_ids": missing,
        "unexpected_actor_ids": unexpected,
        "field_mismatch_count": len(field_mismatches),
        "field_mismatches": field_mismatches,
    }


__all__ = [
    "REPLAY_ACTOR_FIELDS",
    "REPLAY_CANONICAL_RULE_VERSION",
    "REPLAY_CANONICAL_SCHEMA_VERSION",
    "REPLAY_ROTATION_PRECISION",
    "REPLAY_TRANSFORM_PRECISION",
    "canonical_actor",
    "canonical_payload",
    "canonical_replay",
    "canonical_transform",
    "payload_hash",
    "replay_mismatch_diagnostics",
]
