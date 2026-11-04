"""CARLA-free planner for the independent Benchmark V3 Full.1 protocol.

Full.1 (V3-PLAN-20260808-FULL.1) applies the versioned split redesign
(benchmark_v3_full_split_redesign_0.2): Core 6 scenes x 4 sites = 24/60/960,
Challenge 29/66/1650, Development-Train 131/250/12000, Development-Val
20/33/1584, active 204/409/16194, 10 withheld companions. Assets are reused
from the old release only when the semantic fingerprint is identical (same
subset, same site/anchor, identical requested conditions and traffic
semantics); otherwise they are incremental recaptures. Sample IDs are
re-derived under the Full.1 protocol (never reusing old sample IDs).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import csv
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path
import random
from typing import Any

from benchmark_generator.config import ConfigError, ensure_path_within, load_json

from .constants import OFFICIAL_LABELS, V3_ARTIFACT_ROOT, V3_CONFIG_ROOT, V3_SCHEMA_ROOT
from .ids import derive_seed, make_control_bundle_id, make_display_name, make_recipe_id, make_sample_id
from benchmark_generator.common.io import file_sha256
from benchmark_generator.v3.capture_plan import (
    R4_ASSETS,
    R4_TRAFFIC_RULE_VERSION,
    _camera_geometry,
    _execution_condition,
    _generate_master_layout,
    _layout_qa,
    _recipe_expected_projection,
    _select_density_subset,
    _sha256_json,
    load_r4_config,
    traffic_actor_count,
)
from .recipes import _conditions, validate_l18_balance
from .schema_support import validate_schema_instance

FULL_1_CONFIG_PATH = V3_CONFIG_ROOT / "full_1_release_candidate.json"
FULL_1_STATIC_ROOT = V3_ARTIFACT_ROOT / "full_1_static"
FULL_1_EXECUTION_ROOT = V3_ARTIFACT_ROOT / "full_1"
FULL_1_REVIEW_ROOT = V3_ARTIFACT_ROOT / "full_1_review"
FULL_1_PROTOCOL_VERSION = "V3-PLAN-20260808-FULL.1"
FULL_1_RULE_VERSION = "benchmark_v3_full_1_planning_0.1"
FULL_1_ASSETS = tuple(R4_ASSETS)
MAP_IDS = ("Town04_Opt", "Town05_Opt", "Town10HD_Opt")
SUBSETS = ("development_train", "development_val", "core_test", "challenge_test")
ACTIVE_ROLES = ("development_train", "development_val", "core_test", "challenge_test")
CELL_TO_SCENE = {
    "Straight-2Lane": "straight_1_4", "Straight-4Lane": "straight_1_4",
    "Straight-6Lane": "straight_5_8", "Straight-8Lane": "straight_5_8",
    "Curve-2Lane": "curve_1_4", "Curve-4Lane": "curve_1_4",
    "Curve-6Lane": "curve_5_8", "Curve-8Lane": "curve_5_8",
    "Intersection-Approach-2Lane": "intersection", "Intersection-Approach-4Lane": "intersection",
    "Ramp-1-to-Mainline-8": "ramp",
}


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_full_1_config(path: Path = FULL_1_CONFIG_PATH) -> dict[str, Any]:
    config_path = ensure_path_within(Path(path), [V3_CONFIG_ROOT], "V3 Full.1 config")
    config = load_json(config_path)
    validate_schema_instance(config, load_json(V3_SCHEMA_ROOT / "full_1_config.schema.json"), "V3 Full.1 config")
    if config["official_labels"] != list(OFFICIAL_LABELS):
        raise ConfigError("Full.1 official labels drifted")
    if not str(config["runtime"]["carla_python"]).strip():
        raise ConfigError("Full.1 CARLA interpreter drifted")
    config["_config_path"] = str(config_path.resolve())
    config["_config_sha256"] = file_sha256(config_path)

    split = load_json(ensure_path_within(Path(config["source_split_decision"]), [V3_ARTIFACT_ROOT / "full_1_static_inputs"], "Full.1 split decision"))
    if split.get("decision_version") != "benchmark_v3_full_split_redesign_0.2":
        raise ConfigError("Full.1 split decision version drifted")
    config["_split_decision"] = split

    policy = load_json(ensure_path_within(Path(config["source_reporting_policy"]), [V3_ARTIFACT_ROOT / "full_1_static_inputs"], "Full.1 reporting policy"))
    config["_reporting_policy"] = policy

    scene = load_json(ensure_path_within(Path(config["source_scene_decision"]), [V3_ARTIFACT_ROOT / "scene_reclassification"], "Full.1 scene decision"))
    config["_scene_decision"] = scene

    final_v2 = ensure_path_within(Path(config["source_finalization_v2"]), [V3_ARTIFACT_ROOT / "finalization_v2"], "Full.1 finalization-v2")
    for name in ("site_registry.json", "anchor_registry.json", "split_manifest.json", "leakage_group_registry.json"):
        if not (final_v2 / name).is_file():
            raise ConfigError(f"Full.1 source missing {final_v2 / name}")
    sites = {item["site_id"]: item for item in load_json(final_v2 / "site_registry.json")["sites"]}
    anchors: dict[str, dict[str, Any]] = {}
    for item in load_json(final_v2 / "anchor_registry.json")["anchors"]:
        anchors[item["anchor_id"]] = item
    config["_source_sites"] = sites
    config["_source_anchors"] = anchors

    old_release = ensure_path_within(Path(config["source_old_release"]), [V3_ARTIFACT_ROOT / "full_release"], "Full.1 old release")
    old_ledger = _read_jsonl(old_release / "released_sample_ledger.jsonl")
    config["_old_ledger"] = old_ledger
    config["_old_release_root"] = str(old_release)

    csv_path = ensure_path_within(Path(config["source_candidate_csv"]), [V3_CONFIG_ROOT.parents[1] / "reports" / "benchmark_v3"], "Full.1 candidate CSV")
    with csv_path.open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    if file_sha256(csv_path) != "09a2ff3c3531907f591d4690b6a4a39b43a9934037d2bee0430725eb26c53540":
        raise ConfigError("Full.1 candidate CSV checksum drifted")
    config["_candidate_rows"] = csv_rows

    challenge_path = ensure_path_within(Path(config["challenge_l18_path"]), [V3_CONFIG_ROOT], "Full.1 L18")
    challenge = load_json(challenge_path)
    if challenge.get("protocol_version") != "V3-PLAN-20260731-RC-V2.0":
        raise ConfigError("Full.1 L18 source is not the frozen RC-v2 L18 mapping")
    validate_l18_balance(challenge)
    config["_challenge_l18"] = challenge

    base_path = ensure_path_within(Path(config["engineering_base_config"]), [V3_CONFIG_ROOT], "Full.1 engineering base")
    execution = load_r4_config(base_path)
    execution["_source_sites"] = sites
    execution["_source_anchors"] = anchors
    execution["lifecycle"].update(config["lifecycle"])
    execution["limits"].update(config["limits"])
    execution["runtime"].update(config["runtime"])
    execution["sharding"].update(config["sharding"])
    execution["traffic"]["layout_rule_version"] = R4_TRAFFIC_RULE_VERSION
    execution["traffic"]["master_layout_seed"] = int(config["seeds"]["traffic_master"])
    execution["traffic"]["vehicle_blueprint_id"] = config["traffic"]["vehicle_pool"][0]
    execution["traffic"]["qa"]["longitudinal_coverage_ratio_min"] = 0.0
    execution["traffic"].pop("topology_layout_path", None)
    execution["traffic"].pop("conflict_exclusion_rule_version", None)
    execution["traffic"].pop("conflict_zone_radius_m", None)
    execution.pop("_traffic_topology", None)
    execution.pop("_traffic_topology_path", None)
    execution["core"]["defaults"].update({
        "height_m": 20.0, "pitch_deg": -80.0, "yaw_relative_deg": 0.0,
        "traffic_density_vehicles_per_lane_km": 0.0, "weather_family": "dry", "sun_altitude_deg": 75.0,
    })
    execution["_config_path"] = config["_config_path"]
    execution["_config_sha256"] = config["_config_sha256"]
    config["_execution_config"] = execution

    source_files = {
        "split_decision.json": Path(config["source_split_decision"]),
        "reporting_policy.json": Path(config["source_reporting_policy"]),
        "scene_decision.json": Path(config["source_scene_decision"]),
        "finalization_v2/site_registry.json": final_v2 / "site_registry.json",
        "finalization_v2/anchor_registry.json": final_v2 / "anchor_registry.json",
        "finalization_v2/split_manifest.json": final_v2 / "split_manifest.json",
        "finalization_v2/leakage_group_registry.json": final_v2 / "leakage_group_registry.json",
        "old_release/release_manifest.json": old_release / "release_manifest.json",
        "old_release/released_sample_ledger.jsonl": old_release / "released_sample_ledger.jsonl",
        "config/full_1_release_candidate.json": config_path,
        "schema/full_1_config.schema.json": V3_SCHEMA_ROOT / "full_1_config.schema.json",
    }
    config.update({"_source_checksums": {key: file_sha256(value) for key, value in source_files.items()}})
    return config


def _development_forward_capacity_m(site: dict[str, Any], anchor: dict[str, Any]) -> float:
    corridor_length = sum(float(span["length_m"]) for span in site.get("source_spans", []))
    locator = anchor.get("locator", {})
    offset = locator.get("corridor_offset_m")
    if offset is None:
        return round(corridor_length, 6)
    offset_m = max(0.0, min(corridor_length, float(offset)))
    if locator.get("target_direction") == "toward_junction_opposite_corridor_traversal":
        return round(offset_m, 6)
    return round(max(0.0, corridor_length - offset_m), 6)


def _stratified(rank: int, count: int, low: float, high: float, seed: int, key: str) -> float:
    rng = random.Random(derive_seed(seed, key, rank, count))
    unit = (rank + rng.random()) / count
    return round(low + (high - low) * unit, 6)


def _stratified_power(rank: int, count: int, low: float, high: float, seed: int, key: str, exponent: float) -> float:
    linear = _stratified(rank, count, low, high, seed, key)
    unit = (linear - low) / (high - low)
    return round(low + (high - low) * unit**exponent, 6)


def _folded_edge_rank(rank: int, count: int) -> int:
    return rank // 2 if rank % 2 == 0 else count - 1 - rank // 2


def _weather(config: dict[str, Any], family: str) -> dict[str, Any]:
    return {"family": family, **deepcopy(config["weather_representatives"][family])}


def _recipe(payload: dict[str, Any], family: str, subset: str, conditions: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "observation_recipe_id": make_recipe_id("full1", {"rule_version": FULL_1_RULE_VERSION, **payload}),
        "recipe_family": family, "subset_scope": subset, "rule_version": FULL_1_RULE_VERSION,
        "requested_conditions": conditions, "factor_levels": fields.pop("factor_levels", {}),
        "is_reference": fields.pop("is_reference", False), "changed_factor": fields.pop("changed_factor", None),
        "factor_level": fields.pop("factor_level", None), "challenge_row_id": fields.pop("challenge_row_id", None),
        "challenge_level_tuple": fields.pop("challenge_level_tuple", None), "challenge_axis_id": fields.pop("challenge_axis_id", None),
        **fields,
    }


def _new_development_recipes(config: dict[str, Any], anchor: dict[str, Any], subset: str, anchor_rank: int, anchor_count: int) -> list[dict[str, Any]]:
    """Full.1 dedicated sampling for Development anchors without an inherited
    old-release row set. Seeds contain no protocol string."""
    seed = int(config["seeds"]["development_train" if subset == "development_train" else "development_val"])
    ranges = config["development"]["ranges"]
    exponent = float(config["development"]["capacity_rank_exponent"])
    per_anchor = int(config["selection"]["development_recipes_per_anchor"])
    recipes = []
    zero_first = derive_seed(seed, anchor["anchor_id"], "traffic_half") % 2 == 0
    for ordinal in range(per_anchor):
        rank = anchor_rank * per_anchor + ordinal
        count = anchor_count * per_anchor
        traffic_zero = (ordinal % 2 == 0) == (not zero_first)
        half = ordinal // 2
        density = 0.0 if traffic_zero else _stratified(half, per_anchor // 2, 10.0, 40.0, seed, "traffic")
        phase = None if traffic_zero else _stratified(half, per_anchor // 2, 0.25, 0.75, seed, "lane_change_phase")
        family = config["development"]["weather_families"][(ordinal + derive_seed(seed, "weather")) % 3]
        conditions = _conditions(
            height_m=_stratified_power(rank, count, *map(float, ranges["height_m"]), seed, "height", exponent),
            pitch_deg=_stratified_power(rank, count, *map(float, ranges["pitch_deg"]), seed, "pitch", exponent),
            yaw_relative_deg=_stratified(_folded_edge_rank(ordinal, per_anchor), per_anchor, *map(float, ranges["yaw_relative_deg"]), seed, "yaw"),
            traffic_density=density, weather=_weather(config, family),
            sun_altitude_deg=_stratified(ordinal, per_anchor, *map(float, ranges["sun_altitude_deg"]), seed, "sun"),
        )
        conditions["lane_change_phase"] = phase
        recipes.append(_recipe(
            {"subset": subset, "anchor_id": anchor["anchor_id"], "ordinal": ordinal, "conditions": conditions},
            "full_1_development_stratified", subset, conditions, is_reference=False,
            changed_factor="multi_factor_random", factor_level=None, sampling_seed=seed,
            development_traffic_branch="zero" if traffic_zero else "nonzero",
        ))
    return recipes


def _core_recipes(config: dict[str, Any], anchor: dict[str, Any], yaw_sign: int) -> list[dict[str, Any]]:
    default = {"height":20.0,"pitch":-80.0,"yaw":0.0,"traffic":0.0,"weather":"dry","illumination":75.0}
    specs = [
        ("reference", "default", default),
        *(("height", str(value), {**default, "height": float(value)}) for value in (40, 60, 80, 100)),
        *(("pitch", str(value), {**default, "pitch": float(value)}) for value in (-90, -70)),
        *(("relative_yaw", str(value), {**default, "yaw": float(yaw_sign * value)}) for value in (5, 10, 15)),
        *(("traffic_stress", str(value), {**default, "traffic": float(value)}) for value in (20, 40)),
        *(("weather", value, {**default, "weather": value}) for value in ("rain-wet", "fog")),
        *(("illumination", str(value), {**default, "illumination": float(value)}) for value in (15, -5)),
    ]
    result = []
    for ordinal, (factor, level, values) in enumerate(specs):
        conditions = _conditions(
            height_m=values["height"], pitch_deg=values["pitch"], yaw_relative_deg=values["yaw"],
            traffic_density=values["traffic"], weather=_weather(config, values["weather"]), sun_altitude_deg=values["illumination"],
        )
        conditions["lane_change_phase"] = 0.5 if values["traffic"] > 0 else None
        levels = {"height":f"{values['height']:g}","pitch":f"{values['pitch']:g}","relative_yaw":f"{abs(values['yaw']):g}","traffic_stress":f"{values['traffic']:g}","weather":values["weather"],"illumination":f"{values['illumination']:g}"}
        result.append(_recipe(
            {"anchor_yaw_sign": yaw_sign, "factor": factor, "level": level, "conditions": conditions},
            "full_1_core_16", "core_test", conditions, factor_levels=levels,
            is_reference=ordinal == 0, changed_factor=factor, factor_level="default" if ordinal == 0 else level,
        ))
    if len(result) != 16:
        raise ConfigError("Full.1 Core recipe count drifted")
    return result


def _challenge_recipes(config: dict[str, Any], anchor: dict[str, Any], yaw_sign: int, anchor_rank: int, anchor_count: int) -> list[dict[str, Any]]:
    challenge = config["_challenge_l18"]
    result = []
    factors = challenge["factor_order"]
    for ordinal, row in enumerate(challenge["rows"]):
        values = {factor: challenge["factor_level_values"][factor][str(row["levels"][factor])] for factor in factors}
        signed_yaw = float(values["relative_yaw"]) * yaw_sign
        conditions = _conditions(
            height_m=values["height"], pitch_deg=values["pitch"], yaw_relative_deg=signed_yaw,
            traffic_density=values["traffic_density"], weather=_weather(config, values["weather"]), sun_altitude_deg=values["illumination"],
        )
        conditions["lane_change_phase"] = 0.5 if float(values["traffic_density"]) > 0 else None
        level_tuple = [int(row["levels"][factor]) for factor in factors]
        result.append(_recipe(
            {"anchor_yaw_sign":yaw_sign,"row_id":row["challenge_row_id"],"level_tuple":level_tuple,"conditions":conditions},
            "full_1_challenge_l18", "challenge_test", conditions,
            factor_levels={factor:f"L{row['levels'][factor]}" for factor in factors}, is_reference=ordinal == 0,
            changed_factor="reference" if ordinal == 0 else "multi_factor", factor_level="default" if ordinal == 0 else None,
            challenge_row_id=row["challenge_row_id"], challenge_level_tuple=level_tuple,
        ))
    axis_seed = int(config["seeds"]["challenge_axis"])
    defaults = {"height":20.0,"pitch":-80.0,"yaw":0.0,"traffic":0.0,"weather":"dry","illumination":75.0}
    axis_values = {
        "height": {**defaults, "height": _stratified(anchor_rank, anchor_count, 70, 90, axis_seed, "axis_height")},
        "nadir_view": {**defaults, "pitch": -90.0},
        "yaw": {**defaults, "yaw": float(15 * yaw_sign)},
        "traffic": {**defaults, "traffic": _stratified(anchor_rank, anchor_count, 30, 40, axis_seed, "axis_traffic")},
        "weather_rain": {**defaults, "weather": "rain-wet"},
        "weather_fog": {**defaults, "weather": "fog"},
        "illumination": {**defaults, "illumination": -5.0},
    }
    for axis_id in config["challenge_axis"]["axis_ids"]:
        values = axis_values[axis_id]
        conditions = _conditions(
            height_m=values["height"], pitch_deg=values["pitch"], yaw_relative_deg=values["yaw"],
            traffic_density=values["traffic"], weather=_weather(config, values["weather"]), sun_altitude_deg=values["illumination"],
        )
        conditions["lane_change_phase"] = 0.5 if values["traffic"] > 0 else None
        result.append(_recipe(
            {"anchor_yaw_sign":yaw_sign,"axis_id":axis_id,"conditions":conditions},
            "full_1_challenge_axis", "challenge_test", conditions, is_reference=False,
            changed_factor=axis_id, factor_level="axis", challenge_axis_id=axis_id,
        ))
    if len(result) != 25:
        raise ConfigError("Full.1 Challenge recipe count drifted")
    return result


def _inherit_recipes_from_old(old_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild recipes from the inherited old-release conditions. Conditions
    are copied verbatim; the recipe id is re-derived under the full1
    namespace so sample ids never collide with the old release."""
    recipes = []
    for old in old_rows:
        conditions = deepcopy(old["requested_conditions"])
        recipes.append(_recipe(
            {"subset": old["subset"], "anchor_id": old["anchor_id"],
             "ordinal": old["observation_recipe_id"], "conditions": conditions},
            old.get("recipe_family", "full_1_inherited"), old["subset"], conditions,
            is_reference=bool(old["is_reference"]), changed_factor=old.get("changed_factor"),
            factor_level=old.get("factor_level"), factor_levels=deepcopy(old.get("factor_levels", {})),
            challenge_row_id=old.get("challenge_row_id"), challenge_level_tuple=old.get("challenge_level_tuple"),
            challenge_axis_id=old.get("challenge_axis_id"),
            inherited_from_sample_id=old["sample_id"],
        ))
    return recipes


