"""Confirmed CARLA map/shard worker for the frozen Benchmark-V3 full-regeneration ledger.

Every row is a new capture (no reuse); rows on the LABELFIX.1-confirmed
sites are generated through the versioned V3 physical-marking merge branch
of ``capture._generate_one_r4_sample``.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

from benchmark_generator.common.io import append_jsonl, utc_now, write_json
from benchmark_generator.config import ConfigError, ensure_path_within, load_json
from benchmark_generator.v3.constants import V3_SCHEMA_ROOT
from benchmark_generator.v3.plan import (
    CAPTURE_ROOT,
    PROTOCOL_VERSION,
    LABEL_RULE_DEFAULT,
)
from benchmark_generator.v3.execution import load_execution_bundle
from benchmark_generator.v3.label_merge import RULE_VERSION as LABEL_FIX_RULE_VERSION
from benchmark_generator.v3.schema_support import validate_schema_instance


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Internal Benchmark-V3 CARLA worker (full regeneration only)")
    parser.add_argument("--static-run", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--map-id", required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--result-path", type=Path, required=True)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tm-port", type=int, required=True)
    return parser.parse_args(argv)


def _prior_attempt_counts(path: Path) -> dict[str, int]:
    counts = {}
    if not path.is_file():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            sample_id = json.loads(line).get("sample_id")
            if sample_id:
                counts[sample_id] = counts.get(sample_id, 0) + 1
    return counts


def _blueprint_preflight(blueprints: Any, pool: list[str]) -> None:
    missing = []
    for blueprint_id in pool:
        try:
            blueprints.find(blueprint_id)
        except BaseException:
            missing.append(blueprint_id)
    if missing:
        raise ConfigError(f"Benchmark-V3 frozen vehicle blueprints unavailable: {missing}")


def _waypoint_key(waypoint: Any) -> tuple[int, int, int, float, float, float]:
    return (
        int(waypoint.road_id), int(waypoint.section_id), int(waypoint.lane_id), round(float(waypoint.s), 6),
        round(float(waypoint.transform.location.x), 6), round(float(waypoint.transform.location.y), 6),
    )


def _nearby_waypoints(start: Any, radius_m: float, step_m: float) -> list[tuple[float, Any]]:
    candidates = [(0.0, start)]
    steps = max(0, int(math.floor(radius_m / step_m))) if step_m > 0 else 0
    for index in range(1, steps + 1):
        distance = index * step_m
        candidates.extend((distance, item) for item in start.next(distance))
        candidates.extend((distance, item) for item in start.previous(distance))
    dedupe = {}
    for displacement, waypoint in candidates:
        key = _waypoint_key(waypoint)
        current = dedupe.get(key)
        if current is None or displacement < current[0]:
            dedupe[key] = (displacement, waypoint)
    return sorted(dedupe.values(), key=lambda item: (item[0], _waypoint_key(item[1])))


def _route_waypoints(start: Any, offset_m: float, aligned: bool, radius_m: float, step_m: float) -> list[tuple[float, Any]]:
    use_next = (offset_m > 0) == aligned
    desired = abs(offset_m)
    distances = [desired]
    steps = max(0, int(math.floor(radius_m / step_m))) if step_m > 0 else 0
    for index in range(1, steps + 1):
        delta = index * step_m
        distances.append(desired + delta)
        if desired - delta >= 0:
            distances.append(desired - delta)
    candidates = []
    for distance in distances:
        values = [start] if distance < 1e-6 else (start.next(distance) if use_next else start.previous(distance))
        candidates.extend((abs(distance - desired), item) for item in values)
    dedupe = {}
    for displacement, waypoint in candidates:
        key = _waypoint_key(waypoint)
        current = dedupe.get(key)
        if current is None or displacement < current[0]:
            dedupe[key] = (displacement, waypoint)
    return sorted(dedupe.values(), key=lambda item: (item[0], _waypoint_key(item[1])))


def _resolved_transform(waypoint: Any, logical_lane: int, clearance_m: float) -> dict[str, Any]:
    return {
        "frame": "carla_unreal_frozen_waypoint_r4_v2",
        "location": {"x": round(float(waypoint.transform.location.x), 6), "y": round(float(waypoint.transform.location.y), 6), "z": round(float(waypoint.transform.location.z) + clearance_m, 6)},
        "rotation": {"pitch": round(float(waypoint.transform.rotation.pitch), 6), "yaw": round(float(waypoint.transform.rotation.yaw), 6), "roll": round(float(waypoint.transform.rotation.roll), 6)},
        "logical_lane_index": logical_lane,
    }


def _center_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    return math.hypot(float(left["location"]["x"]) - float(right["location"]["x"]), float(left["location"]["y"]) - float(right["location"]["y"]))


def _resolve_runtime_traffic(carla: Any, carla_map: Any, waypoint_cache: list[Any], row: dict[str, Any], site: dict[str, Any], anchor: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = deepcopy(row["traffic_plan"])
    audits = []
    traffic_config = config["traffic"]
    plan["staged_spawn_base_height_m"] = float(traffic_config.get("runtime_staged_spawn_base_height_m", 100.0))
    plan["staged_spawn_vertical_step_m"] = float(traffic_config.get("runtime_staged_spawn_vertical_step_m", 5.0))
    proxy_threshold = float(traffic_config["runtime_static_proxy_deviation_record_threshold_m"])
    clearance = float(traffic_config["runtime_vehicle_ground_clearance_m"])
    route_radius = float(traffic_config.get("runtime_route_search_radius_m", 12.0))
    route_step = float(traffic_config.get("runtime_route_search_step_m", 2.0))
    center_clearance = float(traffic_config.get("runtime_min_center_clearance_m", 3.0))
    lane_change_radius = float(traffic_config.get("runtime_lane_change_search_radius_m", 100.0))
    offline_selected = [actor["actor_id"] for actor in plan["actors"] if actor.get("lane_change", {}).get("selected")]
    locator = anchor["locator"]
    road_id = int(locator["road_id"])
    section_id = int(locator["section_id"])
    anchor_s = float(locator["s_m"])
    lane_ids = sorted({int(item.lane_id) for item in waypoint_cache if int(item.road_id) == road_id and int(item.section_id) == section_id and item.lane_type == carla.LaneType.Driving})
    anchor_waypoints = [carla_map.get_waypoint_xodr(road_id, lane_id, anchor_s) for lane_id in lane_ids]
    anchor_waypoints = [item for item in anchor_waypoints if item is not None and str(item.lane_type).lower().endswith("driving")]
    lane_count = int(anchor["lane_count_at_anchor"])
    if len(anchor_waypoints) != lane_count:
        raise ConfigError(f"Benchmark-V3 anchor Driving-lane count drifted: {row['sample_id']} expected={lane_count} actual={len(anchor_waypoints)}")
    anchor_yaw = math.radians(-float(anchor["pose_world"]["heading_deg"]))
    left = (-math.sin(anchor_yaw), math.cos(anchor_yaw))
    origin = (float(anchor["pose_world"]["x"]), -float(anchor["pose_world"]["y"]))
    anchor_waypoints.sort(key=lambda item: (float(item.transform.location.x) - origin[0]) * left[0] + (float(item.transform.location.y) - origin[1]) * left[1])
    occupied = []
    for actor in plan["actors"]:
        lane_change = actor.get("lane_change", {})
        static_center = deepcopy(lane_change.get("center_transform") or actor["transform"])
        location = static_center["location"]
        logical_lane = int(actor["logical_lane_index"])
        if not 0 <= logical_lane < len(anchor_waypoints):
            raise ConfigError(f"Benchmark-V3 logical lane index out of range: {row['sample_id']} {actor['actor_id']}")
        start = anchor_waypoints[logical_lane]
        offset = float(actor["longitudinal_offset_m"])
        yaw_delta = math.radians(float(start.transform.rotation.yaw)) - anchor_yaw
        aligned = math.cos(yaw_delta) >= 0.0
        candidates = _route_waypoints(start, offset, aligned, route_radius, route_step)
        candidates = [item for item in candidates if str(item[1].lane_type).lower().endswith("driving")]
        if not candidates:
            raise ConfigError(f"Benchmark-V3 anchor lane route has no endpoint: {row['sample_id']} {actor['actor_id']} offset={offset}")
        ranked = []
        for displacement, option in candidates:
            transform = _resolved_transform(option, logical_lane, clearance)
            minimum = min((_center_distance(transform, previous) for previous in occupied), default=float("inf"))
            static_distance = math.hypot(float(option.transform.location.x) - float(location["x"]), float(option.transform.location.y) - float(location["y"]))
            ranked.append((minimum < center_clearance, displacement, static_distance, _waypoint_key(option), option, transform, minimum))
        _, route_adjustment, _, _, waypoint, resolved, minimum_clearance = min(ranked)
        if minimum_clearance < center_clearance:
            raise ConfigError(f"Benchmark-V3 runtime route center clearance insufficient: {row['sample_id']} {actor['actor_id']} minimum={minimum_clearance:.6f}")
        if waypoint is None or not str(waypoint.lane_type).lower().endswith("driving"):
            raise ConfigError(f"Benchmark-V3 runtime topology resolution has no Driving waypoint: {row['sample_id']} {actor['actor_id']}")
        snap = math.hypot(float(waypoint.transform.location.x) - float(location["x"]), float(waypoint.transform.location.y) - float(location["y"]))
        actor.update({"lane_id": str(int(waypoint.lane_id)), "frozen_lane_id": int(waypoint.lane_id), "waypoint_road_id": int(waypoint.road_id), "waypoint_section_id": int(waypoint.section_id), "waypoint_s_m": round(float(waypoint.s), 6)})
        actor["transform"] = deepcopy(resolved)
        actor["lane_change"] = {"selected": False, "phase": None, "direction": 0, "runtime_recheck": "not_selected_after_carla_resolution", "center_transform": deepcopy(resolved), "offline_selected": bool(lane_change.get("selected"))}
        audit = {"actor_id": actor["actor_id"], "status": "pass", "source": "anchor_lane_waypoint_route", "static_center_transform": static_center, "resolved_center_transform": resolved, "center_snap_distance_m": round(snap, 6), "route_adjustment_m": round(route_adjustment, 6), "minimum_prior_center_clearance_m": None if math.isinf(minimum_clearance) else round(minimum_clearance, 6), "road_id": int(waypoint.road_id), "section_id": int(waypoint.section_id), "lane_id": int(waypoint.lane_id), "s_m": round(float(waypoint.s), 6), "is_junction": bool(waypoint.is_junction)}
        actor["runtime_topology_resolution"] = audit
        audits.append(audit)
        occupied.append(resolved)
    requested_lane_changes = int(plan.get("lane_change_actor_count", 0))
    runtime_selected = []
    phase = float(plan.get("lane_change_phase") or 0.5)
    smooth = 3.0 * phase ** 2 - 2.0 * phase ** 3
    lane_width = float(config["_execution_config"]["traffic"]["lane_width_m"])
    shift_magnitude = lane_width * smooth
    yaw_magnitude = 8.0 * math.sin(math.pi * phase)
    min_clearance = float(config["traffic"]["adjacent_actor_clearance_m"])
    options = []
    for actor in plan["actors"]:
        waypoint = carla_map.get_waypoint_xodr(int(actor["waypoint_road_id"]), int(actor["frozen_lane_id"]), float(actor["waypoint_s_m"]))
        if waypoint is None:
            continue
        for displacement, candidate in _nearby_waypoints(waypoint, lane_change_radius, route_step):
            if bool(candidate.is_junction) or not str(candidate.lane_type).lower().endswith("driving"):
                continue
            permission = str(getattr(candidate, "lane_change", "None")).lower()
            for direction, getter, name in ((1, "get_left_lane", "left"), (-1, "get_right_lane", "right")):
                adjacent = getattr(candidate, getter)()
                allowed = permission.endswith("both") or permission.endswith(name)
                if adjacent is None or not allowed or bool(adjacent.is_junction) or not str(adjacent.lane_type).lower().endswith("driving"):
                    continue
                center = _resolved_transform(candidate, int(actor["logical_lane_index"]), clearance)
                yaw = math.radians(float(center["rotation"]["yaw"]))
                left = (-math.sin(yaw), math.cos(yaw))
                shifted = (float(center["location"]["x"]) + left[0] * direction * shift_magnitude, float(center["location"]["y"]) + left[1] * direction * shift_magnitude)
                other_distance = min((math.hypot(shifted[0] - float(other["transform"]["location"]["x"]), shifted[1] - float(other["transform"]["location"]["y"])) for other in plan["actors"] if other is not actor), default=float("inf"))
                if other_distance >= min_clearance:
                    options.append((actor["actor_id"] not in offline_selected, displacement, actor["actor_id"], direction, actor, candidate, center, other_distance))
                    break
    selected_ids = set()
    for _, displacement, _, direction, actor, waypoint, center, _ in sorted(options, key=lambda item: (item[0], item[1], item[2], item[3])):
        if len(runtime_selected) >= requested_lane_changes:
            break
        if actor["actor_id"] in selected_ids:
            continue
        center = deepcopy(center)
        yaw = math.radians(float(center["rotation"]["yaw"]))
        left = (-math.sin(yaw), math.cos(yaw))
        shift = direction * shift_magnitude
        yaw_offset = direction * yaw_magnitude
        shifted = (float(center["location"]["x"]) + left[0] * shift, float(center["location"]["y"]) + left[1] * shift)
        other_distance = min((math.hypot(shifted[0] - float(other["transform"]["location"]["x"]), shifted[1] - float(other["transform"]["location"]["y"])) for other in plan["actors"] if other is not actor), default=float("inf"))
        if other_distance < min_clearance:
            continue
        actor["lane_change"] = {"selected": True, "phase": round(phase, 6), "direction": direction, "lateral_shift_m": round(shift, 6), "yaw_offset_deg": round(yaw_offset, 6), "runtime_recheck": "pending_carla", "center_transform": center, "offline_selected": actor["actor_id"] in offline_selected, "selection_clearance_m": round(other_distance, 6)}
        actor["transform"] = deepcopy(center)
        actor["transform"]["location"]["x"] = round(float(center["location"]["x"]) + left[0] * shift, 6)
        actor["transform"]["location"]["y"] = round(float(center["location"]["y"]) + left[1] * shift, 6)
        actor["transform"]["rotation"]["yaw"] = round(float(center["rotation"]["yaw"]) + yaw_offset, 6)
        actor["lateral_offset_m"] = round(float(actor["lateral_offset_m"]) + shift, 6)
        actor.update({"lane_id": str(int(waypoint.lane_id)), "frozen_lane_id": int(waypoint.lane_id), "waypoint_road_id": int(waypoint.road_id), "waypoint_section_id": int(waypoint.section_id), "waypoint_s_m": round(float(waypoint.s), 6)})
        actor["runtime_topology_resolution"].update({"resolved_center_transform": deepcopy(center), "lane_change_route_adjustment_m": round(displacement, 6), "road_id": int(waypoint.road_id), "section_id": int(waypoint.section_id), "lane_id": int(waypoint.lane_id), "s_m": round(float(waypoint.s), 6)})
        runtime_selected.append(actor["actor_id"])
        selected_ids.add(actor["actor_id"])
    if len(runtime_selected) != requested_lane_changes:
        raise ConfigError(f"Benchmark-V3 runtime legal lane-change capacity insufficient: {row['sample_id']} requested={requested_lane_changes} selected={len(runtime_selected)}")
    plan["lane_change_actor_count"] = len(runtime_selected)
    plan["qa"]["lane_change_actor_count"] = len(runtime_selected)
    plan["qa"]["lane_change_fraction_actual"] = round(len(runtime_selected) / len(plan["actors"]), 6) if plan["actors"] else 0.0
    replay_payload = {"protocol_version": PROTOCOL_VERSION, "master_layout_hash": plan["master_layout_hash"], "density": plan["requested_density"], "actors": plan["actors"]}
    replay_hash = hashlib.sha256(json.dumps(replay_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    plan["planned_world_state_hash"] = replay_hash
    plan["replay_hash"] = replay_hash
    plan["qa"]["replay"]["planned_replay_hash"] = replay_hash
    audit = {"status": "pass", "rule_version": config["traffic"]["runtime_topology_resolution_rule_version"], "static_proxy_deviation_record_threshold_m": proxy_threshold, "static_proxy_deviation_over_threshold_count": sum(item["center_snap_distance_m"] > proxy_threshold for item in audits), "vehicle_ground_clearance_m": clearance, "actor_count": len(audits), "max_observed_center_snap_distance_m": max((item["center_snap_distance_m"] for item in audits), default=0.0), "offline_lane_change_actor_ids": offline_selected, "runtime_lane_change_actor_ids": runtime_selected, "lane_change_actor_count": len(runtime_selected), "actors": audits, "sample_identity": "frozen_static_id_preserved_runtime_resolution_recorded"}
    return plan, audit


def _runtime_lane_change_audit(carla: Any, carla_map: Any, row: dict[str, Any]) -> dict[str, Any]:
    audits = []
    for actor in row["traffic_plan"]["actors"]:
        lane_change = actor.get("lane_change", {})
        if not lane_change.get("selected"):
            continue
        location = (lane_change.get("center_transform") or actor["transform"])["location"]
        waypoint = carla_map.get_waypoint(carla.Location(x=float(location["x"]), y=float(location["y"]), z=float(location["z"])), project_to_road=True, lane_type=carla.LaneType.Driving)
        if waypoint is None or bool(waypoint.is_junction):
            raise ConfigError(f"Benchmark-V3 lane-change runtime waypoint invalid/junction: {row['sample_id']} {actor['actor_id']}")
        direction = int(lane_change["direction"])
        getter = "get_left_lane" if direction > 0 else "get_right_lane"
        side = "left" if direction > 0 else "right"
        candidate = getattr(waypoint, getter)()
        adjacent = [] if candidate is None else [candidate]
        permission = str(getattr(waypoint, "lane_change", "None"))
        permitted = permission.lower().endswith("both") or permission.lower().endswith(side)
        if not adjacent or not permitted or not str(candidate.lane_type).lower().endswith("driving") or bool(candidate.is_junction):
            raise ConfigError(f"Benchmark-V3 lane-change runtime adjacent lane/marking not permitted: {row['sample_id']} {actor['actor_id']} {permission}")
        audits.append({"actor_id": actor["actor_id"], "status": "pass", "road_id": int(waypoint.road_id), "section_id": int(waypoint.section_id), "lane_id": int(waypoint.lane_id), "is_junction": False, "adjacent_driving_lane_count": len(adjacent), "lane_change_permission": permission})
    return {"status": "pass", "checked_actor_count": len(audits), "actors": audits, "rule_version": "benchmark_v3_runtime_lane_change_recheck_1.0"}


def _benchmark_v3_metadata(metadata: dict[str, Any], row: dict[str, Any], run_id: str, lane_change_audit: dict[str, Any], topology_resolution: dict[str, Any], config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    value = deepcopy(metadata)
    value.update({
        "schema_version": "benchmark_v3_sample_metadata_1.0",
        "protocol_version": PROTOCOL_VERSION,
        "physical_subset": row["physical_subset"], "subset": row["physical_subset"], "display_name": row["display_name"],
        "control_bundle_id": row["control_bundle_id"], "reference_sample_id": row["reference_sample_id"],
        "changed_factor": row["changed_factor"], "factor_level": row["factor_level"],
        "factor_levels": row["factor_levels"], "challenge_row_id": row["challenge_row_id"],
        "challenge_level_tuple": row["challenge_level_tuple"], "challenge_axis_id": row["challenge_axis_id"],
        "yaw_magnitude_deg": row["yaw_magnitude_deg"], "yaw_sign": row["yaw_sign"],
        "development_qa_tier": row["development_qa_tier"], "canonical_dataset": True,
        "benchmark_v3_status": "accepted", "generation_status": "new_capture",
        "label_rule_version": row["label_rule_version"],
        "source_uri": f"full-3://{run_id}/canonical/{row['sample_id']}",
    })
    geometry = value["qa"]["strict_geometry"]
    if geometry.get("status") != "pass":
        raise ConfigError(f"Benchmark-V3 D0/strict geometry rejected {row['sample_id']}: {geometry}")
    exposure = value["qa"]["exposure"]
    exposure_warn = exposure.get("status") == "warn"
    value["qa"].update({"status": "accepted", "development_qa_tier": row["development_qa_tier"], "warn_unresolved": 0, "warn_adjudication": {"exposure_warn_present": exposure_warn, "disposition": "accepted_by_frozen_histogram_exposure_warn_only_policy" if exposure_warn else "not_applicable", "reviewer_kind": "protocol_policy_automatic", "human_review_claimed": False}, "lane_change_runtime": lane_change_audit})
    value["traffic"].update({"vehicle_pool_version": row["traffic_plan"]["vehicle_pool_version"], "vehicle_pool": row["traffic_plan"]["vehicle_pool"], "lane_change_supported": row["traffic_plan"]["lane_change_supported"], "lane_change_unsupported_reason": row["traffic_plan"]["lane_change_unsupported_reason"], "lane_change_actor_count": topology_resolution["lane_change_actor_count"], "lane_change_phase": row["traffic_plan"]["lane_change_phase"], "lane_change_runtime_audit": lane_change_audit, "runtime_topology_resolution": topology_resolution})
    value["generation_runtime"] = {
        "carla_python": config["runtime"]["carla_python"], "carla_root": config["runtime"]["carla_root"], "carla_executable": config["runtime"]["carla_executable"],
        "host": args.host, "rpc_port": int(args.port), "traffic_manager_port": int(args.tm_port), "graphics_adapter": int(config["lifecycle"]["graphics_adapter"]),
        "map_id": row["map_id"], "world_seed": int(row["world_seed"]), "sensor_seed": int(row["sensor_seed"]), "frozen_seeds": deepcopy(config["seeds"]),
        "camera": deepcopy(row["requested_conditions"]), "official_labels": deepcopy(row["official_labels"]), "worker_command": list(sys.argv),
    }
    prefix = f"canonical/{row['sample_id']}"
    for name, asset in value["assets"].items():
        asset["path"] = f"{prefix}/{name}"
    value["labels"]["road_mask_visible"]["path"] = f"{prefix}/road_mask_visible.png"
    lane = value["labels"]["lane_polyline_projected"]
    if row["v3_physical_marking_merge"]:
        if lane.get("rule_version") != LABEL_FIX_RULE_VERSION:
            raise ConfigError(f"Benchmark-V3 merge-row label rule missing for {row['sample_id']}")
        if "physical_marking_merge_provenance" not in lane:
            raise ConfigError(f"Benchmark-V3 merge-row provenance missing for {row['sample_id']}")
        if len(lane["instances"]) != int(lane.get("expected_physical_marking_count", -1)):
            raise ConfigError(f"Benchmark-V3 merge-row instance count mismatch for {row['sample_id']}")
    elif lane.get("rule_version") is not None:
        raise ConfigError(f"Benchmark-V3 default-row unexpectedly carries the merge rule for {row['sample_id']}")
    else:
        lane["rule_version"] = LABEL_RULE_DEFAULT
    validate_schema_instance(value, load_json(V3_SCHEMA_ROOT / "sample_metadata.schema.json"), f"Benchmark-V3 metadata {row['sample_id']}")
    return value


def run_worker(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_execution_bundle(args.static_run)
    config = bundle["config"]
    execution = bundle["execution_config"]
    if args.host != config["lifecycle"]["host"] or int(args.port) != int(config["lifecycle"]["rpc_port"]) or int(args.tm_port) != int(config["lifecycle"]["traffic_manager_port"]):
        raise ConfigError("Benchmark-V3 worker host/RPC/TM port drifted")
    if int(args.shard_count) != int(config["sharding"]["shard_count"]):
        raise ConfigError("Benchmark-V3 worker shard count drifted")
    run_dir = ensure_path_within(args.run_dir, [CAPTURE_ROOT], "Benchmark-V3 worker run")
    manifest = load_json(run_dir / "run_manifest.json")
    if manifest.get("run_id") != args.run_id or manifest.get("static_run") != str(bundle["static_run"]):
        raise ConfigError("Benchmark-V3 worker run/static identity drifted")
    result_path = ensure_path_within(args.result_path, [run_dir / "_workers"], "Benchmark-V3 worker result")
    if result_path.exists():
        raise ConfigError(f"Benchmark-V3 worker result exists: {result_path}")
    map_rows = [row for row in bundle["rows"] if row["map_id"] == args.map_id]
    rows = [row for index, row in enumerate(sorted(map_rows, key=lambda item: item["sample_id"])) if index % int(args.shard_count) == int(args.shard_index)]
    import carla  # type: ignore
    from benchmark_generator.carla_client import carla_helpers as helpers
    from benchmark_generator.carla_client import v3_preview_worker as preview_helpers
    from benchmark_generator.carla_client.capture import _generate_one_r4_sample
    client = carla.Client(args.host, int(args.port))
    client.set_timeout(30.0)
    world = client.get_world()
    if str(world.get_map().name).split("/")[-1] != args.map_id:
        raise ConfigError(f"Benchmark-V3 worker refuses map loading: {args.map_id}")
    carla_map = world.get_map()
    waypoint_cache = carla_map.generate_waypoints(2.0)
    blueprints = world.get_blueprint_library()
    _blueprint_preflight(blueprints, config["traffic"]["vehicle_pool"])
    old_settings = world.get_settings()
    old_weather = world.get_weather()
    settings = world.get_settings()
    settings.synchronous_mode = True
    settings.fixed_delta_seconds = float(execution["camera"]["fixed_delta_seconds"])
    world.apply_settings(settings)
    accepted = []
    rejected = []
    retried_count = 0
    prior = _prior_attempt_counts(run_dir / "failures.jsonl")
    attempts_max = int(config["sharding"]["sample_attempts_max"])
    try:
        for row in rows:
            destination = run_dir / "canonical" / row["sample_id"]
            if destination.is_dir():
                raise ConfigError(f"Benchmark-V3 never reuses assets but found existing sample: {row['sample_id']}")
            used = int(prior.get(row["sample_id"], 0))
            if used >= attempts_max:
                rejected.append(row["sample_id"])
                continue
            last_error = None
            for attempt in range(used + 1, attempts_max + 1):
                staging = run_dir / "_staging" / f"{row['sample_id']}_attempt{attempt}"
                suffix = 0
                while staging.exists():
                    suffix += 1
                    staging = run_dir / "_staging" / f"{row['sample_id']}_attempt{attempt}_{suffix}"
                staging.mkdir(parents=False)
                if attempt > 1:
                    retried_count += 1
                    append_jsonl(run_dir / "retries.jsonl", {"sample_id": row["sample_id"], "attempt": attempt, "timestamp": utc_now(), "same_identity": True, "same_recipe": True})
                generation_row = deepcopy(row)
                generation_row["subset"] = "corrective_pilot"
                generation_row["_static_artifact_root"] = str(bundle["static_run"])
                # The r4 worker keys its nominal-traffic and replay gates on
                # the RC-v2 protocol version (same pattern as Full.2).  The
                # V3 merge branch engages through the frozen ledger flag
                # below, never through this protocol key.
                generation_row["protocol_version"] = "V3-PLAN-20260731-RC-V2.0"
                generation_row["_v3_merge_rule"] = bool(row["v3_physical_marking_merge"])
                try:
                    generation_row["traffic_plan"], topology_resolution = _resolve_runtime_traffic(carla, carla_map, waypoint_cache, row, config["_source_sites"][row["site_id"]], config["_source_anchors"][row["anchor_id"]], config)
                    lane_change_audit = _runtime_lane_change_audit(carla, carla_map, generation_row)
                    metadata = _generate_one_r4_sample(carla, helpers, preview_helpers, world, carla_map, blueprints, waypoint_cache, execution, generation_row, config["_source_sites"][row["site_id"]], config["_source_anchors"][row["anchor_id"]], staging, args)
                    metadata = _benchmark_v3_metadata(metadata, row, args.run_id, lane_change_audit, topology_resolution, config, args)
                    write_json(staging / "metadata.json", metadata)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staging, destination)
                    accepted.append(row["sample_id"])
                    append_jsonl(run_dir / "results.jsonl", {"sample_id": row["sample_id"], "subset": row["physical_subset"], "map_id": row["map_id"], "shard_index": args.shard_index, "status": "accepted_full_regeneration", "attempt": attempt, "qa": metadata["qa"]["status"], "label_rule": row["label_rule_version"], "timestamp": utc_now()})
                    last_error = None
                    break
                except BaseException as exc:
                    last_error = exc
                    prior[row["sample_id"]] = attempt
                    append_jsonl(run_dir / "failures.jsonl", {"sample_id": row["sample_id"], "subset": row["physical_subset"], "map_id": row["map_id"], "shard_index": args.shard_index, "attempt": attempt, "error_type": type(exc).__name__, "error_message": str(exc), "staging_dir": str(staging), "timestamp": utc_now()})
            if last_error is not None:
                rejected.append(row["sample_id"])
    finally:
        cleanup = []
        try:
            world.apply_settings(old_settings)
        except BaseException as exc:
            cleanup.append(f"restore_settings: {exc}")
        try:
            world.set_weather(old_weather)
        except BaseException as exc:
            cleanup.append(f"restore_weather: {exc}")
        if cleanup:
            raise ConfigError(f"Benchmark-V3 worker cleanup failed: {cleanup}")
    result = {"schema_version": "benchmark_v3_worker_result_1.0", "run_id": args.run_id, "map_id": args.map_id, "shard_index": args.shard_index, "shard_count": args.shard_count, "expected": [row["sample_id"] for row in rows], "accepted": accepted, "reused": [], "rejected": rejected, "retried_count": retried_count, "cleanup": "pass"}
    write_json(result_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(run_worker(parse_args(argv)), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