def _lane_change_capability(site: dict[str, Any], anchor: dict[str, Any]) -> tuple[bool, str | None]:
    if site["road_layout"] in {"Intersection-Approach", "Ramp-Transition"}:
        return False, "intersection_or_ramp"
    if int(anchor["lane_count_at_anchor"]) < 4:
        return False, "fewer_than_four_total_driving_lanes"
    return True, None


def _vehicle_pool_and_lane_change(config: dict[str, Any], site: dict[str, Any], anchor: dict[str, Any], master: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(master)
    pool = config["traffic"]["vehicle_pool"]
    for actor in value["actors"]:
        actor["vehicle_blueprint_id"] = pool[derive_seed(int(config["seeds"]["vehicle_pool"]), anchor["anchor_id"], actor["actor_id"]) % len(pool)]
        actor["lane_change"] = {"selected":False,"phase":None,"direction":0,"runtime_recheck":"not_applicable"}
    supported, reason = _lane_change_capability(site, anchor)
    candidates = []
    clearance = float(config["traffic"]["adjacent_actor_clearance_m"])
    lane_count = int(anchor["lane_count_at_anchor"])
    if supported:
        for actor in value["actors"]:
            lane = int(actor["logical_lane_index"])
            direction = 1 if lane < lane_count - 1 else -1
            adjacent = lane + direction
            if all(
                abs(float(other["longitudinal_offset_m"]) - float(actor["longitudinal_offset_m"])) >= clearance
                for other in value["actors"] if int(other["logical_lane_index"]) == adjacent
            ):
                candidates.append((actor, direction))
    if supported and not candidates:
        supported, reason = False, "no_clear_adjacent_actor_slot"
    if supported:
        half_ids = {item["actor_id"] for item in _select_density_subset(value, 20.0, config["_execution_config"])[0]}
        candidates.sort(key=lambda pair: (pair[0]["actor_id"] not in half_ids, derive_seed(int(config["seeds"]["lane_change"]), anchor["anchor_id"], pair[0]["actor_id"])))
        count = max(1, round(len(value["actors"]) * float(config["traffic"]["lane_change_fraction"])))
        for actor, direction in candidates[:count]:
            _apply_lane_change(actor, direction, 0.5, float(config["_execution_config"]["traffic"]["lane_width_m"]))
    value["lane_change_supported"] = supported
    value["lane_change_unsupported_reason"] = reason
    value["vehicle_pool_version"] = config["traffic"]["vehicle_pool_rule_version"]
    value["lane_change_rule_version"] = config["traffic"]["lane_change_rule_version"]
    value["protocol_version"] = FULL_1_PROTOCOL_VERSION
    value["master_layout_hash"] = _sha256_json({key: item for key, item in value.items() if key != "master_layout_hash"})
    return value


def _apply_lane_change(actor: dict[str, Any], direction: int, phase: float, lane_width: float) -> None:
    if not actor["lane_change"].get("center_transform"):
        actor["lane_change"]["center_transform"] = deepcopy(actor["transform"])
        actor["lane_change"]["center_lateral_offset_m"] = float(actor["lateral_offset_m"])
    center = actor["lane_change"]["center_transform"]
    smooth = 3.0 * phase ** 2 - 2.0 * phase ** 3
    shift = direction * lane_width * smooth
    yaw_offset = direction * 8.0 * math.sin(math.pi * phase)
    yaw = math.radians(float(center["rotation"]["yaw"]))
    left = (-math.sin(yaw), math.cos(yaw))
    actor["transform"] = deepcopy(center)
    actor["transform"]["location"]["x"] = round(float(center["location"]["x"]) + left[0] * shift, 3)
    actor["transform"]["location"]["y"] = round(float(center["location"]["y"]) + left[1] * shift, 3)
    actor["transform"]["rotation"]["yaw"] = round(float(center["rotation"]["yaw"]) + yaw_offset, 1)
    actor["lateral_offset_m"] = round(float(actor["lane_change"]["center_lateral_offset_m"]) + shift, 6)
    actor["lane_change"].update({"selected":True,"phase":round(phase,6),"direction":direction,"lateral_shift_m":round(shift,6),"yaw_offset_deg":round(yaw_offset,6),"runtime_recheck":"pending_carla"})


def _select_arbitrary_density(master: dict[str, Any], density: float, config: dict[str, Any]) -> list[dict[str, Any]]:
    lane_count = int(master["lane_count"])
    _, target = traffic_actor_count(density, lane_count, float(config["traffic"]["valid_window_m_each_side"]))
    if target == 0:
        return []
    by_lane = {lane: sorted((deepcopy(item) for item in master["actors"] if int(item["logical_lane_index"]) == lane), key=lambda item: (item["master_ordinal"], item["actor_id"])) for lane in range(lane_count)}
    base, remainder = divmod(target, lane_count)
    result = []
    execution_traffic=config["_execution_config"]["traffic"]
    near_threshold=float(execution_traffic["qa"]["cross_lane_near_row_threshold_m"]); coverage_min=float(execution_traffic["qa"]["longitudinal_coverage_ratio_min"]); window=2.0*float(execution_traffic["valid_window_m_each_side"]); gap_min=float(execution_traffic["qa"]["same_lane_min_gap_m"]); collision_min=float(execution_traffic["qa"]["cross_lane_collision_distance_m"])
    for lane in range(lane_count):
        count = base + (lane < remainder)
        candidates = by_lane[lane]
        if count > len(candidates):
            raise ConfigError(f"Full.1 traffic capacity insufficient for {master['anchor_id']}")
        if not count: continue
        valid=[]
        for choice in combinations(candidates,count):
            positions=sorted(float(item["longitudinal_offset_m"]) for item in choice)
            min_gap=min((right-left for left,right in zip(positions,positions[1:])),default=float("inf")); coverage=(positions[-1]-positions[0])/window if len(positions)>1 else 0.0
            if min_gap<gap_min or coverage<coverage_min: continue
            if any(math.hypot(float(item["longitudinal_offset_m"])-float(previous["longitudinal_offset_m"]),float(item["lateral_offset_m"])-float(previous["lateral_offset_m"]))<collision_min for item in choice for previous in result): continue
            near_pairs=sum(abs(float(item["longitudinal_offset_m"])-float(previous["longitudinal_offset_m"]))<=near_threshold for item in choice for previous in result)
            valid.append((near_pairs,tuple(item["actor_id"] for item in choice),choice))
        if not valid: raise ConfigError(f"Full.1 arbitrary-density lane subset has no QA-valid combination: {master['anchor_id']} lane={lane} count={count}")
        result.extend(deepcopy(item) for item in min(valid,key=lambda item:(item[0],item[1]))[2])
    return result


def _traffic_plan(config: dict[str, Any], master: dict[str, Any], recipe: dict[str, Any], subset: str) -> dict[str, Any]:
    density = float(recipe["requested_conditions"]["traffic_density_vehicles_per_lane_km"])
    lane_count = int(master["lane_count"])
    lane_km, target = traffic_actor_count(density, lane_count, float(config["traffic"]["valid_window_m_each_side"]))
    if density in {0.0, 20.0, 40.0}:
        actors = _select_density_subset(master, density, config["_execution_config"])[0]
    else:
        actors = _select_arbitrary_density(master, density, config)
    phase = recipe["requested_conditions"].get("lane_change_phase")
    if density > 0 and master["lane_change_supported"] and subset.startswith("development_"):
        changed = [item for item in actors if item["lane_change"]["selected"]]
        if not changed and actors:
            actors[0]["lane_change"]["center_transform"] = deepcopy(actors[0]["transform"])
            actors[0]["lane_change"]["center_lateral_offset_m"] = float(actors[0]["lateral_offset_m"])
            _apply_lane_change(actors[0], 1 if int(actors[0]["logical_lane_index"]) < lane_count - 1 else -1, float(phase), float(config["_execution_config"]["traffic"]["lane_width_m"]))
        else:
            for actor in changed:
                _apply_lane_change(actor, int(actor["lane_change"]["direction"]), float(phase), float(config["_execution_config"]["traffic"]["lane_width_m"]))
    changed_count = sum(bool(item["lane_change"]["selected"]) for item in actors)
    qa_actors=deepcopy(actors)
    for actor in qa_actors:
        lane_change=actor.get("lane_change",{})
        if lane_change.get("selected") and lane_change.get("center_transform"):
            actor["transform"]=deepcopy(lane_change["center_transform"]); actor["lateral_offset_m"]=float(lane_change["center_lateral_offset_m"])
    qa = _layout_qa(qa_actors,lane_count,config["_execution_config"],use_actual_geometry=False)
    if qa["status"]!="pass": raise ConfigError(f"Full.1 selected traffic subset failed offline QA: {master['anchor_id']} density={density} reasons={qa['reasons']}")
    qa.update({
        "status":"pass","reasons":[],"density":density,"planned_actor_count":len(actors),"target_actor_count":target,
        "lane_change_supported":master["lane_change_supported"],"lane_change_unsupported_reason":master["lane_change_unsupported_reason"],
        "lane_change_actor_count":changed_count,"lane_change_fraction_actual":round(changed_count / len(actors), 6) if actors else 0.0,
        "spawn_collision":{"status":"pending_carla_hard_gate","static_proxy":"pass"},
        "replay":{"status":"pending_carla_replay","master_layout_hash":master["master_layout_hash"]},
        "subset_selection":{"strict_subset":density == 20.0,"master_density":40.0,"selection_rule":"spatial_balanced_or_rank_stratified"},
    })
    replay_hash = _digest({"protocol_version":FULL_1_PROTOCOL_VERSION,"master_layout_hash":master["master_layout_hash"],"density":density,"actors":actors})
    qa["replay"]["planned_replay_hash"] = replay_hash
    return {
        "density_unit":"vehicles_per_lane_km","requested_density":density,"valid_lane_count":lane_count,
        "valid_window_m":2.0*float(config["traffic"]["valid_window_m_each_side"]),"lane_km":lane_km,
        "target_actor_count":target,"planned_actor_count":len(actors),"capacity_status":"sufficient",
        "master_layout_seed":int(master["master_layout_seed"]),"master_layout_hash":master["master_layout_hash"],
        "layout_rule_version":config["traffic"]["lane_change_rule_version"],"actors":actors,
        "nested_parent_density":40.0 if density == 20.0 else None,"planned_world_state_hash":replay_hash,"replay_hash":replay_hash,"qa":qa,
        "vehicle_pool_version":config["traffic"]["vehicle_pool_rule_version"],"vehicle_pool":list(config["traffic"]["vehicle_pool"]),
        "lane_change_supported":master["lane_change_supported"],"lane_change_unsupported_reason":master["lane_change_unsupported_reason"],
        "lane_change_actor_count":changed_count,"lane_change_phase":phase,
    }


def _row(config: dict[str, Any], subset: str, site: dict[str, Any], anchor: dict[str, Any], recipe: dict[str, Any], master: dict[str, Any], yaw_sign: int | None, asset_action: str) -> dict[str, Any]:
    condition = _execution_condition(recipe["requested_conditions"], config["_execution_config"])
    world_seed = derive_seed(int(config["seeds"]["traffic_master"]), FULL_1_RULE_VERSION, "world", anchor["anchor_id"], recipe["observation_recipe_id"])
    sensor_seed = derive_seed(int(config["seeds"]["review"]), FULL_1_RULE_VERSION, "sensor", anchor["anchor_id"], recipe["observation_recipe_id"])
    density = float(condition["traffic_density_vehicles_per_lane_km"])
    traffic = _traffic_plan(config, master, {**recipe, "requested_conditions": condition}, subset)
    sample_id = make_sample_id(site["map_id"],site["site_id"],anchor["anchor_id"],recipe["observation_recipe_id"],world_seed,sensor_seed,FULL_1_PROTOCOL_VERSION,layout_fingerprint=_digest({"master":master["master_layout_hash"],"actors":traffic["actors"]}) if density else None)
    magnitude = abs(float(condition["yaw_relative_deg"]))
    sign = 0 if magnitude == 0 else (1 if float(condition["yaw_relative_deg"]) > 0 else -1)
    return {
        "protocol_version":FULL_1_PROTOCOL_VERSION,"sample_id":sample_id,
        "display_name":make_display_name(site["map_id"],site["structural_cell"],site["site_id"],anchor["anchor_id"],recipe["recipe_family"],sample_id),
        "subset":subset,"source_subset":subset,"pilot_kind":"full_1_split_redesign","recipe_origin":"full_1_authoritative_recipe_table_0.1",
        "map_id":site["map_id"],"site_id":site["site_id"],"anchor_id":anchor["anchor_id"],"leakage_group_id":site["leakage_group_id"],
        "structural_cell":site["structural_cell"],"road_layout":site["road_layout"],"lane_configuration":site["lane_configuration"],
        "observation_recipe_id":recipe["observation_recipe_id"],"recipe_family":recipe["recipe_family"],"requested_conditions":condition,
        "factor_levels":deepcopy(recipe["factor_levels"]),"is_reference":bool(recipe["is_reference"]),"reference_sample_id":None,
        "control_bundle_id":make_control_bundle_id(anchor["anchor_id"]) if subset in {"core_test","challenge_test"} else None,
        "changed_factor":recipe["changed_factor"],"factor_level":recipe.get("factor_level"),"challenge_row_id":recipe.get("challenge_row_id"),
        "challenge_level_tuple":recipe.get("challenge_level_tuple"),"challenge_axis_id":recipe.get("challenge_axis_id"),
        "yaw_magnitude_deg":magnitude,"yaw_sign":sign,"anchor_yaw_sign":yaw_sign,
        "world_seed":int(world_seed),"sensor_seed":int(sensor_seed),"camera_geometry":_camera_geometry(anchor,condition),
        "recipe_expected_projection":_recipe_expected_projection(site,anchor,{**recipe,"requested_conditions":condition},config["_execution_config"]),
        "traffic_plan":traffic,"official_labels":list(OFFICIAL_LABELS),"qa_policy_version":config["_execution_config"]["qa"]["policy_version"],
        "development_qa_tier":"D0-strict" if subset.startswith("development_") else None,"qa_status":"pending_carla","generation_status":"planned",
        "canonical_dataset":True,"full_1_status":"planned_pending_execution","asset_action":asset_action,
        "inherited_from_sample_id":recipe.get("inherited_from_sample_id"),
        "asset_paths":{name:f"canonical/{sample_id}/{name}" for name in FULL_1_ASSETS},
        "source_uri":f"full-1-ledger://{config['release_id']}/{sample_id}",
    }


def _build_site_table(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    split = config["_split_decision"]
    scene = config["_scene_decision"]
    new_cell = {p["site_id"]: p["new_cell"] for p in scene["per_site_mapping"] if p["changed"]}
    assignment = split["site_assignment"]
    sites = config["_source_sites"]
    table: dict[str, dict[str, Any]] = {}
    for role in ACTIVE_ROLES:
        for site_id in assignment[role]:
            site = sites[site_id]
            cell = new_cell.get(site_id, site["structural_cell"])
            entry = {
                "site_id": site_id, "subset": role, "map_id": site["map_id"],
                "structural_cell": cell, "scene": CELL_TO_SCENE[cell],
                "leakage_group_id": site["leakage_group_id"],
                "anchor_ids": sorted(site.get("anchor_ids", [])),
            }
            table[site_id] = entry
    return table


def _old_index(config: dict[str, Any]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in config["_old_ledger"]:
        index[(row["subset"], row["site_id"], row["anchor_id"])].append(row)
    return index


def build_full_1_plan(config_path: Path = FULL_1_CONFIG_PATH) -> dict[str, Any]:
    config = load_full_1_config(config_path)
    site_table = _build_site_table(config)
    old_index = _old_index(config)
    old_by_site_anchor: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in config["_old_ledger"]:
        old_by_site_anchor[(row["site_id"], row["anchor_id"])].append(row)

    rows: list[dict[str, Any]] = []
    masters: dict[str, dict[str, Any]] = {}
    reuse_rows = 0
    recapture_rows = 0
    dev_ranks: dict[str, dict[str, int]] = {}
    for subset in ("development_train", "development_val"):
        anchors = []
        for entry in site_table.values():
            if entry["subset"] != subset:
                continue
            for aid in entry["anchor_ids"]:
                anchors.append((entry, config["_source_anchors"][aid]))
        anchors.sort(key=lambda pair: (_development_forward_capacity_m(config["_source_sites"][pair[0]["site_id"]], pair[1]), pair[1]["anchor_id"]))
        dev_ranks[subset] = {a["anchor_id"]: rank for rank, (_, a) in enumerate(anchors)}

    # yaw signs: inherit for old Core/Challenge anchors, else assign
    old_yaw = {}
    for row in config["_old_ledger"]:
        if row["subset"] in {"core_test", "challenge_test"} and row.get("anchor_yaw_sign") is not None:
            old_yaw[row["anchor_id"]] = int(row["anchor_yaw_sign"])

    test_anchors = []
    chal_ranks: dict[str, int] = {}
    for subset in ("core_test", "challenge_test"):
        seen = set()
        for entry in site_table.values():
            if entry["subset"] != subset:
                continue
            for aid in entry["anchor_ids"]:
                if aid not in seen:
                    seen.add(aid)
                    test_anchors.append((entry, config["_source_anchors"][aid]))
    test_anchors.sort(key=lambda pair: (pair[1]["anchor_id"]))
    for subset in ("core_test", "challenge_test"):
        sub = [pair for pair in test_anchors if pair[0]["subset"] == subset]
        sub.sort(key=lambda pair: pair[1]["anchor_id"])
        if subset == "challenge_test":
            chal_ranks = {a["anchor_id"]: rank for rank, (_, a) in enumerate(sub)}
    yaw_assignment = _yaw_assignments(config, test_anchors, old_yaw)

    for subset in SUBSETS:
        for entry in site_table.values():
            if entry["subset"] != subset:
                continue
            site = deepcopy(config["_source_sites"][entry["site_id"]])
            site["structural_cell"] = entry["structural_cell"]
            for aid in entry["anchor_ids"]:
                anchor = config["_source_anchors"][aid]
                old_rows = old_index.get((subset, entry["site_id"], aid), [])
                expected = int(config["selection"]["development_recipes_per_anchor"]) if subset.startswith("development_") else (16 if subset == "core_test" else 25)
                if len(old_rows) == expected:
                    recipes = _inherit_recipes_from_old(old_rows)
                    asset_action = "reuse_candidate_requires_semantic_fingerprint_match"
                else:
                    if subset.startswith("development_"):
                        recipes = _new_development_recipes(config, anchor, subset, dev_ranks[subset][aid], len(dev_ranks[subset]))
                        sign = None
                    elif subset == "core_test":
                        sign = yaw_assignment[aid]
                        recipes = _core_recipes(config, anchor, sign)
                    else:
                        sign = yaw_assignment[aid]
                        recipes = _challenge_recipes(config, anchor, sign, chal_ranks[aid], len(chal_ranks))
                    asset_action = "incremental_recapture_for_new_subset_recipe"
                master = _vehicle_pool_and_lane_change(config, site, anchor, _generate_master_layout(site, anchor, config["_execution_config"]))
                masters[aid] = master
                bundle = [_row(config, subset, site, anchor, recipe, master, yaw_assignment.get(aid), asset_action) for recipe in recipes]
                references = [item for item in bundle if item["is_reference"]]
                reference_id = references[0]["sample_id"] if references else None
                for item in bundle:
                    item["reference_sample_id"] = reference_id
                rows.extend(bundle)
                if asset_action.startswith("reuse"):
                    reuse_rows += len(bundle)
                else:
                    recapture_rows += len(bundle)

    rows.sort(key=lambda item: (MAP_IDS.index(item["map_id"]), SUBSETS.index(item["subset"]), item["site_id"], item["anchor_id"], item["sample_id"]))
    gates = _validate_plan(config, rows)
    subset_counts = {}
    for subset in SUBSETS:
        sub = [row for row in rows if row["subset"] == subset]
        subset_counts[subset] = {"images": len(sub), "anchors": len({row["anchor_id"] for row in sub}), "sites": len({row["site_id"] for row in sub}), "leakage_groups": len({row["leakage_group_id"] for row in sub})}
    plan = {
        "schema_version": "benchmark_v3_full_1_plan_0.1", "protocol_version": FULL_1_PROTOCOL_VERSION,
        "stage": "v3_full_1_static", "release_id": config["release_id"], "rule_version": FULL_1_RULE_VERSION,
        "expected_image_count": len(rows), "subset_counts": subset_counts,
        "maps": dict(sorted(Counter(row["map_id"] for row in rows).items())),
        "structural_cells": dict(sorted(Counter(row["structural_cell"] for row in rows).items())),
        "gates": gates, "official_labels": list(OFFICIAL_LABELS),
        "source_checksums": dict(sorted(config["_source_checksums"].items())),
        "asset_actions": {"reuse_candidate": reuse_rows, "incremental_recapture": recapture_rows},
    }
    return {"config": config, "execution_config": config["_execution_config"], "site_table": site_table,
            "rows": rows, "masters": masters, "plan": plan, "yaw_assignments": yaw_assignment}


def _yaw_assignments(config: dict[str, Any], anchors: list[tuple[dict[str, Any], dict[str, Any]]], inherited: dict[str, int]) -> dict[str, int]:
    seed = int(config["seeds"]["yaw_sign"])
    ordered = sorted(
        ((entry, anchor) for entry, anchor in anchors if anchor["anchor_id"] not in inherited),
        key=lambda pair: pair[1]["anchor_id"],
    )
    assignment = dict(inherited)
    global_counts = Counter(assignment.values())
    bucket_counts: dict[tuple[str, str], Counter[int]] = defaultdict(Counter)
    for aid, sign in assignment.items():
        bucket = None
        for entry, anchor in anchors:
            if anchor["anchor_id"] == aid:
                bucket = (entry["map_id"], entry["structural_cell"])
                break
        if bucket:
            bucket_counts[bucket][sign] += 1
    for entry, anchor in ordered:
        bucket = (entry["map_id"], entry["structural_cell"])
        scores = {}
        for sign in (-1, 1):
            next_bucket = bucket_counts[bucket].copy()
            next_bucket[sign] += 1
            next_global = global_counts.copy()
            next_global[sign] += 1
            scores[sign] = (
                abs(next_bucket[1] - next_bucket[-1]),
                abs(next_global[1] - next_global[-1]),
                derive_seed(seed, bucket, anchor["anchor_id"], sign),
            )
        sign = min(scores, key=scores.get)
        assignment[anchor["anchor_id"]] = sign
        bucket_counts[bucket][sign] += 1
        global_counts[sign] += 1
    return assignment


def _validate_plan(config: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    expected = config["selection"]["quotas"]
    counts = Counter(row["subset"] for row in rows)
    if {k: v["images"] for k, v in expected.items()} != dict(counts) or len(rows) != int(config["target_images"]):
        raise ConfigError(f"Full.1 quantity gate failed: {counts}")
    if len({row["sample_id"] for row in rows}) != len(rows):
        raise ConfigError("Full.1 sample ID collision")
    group_subsets: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        group_subsets[row["leakage_group_id"]].add(row["subset"])
        if row["official_labels"] != list(OFFICIAL_LABELS):
            raise ConfigError("Full.1 official label drift")
    if any(len(values) != 1 for values in group_subsets.values()):
        raise ConfigError("Full.1 active leakage group crosses subset")
    for subset in ("development_train", "development_val"):
        sub = [row for row in rows if row["subset"] == subset]
        zero = sum(float(row["traffic_plan"]["requested_density"]) == 0 for row in sub)
        if zero * 2 != len(sub):
            raise ConfigError(f"Full.1 Development 50/50 failed for {subset}")
        per_anchor = Counter(row["anchor_id"] for row in sub)
        if any(v != 48 for v in per_anchor.values()):
            raise ConfigError(f"Full.1 Development per-anchor rows drifted for {subset}")
    bundle_sizes = {"core_test": 16, "challenge_test": 25}
    for subset, size in bundle_sizes.items():
        grouped = Counter(row["anchor_id"] for row in rows if row["subset"] == subset)
        if set(grouped.values()) != {size}:
            raise ConfigError(f"Full.1 {subset} bundle incomplete")
    axes = Counter(row["challenge_axis_id"] for row in rows if row["subset"] == "challenge_test" and row["challenge_axis_id"])
    if set(axes) != set(config["challenge_axis"]["axis_ids"]):
        raise ConfigError("Full.1 Challenge Axis coverage failed")
    maps_by_subset = {subset: sorted({row["map_id"] for row in rows if row["subset"] == subset}) for subset in SUBSETS}
    if any(values != list(MAP_IDS) for values in maps_by_subset.values()):
        raise ConfigError(f"Full.1 map coverage failed: {maps_by_subset}")
    # old sample IDs must not reappear
    old_ids = {row["sample_id"] for row in config["_old_ledger"]}
    collision = old_ids & {row["sample_id"] for row in rows}
    if collision:
        raise ConfigError(f"Full.1 sample IDs collide with old release: {list(collision)[:3]}")
    return {
        "status": "pass", "quantity": len(rows), "development_50_50": "pass",
        "split_leakage": "pass", "sample_id_uniqueness": "pass", "old_sample_id_collision": "none",
        "core_bundle": "complete_16_each", "challenge_bundle": "complete_25_each",
        "challenge_axis": "all_7_each", "maps_by_subset": maps_by_subset,
    }


__all__ = [
    "FULL_1_ASSETS", "FULL_1_CONFIG_PATH", "FULL_1_EXECUTION_ROOT", "FULL_1_PROTOCOL_VERSION",
    "FULL_1_REVIEW_ROOT", "FULL_1_STATIC_ROOT", "MAP_IDS", "SUBSETS",
    "build_full_1_plan", "load_full_1_config",
]
