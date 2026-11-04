"""Confirmed CARLA worker for the frozen V3 R4 corrective pilot.

The outer wrapper owns and prepares one CARLA server/map.  This worker never
calls ``load_world`` or starts a server.  All offline identity and output
checks run before the delayed CARLA import.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

from benchmark_generator.common.cleanup import destroy_actors
from benchmark_generator.common.io import append_jsonl, utc_now, write_json
from benchmark_generator.config import ConfigError, ensure_path_within, load_json
from benchmark_generator.v2.geometry import build_target_corridor, collect_local_lane_markings
from benchmark_generator.v3.constants import OFFICIAL_LABELS, R4_CONFLICT_EXCLUSION_RULE_VERSION, R4_CONFLICT_SAFE_TOPOLOGY_RULE_VERSION, R4_EXECUTION_ARTIFACT_ROOT, R4_PROTOCOL_VERSION, R4_SAMPLE_METADATA_SCHEMA_VERSION, R4_TRAFFIC_TOPOLOGY_SNAP_HARD_MAX_M, R4_TRAFFIC_TOPOLOGY_SNAP_WARN_M, R4_CONFLICT_ZONE_RADIUS_M, V3_ARTIFACT_ROOT, V3_SCHEMA_ROOT
from benchmark_generator.common.io import file_sha256
from benchmark_generator.v3.capture_plan import R4_ASSETS, R4_CONFIG_PATH, _apply_conflict_safe_coverage_policy, _layout_qa, _uses_conflict_safe_layout, assert_formal_traffic_gate, build_r4_plan
from benchmark_generator.v3.capture_artifacts import validate_r4_static_artifact
from benchmark_generator.v3.capture_execution import partition_rows, publish_r4_sample, validate_r4_sample
from benchmark_generator.v3.schema_support import validate_schema_instance
from benchmark_generator.v3.strict_geometry import evaluate_geometry
from benchmark_generator.v3.replay import canonical_replay
from benchmark_generator.v3.label_merge import (
    RULE_VERSION as V3_PHYSICAL_MARKING_RULE_VERSION,
    project_physical_chains,
    reconstruct_site_physical_chains,
)
from benchmark_generator.v3.plan import PROTOCOL_VERSION as V3_PROTOCOL_VERSION


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Internal confirmed V3 R4 corrective map worker.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--static-run", type=Path, required=True)
    parser.add_argument("--map-id", choices=["Town04_Opt", "Town05_Opt", "Town10HD_Opt"], required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--result-path", type=Path, required=True)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=3012)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--execute-worker", action="store_true")
    parser.add_argument("--confirm-v3-p1-corrective-worker", action="store_true")
    parser.add_argument("--confirm-v3-p1-corrective", action="store_true")
    parser.add_argument("--confirm-v3-p1-server-lifecycle", action="store_true")
    return parser.parse_args(argv)


def _validate_confirmations(args: argparse.Namespace) -> None:
    if not args.execute_worker or not args.confirm_v3_p1_corrective_worker:
        raise ConfigError("R4 worker requires --execute-worker and --confirm-v3-p1-corrective-worker")
    if not args.confirm_v3_p1_corrective:
        raise ConfigError("R4 worker requires --confirm-v3-p1-corrective")
    if not args.confirm_v3_p1_server_lifecycle:
        raise ConfigError("R4 worker requires --confirm-v3-p1-server-lifecycle")


def _validate_worker_manifest(manifest: dict[str, Any], config: dict[str, Any], static_root: Path, args: argparse.Namespace, rows: list[dict[str, Any]]) -> None:
    if manifest.get("run_id") != args.run_id or manifest.get("stage") != "p1_corrective" or manifest.get("scope") not in {"corrective_4_anchors_24_rows_r4", "corrective_conflict_safe_6_rows_r4", "corrective_conflict_safe_7_rows_r4", "corrective_conflict_safe_14_rows_r4"}:
        raise ConfigError("R4 worker run identity or scope drifted")
    if manifest.get("config_sha256") != config["_config_sha256"]:
        raise ConfigError("R4 worker config checksum mismatch")
    if manifest.get("static_artifact", {}).get("run_id") != static_root.name:
        raise ConfigError("R4 worker static artifact run mismatch")
    expected_static = {
        "artifact_root": str(static_root),
        "static_manifest_sha256": file_sha256(static_root / "static_manifest.json"),
        "r4_plan_sha256": file_sha256(static_root / "r4_plan.json"),
        "expected_r4_ledger_sha256": file_sha256(static_root / "expected_r4_ledger.json"),
        "expected_r4_traffic_ledger_sha256": file_sha256(static_root / "expected_r4_traffic_ledger.json"),
        "recipe_table_sha256": file_sha256(static_root / "recipe_table.json"),
        "source_checksums_sha256": file_sha256(static_root / "source_checksums.json"),
    }
    for key, value in expected_static.items():
        if manifest.get("static_artifact", {}).get(key) != value:
            raise ConfigError(f"R4 worker static artifact checksum drifted: {key}")
    if sorted(manifest.get("sample_ids", [])) != sorted(row["sample_id"] for row in rows):
        raise ConfigError("R4 worker ledger IDs drifted")
    if manifest.get("map_ids") != ["Town04_Opt", "Town05_Opt", "Town10HD_Opt"] or manifest.get("official_labels") != OFFICIAL_LABELS:
        raise ConfigError("R4 worker map or label contract drifted")
    if int(manifest.get("shard_count", -1)) != int(args.shard_count):
        raise ConfigError("R4 worker shard count drifted")
    if manifest.get("pilot_evidence") is not True or manifest.get("canonical_dataset") is not False or manifest.get("registry_write") is not False or manifest.get("formal_split") != "not_run":
        raise ConfigError("R4 worker evidence boundary drifted")
    if manifest.get("p0") != "closed_not_mutated" or manifest.get("rc") != "not_entered" or manifest.get("full") != "not_entered":
        raise ConfigError("R4 worker release boundary drifted")
    lifecycle = manifest.get("lifecycle", {})
    if lifecycle.get("host") != config["lifecycle"]["host"] or int(lifecycle.get("rpc_port", -1)) != int(config["lifecycle"]["rpc_port"]):
        raise ConfigError("R4 worker lifecycle host/port drifted")
    if lifecycle.get("worker_map_load") != "forbidden" or lifecycle.get("adopt_existing") is not False:
        raise ConfigError("R4 worker must not load a map or adopt a server")


def _v3_physical_marking_merge_applies(row: dict[str, Any], site: dict[str, Any], config: dict[str, Any]) -> bool:
    """Versioned V3 generation-time LABELFIX composite branch.

    The frozen Benchmark-V3 ledger marks exactly the user-confirmed LABELFIX.1
    sites with ``_v3_merge_rule``; the branch engages only when that flag is
    present AND the site is inside the confirmed scope carried by the
    execution config.  Every other row (R4 / RC-v2 / Full.2, and Benchmark-V3
    rows outside the confirmed scope) keeps the historical
    project-then-dedup default; R4 default semantics are untouched.
    """
    if not row.get("_v3_merge_rule"):
        return False
    confirmed = set(config.get("_confirmed_merge_site_ids") or ())
    return site["site_id"] in confirmed


def _project_v3_physical_markings(
    carla: Any,
    waypoints: list[Any],
    site: dict[str, Any],
    config: dict[str, Any],
    row: dict[str, Any],
    camera_metadata: dict[str, Any],
    corridor_construction: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reconstruct unique physical marking chains and reproject them.

    Deterministic on the frozen OpenDRIVE world geometry; a chain count
    drift or projection mismatch fails closed instead of falling back to
    the default rule.
    """
    road_library = config.get("_road_library")
    if road_library is None:
        raise ConfigError(f"V3 physical-marking merge requires the frozen road library for {row['sample_id']}")
    drop_ref = None
    if site["site_id"] == config.get("_special_drop_site_id"):
        drop_ref = tuple(config["_special_drop_source_ref"])
    geometry = reconstruct_site_physical_chains(
        carla=carla, waypoints=waypoints, site=site, road_library=road_library,
        allow_marking_type_transitions=True, drop_source_ref=drop_ref,
    )
    metadata = {
        "sample_id": row["sample_id"],
        "actual_conditions": {"camera": camera_metadata},
        "labels": {"lane_polyline_projected": {"corridor_construction": corridor_construction}},
    }
    instances = project_physical_chains(carla=carla, site_geometry=geometry, metadata=metadata)
    expected = int(geometry["expected_physical_marking_count"])
    if len(instances) != expected:
        raise ConfigError(
            f"V3 physical marking instance count mismatch: {row['sample_id']} "
            f"actual={len(instances)} expected={expected} dropped={geometry['dropped_fragment_ids']}"
        )
    return instances, geometry


def _apply_v3_merge_provenance(metadata: dict[str, Any], geometry: dict[str, Any]) -> None:
    lane = metadata["labels"]["lane_polyline_projected"]
    lane.update({
        "rule_version": V3_PHYSICAL_MARKING_RULE_VERSION,
        "expected_physical_marking_count": int(geometry["expected_physical_marking_count"]),
        "actual_physical_marking_count": len(lane["instances"]),
        "lane_group_count": int(geometry["lane_group_count"]),
        "lane_groups": deepcopy(geometry["lane_groups"]),
        "physical_marking_merge_provenance": {
            "site_geometry_rule_version": V3_PHYSICAL_MARKING_RULE_VERSION,
            "source_fragment_count": len(geometry["fragments"]),
            "merged_chain_count": len(geometry["chains"]),
            "merge_edge_count": len(geometry["edges"]),
            "dropped_fragment_ids": deepcopy(geometry["dropped_fragment_ids"]),
            "special_user_drop_without_merge": bool(geometry["dropped_fragment_ids"]),
            "source_boundary_unique_chain_assignment": True,
            "junction_internal_crossing": geometry["junction_internal_crossing"],
            "merge_split_conflict_crossing": geometry["merge_split_conflict_crossing"],
        },
    })


def _new_staging_dir(run_dir: Path, sample_id: str) -> Path:
    root = ensure_path_within(Path(run_dir) / "_staging", [R4_EXECUTION_ARTIFACT_ROOT], "R4 staging root")
    root.mkdir(parents=True, exist_ok=True)
    index = len(list(root.glob(f"{sample_id}_*")))
    path = root / f"{sample_id}_{index:04d}"
    while path.exists():
        index += 1
        path = root / f"{sample_id}_{index:04d}"
    path.mkdir(parents=False, exist_ok=False)
    return path


def run_worker(args: argparse.Namespace) -> dict[str, Any]:
    _validate_confirmations(args)
    static_root = ensure_path_within(args.static_run, [V3_ARTIFACT_ROOT / "p1_corrective_static"], "R4 worker static run")
    static_manifest = validate_r4_static_artifact(static_root)
    bundle = build_r4_plan(Path(static_manifest["config_path"]))
    config = bundle["config"]
    if Path(args.config).resolve() != Path(config["_config_path"]).resolve():
        raise ConfigError("R4 worker config path does not match immutable static input")
    if args.host != config["lifecycle"]["host"] or int(args.port) != int(config["lifecycle"]["rpc_port"]):
        raise ConfigError("R4 worker host/port does not match frozen lifecycle")
    if int(args.shard_count) != int(config["sharding"]["shard_count"]):
        raise ConfigError("R4 worker shard count does not match frozen config")
    run_dir = ensure_path_within(args.run_dir, [R4_EXECUTION_ARTIFACT_ROOT], "R4 worker run")
    result_path = ensure_path_within(args.result_path, [run_dir / "_workers"], "R4 worker result")
    if result_path.exists():
        raise ConfigError(f"R4 worker result already exists: {result_path}")
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.is_file():
        raise ConfigError(f"R4 worker run manifest is missing: {manifest_path}")
    manifest = load_json(manifest_path)
    selected_ids = set(manifest.get("sample_ids", []))
    selected_rows = [row for row in bundle["rows"] if row["sample_id"] in selected_ids]
    rows = partition_rows((row for row in selected_rows if row["map_id"] == args.map_id), int(args.shard_count), int(args.shard_index))
    expected_ids = [row["sample_id"] for row in rows]
    _validate_worker_manifest(manifest, config, static_root, args, selected_rows)

    reused: list[str] = []
    pending: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    for row in rows:
        existing = validate_r4_sample(run_dir, row)
        if existing is None:
            pending.append(row)
        else:
            reused.append(row["sample_id"])
            result_rows.append({"sample_id": row["sample_id"], "status": "reused_verified_complete", "metadata_path": f"samples/{row['sample_id']}/metadata.json"})
    if not pending:
        return _write_worker_result(result_path, args, expected_ids, completed=[], reused=reused, failed=[], rows=result_rows, cleanup_errors=[])

    # Keep all confirmation, checksum, path, and resume checks above this line.
    import carla  # type: ignore
    from benchmark_generator.carla_client import carla_helpers as helpers
    from benchmark_generator.carla_client import v3_preview_worker as preview_helpers

    client = carla.Client(args.host, int(args.port))
    client.set_timeout(min(30.0, float(config["limits"]["server_ready_timeout_seconds"])))
    world = client.get_world()
    current_map = str(world.get_map().name).split("/")[-1]
    if current_map != args.map_id:
        raise ConfigError(f"R4 worker refuses map loading: expected prepared {args.map_id}, got {current_map}")
    carla_map = world.get_map()
    waypoint_cache = carla_map.generate_waypoints(2.0)
    blueprints = world.get_blueprint_library()
    old_settings = world.get_settings()
    old_weather = world.get_weather()
    settings = world.get_settings()
    settings.synchronous_mode = True
    settings.fixed_delta_seconds = float(config["camera"]["fixed_delta_seconds"])
    world.apply_settings(settings)

    completed: list[str] = []
    failed: list[str] = []
    cleanup_errors: list[str] = []
    primary_error: BaseException | None = None
    sites = bundle["source_sites"]
    anchors = bundle["source_anchors"]
    try:
        for row in pending:
            site = sites[row["site_id"]]
            anchor = anchors[row["anchor_id"]]
            staging_dir = _new_staging_dir(run_dir, row["sample_id"])
            try:
                metadata = _generate_one_r4_sample(carla, helpers, preview_helpers, world, carla_map, blueprints, waypoint_cache, config, row, site, anchor, staging_dir, args)
                final_dir = run_dir / "samples" / row["sample_id"]
                checksums = publish_r4_sample(staging_dir, final_dir)
                integrity = {"sample_id": row["sample_id"], "site_id": row["site_id"], "anchor_id": row["anchor_id"], "map_id": row["map_id"], "leakage_group_id": row["leakage_group_id"], "assets": checksums, "metadata_sha256": checksums["metadata.json"], "planned_world_state_hash": row["traffic_plan"]["planned_world_state_hash"], "actual_world_state_hash": metadata["traffic"]["actual_world_state_hash"], "traffic_replay_hash": metadata["traffic"]["replay"]["actual_replay_hash"], "pilot_evidence": True, "canonical_dataset": False, "timestamp": utc_now()}
                append_jsonl(run_dir / "integrity.jsonl", integrity)
                result = {"sample_id": row["sample_id"], "site_id": row["site_id"], "anchor_id": row["anchor_id"], "map_id": row["map_id"], "status": "complete", "qa_disposition": metadata["qa"]["status"], "traffic_qa": metadata["traffic"]["qa"], "assets": checksums, "pilot_evidence": True, "canonical_dataset": False}
                completed.append(row["sample_id"])
                result_rows.append(result)
                append_jsonl(run_dir / "results.jsonl", result)
            except BaseException as exc:
                failed.append(row["sample_id"])
                failure = {"sample_id": row["sample_id"], "site_id": row["site_id"], "anchor_id": row["anchor_id"], "map_id": row["map_id"], "status": "failed_retry_same_identity_on_resume", "failure_reason": _failure_reason(exc), "failure_type": type(exc).__name__, "failure_message": str(exc), "identity_diagnostic": deepcopy(getattr(exc, "r4_identity_diagnostic", None)), "cleanup_errors": list(getattr(exc, "r4_cleanup_errors", [])), "staging_dir": str(staging_dir), "timestamp": utc_now()}
                append_jsonl(run_dir / "failures.jsonl", failure)
                raise
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            world.apply_settings(old_settings)
        except BaseException as exc:
            cleanup_errors.append(f"restore_world_settings: {exc}")
        try:
            world.set_weather(old_weather)
        except BaseException as exc:
            cleanup_errors.append(f"restore_world_weather: {exc}")
        if cleanup_errors:
            append_jsonl(run_dir / "execution_breadcrumbs.jsonl", {"run_id": args.run_id, "event": "capture_worker_cleanup_error", "timestamp": utc_now(), "details": {"map_id": args.map_id, "cleanup_errors": cleanup_errors, "primary_error": type(primary_error).__name__ if primary_error else None}})
            if primary_error is not None:
                setattr(primary_error, "r4_cleanup_errors", list(cleanup_errors))
        if primary_error is None and cleanup_errors:
            raise ConfigError(f"R4 worker cleanup failed: {cleanup_errors}")
    return _write_worker_result(result_path, args, expected_ids, completed=completed, reused=reused, failed=failed, rows=result_rows, cleanup_errors=cleanup_errors)


def _generate_one_r4_sample(carla: Any, helpers: Any, preview_helpers: Any, world: Any, carla_map: Any, blueprints: Any, waypoint_cache: list[Any], config: dict[str, Any], row: dict[str, Any], site: dict[str, Any], anchor: dict[str, Any], staging_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    condition = row["requested_conditions"]
    transform, camera_metadata = preview_helpers._camera_transform(carla, carla_map, site, anchor, condition)
    expected_geometry = row["camera_geometry"]
    ground_z = float(camera_metadata["resolved_world"]["ground_z"])
    actual_camera_geometry = {"road_reference_z_m": round(ground_z, 6), "requested_relative_height_m": float(condition["height_m"]), "camera_world_z_m": round(float(transform.location.z), 6), "resolved_relative_height_m": round(float(transform.location.z) - ground_z, 6), "planned": expected_geometry, "road_reference_z_delta_m": round(ground_z - float(expected_geometry["road_reference_z_m"]), 6), "camera_world_z_delta_m": round(float(transform.location.z) - float(expected_geometry["expected_camera_world_z_m"]), 6)}
    weather_metadata = _apply_r4_weather(carla, world, anchor, condition)
    segments = preview_helpers._site_segments(carla, waypoint_cache, site)
    if not segments:
        raise ConfigError(f"no CARLA driving waypoints for R4 site {site['site_id']}")
    boundaries: list[dict[str, Any]] = []
    corridor_polygons: list[list[list[float]]] = []
    corridor_construction: list[dict[str, Any]] = []
    for segment in segments:
        fraction = preview_helpers._anchor_fraction_for_segment(anchor, segment)
        boundaries.extend(collect_local_lane_markings(carla, waypoint_cache, segment, fraction, 120.0))
        corridor = build_target_corridor(waypoint_cache, segment, fraction, transform, float(condition["fov_degrees"]), int(condition["image_width"]), int(condition["image_height"]), half_window_m=120.0)
        corridor_polygons.extend(corridor["polygons"])
        corridor_construction.append(corridor["construction"])
    v3_geometry = None
    if _v3_physical_marking_merge_applies(row, site, config):
        lane_instances, v3_geometry = _project_v3_physical_markings(
            carla, waypoint_cache, site, config, row, camera_metadata, corridor_construction,
        )
    else:
        lane_instances = helpers._project_lane_instances(boundaries, transform, float(condition["fov_degrees"]))
    planned_actors = deepcopy(row["traffic_plan"]["actors"])
    topology = _validate_traffic_topology(carla, carla_map, planned_actors, config, row)
    actors: list[Any] = []
    primary_error: BaseException | None = None
    try:
        rc_v2_staged_static = row.get("protocol_version") == "V3-PLAN-20260731-RC-V2.0"
        planned, actual_actors, actors = _spawn_r4_traffic(
            carla, world, blueprints, row["traffic_plan"], row["sample_id"],
            tm_port=getattr(args, "tm_port", None), staged_static=rc_v2_staged_static,
        )
        rgb_path = staging_dir / "rgb.jpg"
        mask_path = staging_dir / "road_mask_visible.png"
        rgb_image, semantic_image, simulation_tick, exposure_metadata = preview_helpers._capture_preview_pair(world, blueprints, helpers, transform, condition, config["limits"])
        helpers._save_rgb(rgb_image, rgb_path, int(condition["image_width"]), int(condition["image_height"]))
        semantic_meta = preview_helpers._save_road_mask_visible(semantic_image, mask_path, helpers, int(condition["image_width"]), int(condition["image_height"]))
        preview_helpers._write_road_overlay(rgb_path, mask_path, staging_dir / "road_mask_visible_overlay.jpg")
        preview_helpers._write_lane_overlay(rgb_path, lane_instances, corridor_polygons, staging_dir / "lane_polyline_projected_overlay.jpg")
        exposure_metrics = preview_helpers._rgb_exposure_metrics(rgb_path)
        exposure_qa = preview_helpers._evaluate_exposure_metrics(exposure_metrics, {"preview": {"exposure_qa": {"max_y_ge_245_ratio": 0.03, "max_y_ge_250_ratio": 0.01, "min_p50_y": 65.0, "max_p50_y": 190.0, "max_y_le_5_ratio": 0.35}}})
        geometry_qa = _evaluate_r4_geometry(config, row, site, corridor_polygons, lane_instances, condition, actual_camera_geometry)
        if semantic_meta["mask_ratio"] <= 0.001:
            raise ConfigError("road_mask_visible is empty or below R4 minimum ratio")
        if not lane_instances:
            raise ConfigError("lane_polyline_projected is empty")
        replay_audit = canonical_replay(
            protocol_version=R4_PROTOCOL_VERSION,
            master_layout_hash=row["traffic_plan"]["master_layout_hash"],
            density=float(row["traffic_plan"]["requested_density"]),
            planned_actors=planned,
            actual_actors=actual_actors,
        )
        if row.get("protocol_version") == "V3-PLAN-20260731-RC-V2.0" and not replay_audit["match"]:
            raise ConfigError(f"RC-v2 planned/actual canonical replay mismatch for {row['sample_id']}: {replay_audit['mismatch_diagnostics']}")
        if _uses_conflict_safe_layout(config) and not replay_audit["match"]:
            raise ConfigError(f"R4 planned/actual canonical replay mismatch for {row['sample_id']}: {replay_audit['mismatch_diagnostics']}")
        actual_world_state = _actual_world_state(row, config, weather_metadata, planned, actual_actors, camera_metadata, actual_camera_geometry, simulation_tick, replay_audit=replay_audit)
        traffic_qa = deepcopy(row["traffic_plan"]["qa"])
        traffic_qa.update({"topology": topology, "spawn_collision": {"status": "pass", "planned_actor_count": len(planned), "actual_actor_count": len(actual_actors), "method": "elevated_spawn_fixed_transform_with_obb_gate" if rc_v2_staged_static else "direct_final_transform_spawn"}, "replay": {**traffic_qa.get("replay", {}), "status": "pass" if replay_audit["match"] else "fail_closed", "match": bool(replay_audit["match"]), "planned_replay_hash": replay_audit["planned_hash"], "actual_replay_hash": replay_audit["actual_hash"], "canonical_schema_version": replay_audit["schema_version"], "canonical_rule_version": replay_audit["rule_version"], "mismatch_diagnostics": replay_audit["mismatch_diagnostics"]}})
        formal_traffic = assert_formal_traffic_gate(
            traffic_qa,
            traffic={"actual_actor_count": len(actual_actors), "planned_actor_count": len(planned)},
            world_state=actual_world_state,
            config=config,
            sample_id=row["sample_id"],
        )
        traffic_qa["formal_gate"] = formal_traffic
        qa_status = "needs_manual_review" if geometry_qa["status"] == "fail" else ("warn" if exposure_qa["status"] != "pass" else "pass")
        assets: dict[str, dict[str, Any]] = {}
        for name in R4_ASSETS:
            item: dict[str, Any] = {"path": f"samples/{row['sample_id']}/{name}"}
            if name != "metadata.json":
                item["sha256"] = file_sha256(staging_dir / name)
            assets[name] = item
        metadata = {
            "schema_version": R4_SAMPLE_METADATA_SCHEMA_VERSION,
            "protocol_version": R4_PROTOCOL_VERSION,
            "sample_id": row["sample_id"], "subset": row["subset"], "map_id": row["map_id"], "site_id": row["site_id"], "anchor_id": row["anchor_id"], "leakage_group_id": row["leakage_group_id"], "structural_cell": row["structural_cell"], "observation_recipe_id": row["observation_recipe_id"], "recipe_family": row["recipe_family"], "pilot_kind": row["pilot_kind"], "is_reference": bool(row["is_reference"]),
            "requested_conditions": deepcopy(condition), "camera_geometry": deepcopy(expected_geometry), "actual_camera_geometry": actual_camera_geometry,
            "actual_conditions": {"camera": camera_metadata, "weather": weather_metadata, "simulation_tick": int(simulation_tick), "fixed_delta_seconds": float(config["camera"]["fixed_delta_seconds"]), "warmup_ticks": int(condition["warmup_ticks"]), "exposure": exposure_metadata},
            "official_labels": list(OFFICIAL_LABELS),
            "traffic": {"density_unit": row["traffic_plan"]["density_unit"], "requested_density": row["traffic_plan"]["requested_density"], "valid_lane_count": row["traffic_plan"]["valid_lane_count"], "valid_window_m": row["traffic_plan"]["valid_window_m"], "lane_km": row["traffic_plan"]["lane_km"], "target_actor_count": row["traffic_plan"]["target_actor_count"], "planned_actor_count": row["traffic_plan"]["planned_actor_count"], "actual_actor_count": len(actual_actors), "master_layout_seed": row["traffic_plan"]["master_layout_seed"], "master_layout_hash": row["traffic_plan"]["master_layout_hash"], "layout_rule_version": row["traffic_plan"]["layout_rule_version"], "nested_parent_density": row["traffic_plan"]["nested_parent_density"], "planned_actors": planned, "actual_actors": actual_actors, "planned_world_state_hash": row["traffic_plan"]["planned_world_state_hash"], "actual_world_state_hash": actual_world_state["world_state_hash"], "qa": traffic_qa, "replay": {"schema_version": replay_audit["schema_version"], "rule_version": replay_audit["rule_version"], "planned_replay_hash": replay_audit["planned_hash"], "actual_replay_hash": replay_audit["actual_hash"], "master_layout_hash": row["traffic_plan"]["master_layout_hash"], "canonical_payload": {"planned": replay_audit["planned_payload"], "actual": replay_audit["actual_payload"]}, "mismatch_diagnostics": replay_audit["mismatch_diagnostics"], "status": "pass" if replay_audit["match"] else "fail_closed"}},
            "world_state": actual_world_state,
            "labels": {"road_mask_visible": {"protocol_name": "road_mask_visible", "label_role": "official_prediction_target", "source_type": "platform_direct_sensor_derived", "path": f"samples/{row['sample_id']}/road_mask_visible.png", "decode_policy": "raw_semantic_ids_roads_1_plus_roadlines_24", "accepted_class_ids": [1, 24], "mask_ratio": semantic_meta["mask_ratio"], "semantic_decode": semantic_meta}, "lane_polyline_projected": {"protocol_name": "lane_polyline_projected", "label_role": "official_prediction_target", "source_type": "program_derived", "visibility_semantics": "projected_in_image_no_occlusion_test", "instances": lane_instances, "target_corridor_polygon": corridor_polygons, "corridor_construction": corridor_construction}},
            "qa": {"status": qa_status, "policy_version": config["qa"]["policy_version"], "reference_recipe": bool(row["is_reference"]), "exposure": {**exposure_qa, "status": "pass" if exposure_qa["status"] == "pass" else "warn", "disposition": "warn_only_triage"}, "strict_geometry": geometry_qa, "official_label_keys": list(OFFICIAL_LABELS)},
            "assets": assets, "pilot_evidence": True, "canonical_dataset": False, "formal_split": "not_run", "p0": "closed_not_mutated", "rc": "not_entered", "full": "not_entered",
            "source_checksums": {"config_sha256": config["_config_sha256"], "static_manifest_sha256": file_sha256(Path(row["_static_artifact_root"]) / "static_manifest.json") if row.get("_static_artifact_root") else None, "recipe_projection_fingerprint": row["recipe_expected_projection"]["projection_fingerprint"]},
        }
        metadata["source_checksums"] = {key: value for key, value in metadata["source_checksums"].items() if value is not None}
        if v3_geometry is not None:
            _apply_v3_merge_provenance(metadata, v3_geometry)
        validate_schema_instance(metadata, load_json(V3_SCHEMA_ROOT / "r4_sample_metadata.schema.json"), f"R4 sample metadata {row['sample_id']}")
        write_json(staging_dir / "metadata.json", metadata)
        return metadata
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        errors = destroy_actors(world, actors, tick_after=True)
        if errors:
            if primary_error is not None:
                setattr(primary_error, "r4_cleanup_errors", list(errors))
            else:
                raise ConfigError(f"R4 scripted actor cleanup failed: {errors}")


def _obb_footprint(transform: dict[str, Any], bounding_box: Any, margin_m: float = 0.05) -> tuple[tuple[float,float],tuple[float,float],tuple[float,float],float,float]:
    yaw = math.radians(float(transform["rotation"]["yaw"]))
    longitudinal=(math.cos(yaw),math.sin(yaw)); lateral=(-math.sin(yaw),math.cos(yaw))
    local=getattr(bounding_box,"location",None); local_x=float(getattr(local,"x",0.0)); local_y=float(getattr(local,"y",0.0))
    center=(float(transform["location"]["x"])+longitudinal[0]*local_x+lateral[0]*local_y,float(transform["location"]["y"])+longitudinal[1]*local_x+lateral[1]*local_y)
    extent=getattr(bounding_box,"extent",None)
    return center,longitudinal,lateral,float(getattr(extent,"x",0.0))+margin_m,float(getattr(extent,"y",0.0))+margin_m


def _obb_overlaps(left: tuple[Any,...], right: tuple[Any,...]) -> bool:
    delta=(right[0][0]-left[0][0],right[0][1]-left[0][1])
    for axis in (left[1],left[2],right[1],right[2]):
        distance=abs(delta[0]*axis[0]+delta[1]*axis[1])
        left_radius=left[3]*abs(left[1][0]*axis[0]+left[1][1]*axis[1])+left[4]*abs(left[2][0]*axis[0]+left[2][1]*axis[1])
        right_radius=right[3]*abs(right[1][0]*axis[0]+right[1][1]*axis[1])+right[4]*abs(right[2][0]*axis[0]+right[2][1]*axis[1])
        if distance>=left_radius+right_radius: return False
    return True


def _spawn_r4_traffic(carla: Any, world: Any, blueprints: Any, traffic_plan: dict[str, Any], sample_id: str, *, tm_port: int | None = None, staged_static: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
    planned = deepcopy(traffic_plan["actors"])
    actors: list[Any] = []
    actual: list[dict[str, Any]] = []
    try:
        for index,item in enumerate(planned):
            blueprint = blueprints.find(item["vehicle_blueprint_id"])
            spawn_transform=deepcopy(item["transform"])
            if staged_static:
                base_height=float(traffic_plan.get("staged_spawn_base_height_m",100.0))
                vertical_step=float(traffic_plan.get("staged_spawn_vertical_step_m",5.0))
                spawn_transform["location"]["z"]=float(spawn_transform["location"]["z"])+base_height+index*vertical_step
            actor = world.try_spawn_actor(blueprint, _to_carla_transform(carla, spawn_transform))
            if actor is None:
                raise ConfigError(f"R4 scripted actor spawn/collision failed for {sample_id}: {item['actor_id']}")
            actors.append(actor)
            if callable(getattr(actor, "set_autopilot", None)):
                if tm_port is None:
                    actor.set_autopilot(False)
                else:
                    actor.set_autopilot(False, int(tm_port))
            if not callable(getattr(actor, "set_simulate_physics", None)):
                raise ConfigError(f"R4 actor cannot disable physics for {sample_id}: {item['actor_id']}")
            actor.set_simulate_physics(False)
            if not staged_static: actor.set_transform(_to_carla_transform(carla, item["transform"]))
        if staged_static:
            footprints=[_obb_footprint(item["transform"],actor.bounding_box) for item,actor in zip(planned,actors)]
            overlaps=[(planned[left]["actor_id"],planned[right]["actor_id"]) for left in range(len(footprints)) for right in range(left+1,len(footprints)) if _obb_overlaps(footprints[left],footprints[right])]
            if overlaps: raise ConfigError(f"RC-v2 staged static actor OBB overlap for {sample_id}: {overlaps[:5]}")
            for item,actor in zip(planned,actors): actor.set_transform(_to_carla_transform(carla,item["transform"]))
        if planned:
            world.tick()
        for item, actor in zip(planned, actors):
            if getattr(actor, "is_alive", True) is False or (callable(getattr(actor, "is_alive", None)) and not actor.is_alive()):
                raise ConfigError(f"R4 actor died after spawn for {sample_id}: {item['actor_id']}")
            resolved = _serialize_transform(actor.get_transform())
            if not _transform_close(resolved, item["transform"]):
                raise ConfigError(f"R4 actor transform drifted for {sample_id}: {item['actor_id']}")
            if tm_port is not None:
                item["transform"] = deepcopy(resolved)
            actual.append({**deepcopy(item), "carla_actor_id": int(getattr(actor, "id", -1)), "resolved_transform": resolved, "control": "fixed_transform_no_autopilot"})
        return planned, actual, actors
    except BaseException as exc:
        errors = destroy_actors(world, actors, tick_after=True)
        if errors:
            setattr(exc, "r4_cleanup_errors", errors)
        raise


def _validate_traffic_topology(carla: Any, carla_map: Any, actors: list[dict[str, Any]], config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    # Static R4 transforms are frozen from an OpenDRIVE anchor tangent.  CARLA
    # can expose a slightly curved lane center at the same location, so the
    # warning threshold is one lane width while 1.25 lane widths remains a
    # hard capacity gate.  Spawn/collision and the resolved Driving waypoint
    # are still independent hard gates.
    rc_v2 = row.get("protocol_version") == "V3-PLAN-20260731-RC-V2.0"
    conflict_safe = _uses_conflict_safe_layout(config)
    warn_threshold = float(config["traffic"]["qa"].get("topology_snap_warning_m", R4_TRAFFIC_TOPOLOGY_SNAP_WARN_M))
    threshold = float(config["traffic"]["qa"].get("topology_snap_distance_m", R4_TRAFFIC_TOPOLOGY_SNAP_HARD_MAX_M))
    conflict_radius = float(config["traffic"].get("conflict_zone_radius_m", R4_CONFLICT_ZONE_RADIUS_M))
    junction_centers = []
    if conflict_safe:
        junction_centers = [
            {"x": float(item.transform.location.x), "y": float(item.transform.location.y), "road_id": int(item.road_id), "section_id": int(item.section_id), "lane_id": int(item.lane_id), "s_m": round(float(item.s), 6)}
            for item in carla_map.generate_waypoints(2.0) if bool(item.is_junction)
        ]
    waypoints: list[dict[str, Any]] = []
    for item in actors:
        location = item["transform"]["location"]
        expected_identity = {"road_id": item.get("waypoint_road_id"), "section_id": item.get("waypoint_section_id"), "lane_id": item.get("frozen_lane_id"), "s_m": item.get("waypoint_s_m")}
        if "frozen_lane_id" in item:
            waypoint = carla_map.get_waypoint_xodr(
                int(item["waypoint_road_id"]),
                int(item["frozen_lane_id"]),
                float(item["waypoint_s_m"]),
            )
        else:
            waypoint = carla_map.get_waypoint(
                carla.Location(x=float(location["x"]), y=float(location["y"]), z=float(location["z"])),
                project_to_road=True,
                lane_type=carla.LaneType.Driving,
            )
        if waypoint is None:
            error = ConfigError(f"R4 traffic topology has no driving waypoint for {row['sample_id']}: {item['actor_id']}")
            setattr(error, "r4_identity_diagnostic", {"expected": expected_identity, "actual": None})
            raise error
        distance = math.hypot(float(waypoint.transform.location.x) - float(location["x"]), float(waypoint.transform.location.y) - float(location["y"]))
        if distance > threshold:
            raise ConfigError(f"R4 traffic topology snap distance exceeds threshold for {row['sample_id']}: {item['actor_id']} {distance:.3f}m")
        if "frozen_lane_id" in item:
            if int(waypoint.lane_id) != int(item["frozen_lane_id"]):
                error = ConfigError(f"R4 traffic frozen lane identity drifted for {row['sample_id']}: {item['actor_id']}")
                setattr(error, "r4_identity_diagnostic", {"expected": expected_identity, "actual": {"road_id": int(waypoint.road_id), "section_id": int(waypoint.section_id), "lane_id": int(waypoint.lane_id), "s_m": round(float(waypoint.s), 6)}})
                raise error
            if int(waypoint.road_id) != int(item["waypoint_road_id"]) or int(waypoint.section_id) != int(item["waypoint_section_id"]):
                error = ConfigError(f"R4 traffic frozen road/section identity drifted for {row['sample_id']}: {item['actor_id']}")
                setattr(error, "r4_identity_diagnostic", {"expected": expected_identity, "actual": {"road_id": int(waypoint.road_id), "section_id": int(waypoint.section_id), "lane_id": int(waypoint.lane_id), "s_m": round(float(waypoint.s), 6)}})
                raise error
            if abs(float(waypoint.s) - float(item["waypoint_s_m"])) > 0.75:
                error = ConfigError(f"R4 traffic frozen waypoint s drifted for {row['sample_id']}: {item['actor_id']}")
                setattr(error, "r4_identity_diagnostic", {"expected": expected_identity, "actual": {"road_id": int(waypoint.road_id), "section_id": int(waypoint.section_id), "lane_id": int(waypoint.lane_id), "s_m": round(float(waypoint.s), 6)}})
                raise error
        nearest_distance = None
        nearest_junction = None
        if conflict_safe:
            for center in junction_centers:
                distance_to_junction = math.hypot(float(waypoint.transform.location.x) - center["x"], float(waypoint.transform.location.y) - center["y"])
                if nearest_distance is None or distance_to_junction < nearest_distance:
                    nearest_distance = distance_to_junction
                    nearest_junction = center
            if bool(waypoint.is_junction) or (nearest_distance is not None and nearest_distance < conflict_radius):
                error = ConfigError(f"R4 conflict-safe traffic rejected junction/conflict actor for {row['sample_id']}: {item['actor_id']}")
                setattr(error, "r4_identity_diagnostic", {"expected": expected_identity, "actual": {"road_id": int(waypoint.road_id), "section_id": int(waypoint.section_id), "lane_id": int(waypoint.lane_id), "s_m": round(float(waypoint.s), 6), "is_junction": bool(waypoint.is_junction)}, "conflict_zone": {"rule_version": str(config["traffic"].get("conflict_exclusion_rule_version")), "radius_m": conflict_radius, "nearest_junction_distance_m": round(float(nearest_distance), 6) if nearest_distance is not None else None, "nearest_junction_identity": nearest_junction}})
                raise error
        waypoints.append({"actor_id": item["actor_id"], "road_id": int(waypoint.road_id), "section_id": int(waypoint.section_id), "lane_id": int(waypoint.lane_id), "frozen_lane_id": item.get("frozen_lane_id"), "waypoint_s_m": round(float(waypoint.s), 6), "is_junction": bool(waypoint.is_junction), "snap_distance_m": round(distance, 6), "nearest_junction_distance_m": round(float(nearest_distance), 6) if nearest_distance is not None else None, "nearest_junction_identity": nearest_junction})
    layout_qa = _layout_qa(actors, int(row["traffic_plan"]["valid_lane_count"]), config, use_actual_geometry=True)
    if conflict_safe:
        topology = config.get("_traffic_topology") or {}
        topology_anchor = next(
            (item for item in topology.get("anchors", []) if item.get("anchor_id") == row["anchor_id"]),
            None,
        )
        if topology_anchor is None or not topology_anchor.get("candidate_audit"):
            raise ConfigError(f"R4 conflict-safe runtime coverage audit is missing for {row['anchor_id']}")
        # The nominal [-100m, 100m] span can be shortened by the hard
        # conflict exclusion.  Apply the same audited safe-window gate used
        # by static planning, while retaining the nominal capacity finding.
        layout_qa = _apply_conflict_safe_coverage_policy(
            layout_qa,
            actors,
            topology_anchor["candidate_audit"],
            config,
        )
    if layout_qa["status"] != "pass":
        raise ConfigError(f"R4 traffic runtime layout QA failed for {row['sample_id']}: {layout_qa['reasons']}")
    return {"status": "pass", "hard_gate": True, "snap_distance_warning_threshold_m": warn_threshold, "snap_distance_threshold_m": threshold, "warning_actor_ids": [item["actor_id"] for item in waypoints if item["snap_distance_m"] > warn_threshold], "actor_waypoints": waypoints, "junction_waypoint_count": sum(item["is_junction"] for item in waypoints), "junction_policy":"rc_v2_driving_waypoint_allowed" if rc_v2 else "historical_zero_only", "layout_qa": layout_qa, "conflict_exclusion": {"status": "pass" if conflict_safe or rc_v2 else "legacy_not_checked", "rule_version": str(config["traffic"].get("conflict_exclusion_rule_version")) if conflict_safe else ("benchmark_v3_rc_v2_runtime_driving_waypoint_gate_0.1" if rc_v2 else None), "is_junction_hard_reject": conflict_safe, "conflict_zone_radius_m": conflict_radius if conflict_safe else None}}


def _to_carla_transform(carla: Any, value: dict[str, Any]) -> Any:
    location = value["location"]
    rotation = value["rotation"]
    return carla.Transform(carla.Location(x=float(location["x"]), y=float(location["y"]), z=float(location["z"])), carla.Rotation(pitch=float(rotation["pitch"]), yaw=float(rotation["yaw"]), roll=float(rotation["roll"])))


def _serialize_transform(transform: Any) -> dict[str, Any]:
    return {"location": {"x": round(float(transform.location.x), 6), "y": round(float(transform.location.y), 6), "z": round(float(transform.location.z), 6)}, "rotation": {"pitch": round(float(transform.rotation.pitch), 6), "yaw": round(float(transform.rotation.yaw), 6), "roll": round(float(transform.rotation.roll), 6)}}


def _transform_close(left: dict[str, Any], right: dict[str, Any], tolerance: float = 0.05) -> bool:
    for key in ("x", "y", "z"):
        if abs(float(left["location"][key]) - float(right["location"][key])) > tolerance:
            return False
    for key in ("pitch", "yaw", "roll"):
        difference = ((float(left["rotation"][key]) - float(right["rotation"][key]) + 180.0) % 360.0) - 180.0
        if abs(difference) > tolerance:
            return False
    return True


def _apply_r4_weather(carla: Any, world: Any, anchor: dict[str, Any], condition: dict[str, Any]) -> dict[str, Any]:
    weather_spec = condition["weather"]
    base = carla.WeatherParameters.ClearNoon
    road_yaw = ((-float(anchor["pose_world"]["heading_deg"]) + 180.0) % 360.0) - 180.0
    relative_azimuth = float(condition.get("sun_azimuth_relative_deg", 90.0))
    absolute_azimuth = ((road_yaw + relative_azimuth + 180.0) % 360.0) - 180.0
    weather = carla.WeatherParameters(cloudiness=float(weather_spec.get("cloudiness", base.cloudiness)), precipitation=float(weather_spec.get("precipitation", 0.0)), precipitation_deposits=0.0, wind_intensity=float(base.wind_intensity), sun_azimuth_angle=absolute_azimuth, sun_altitude_angle=float(condition["sun_altitude_deg"]), fog_density=float(weather_spec.get("fog_density", 0.0)), fog_distance=float(weather_spec.get("fog_distance_m") or base.fog_distance), fog_falloff=float(base.fog_falloff), wetness=float(weather_spec.get("wetness", 0.0)), scattering_intensity=float(base.scattering_intensity), mie_scattering_scale=float(base.mie_scattering_scale), rayleigh_scattering_scale=float(base.rayleigh_scattering_scale), dust_storm=0.0)
    world.set_weather(weather)
    return {"family": str(weather_spec["family"]), "cloudiness": float(weather.cloudiness), "precipitation": float(weather.precipitation), "wetness": float(weather.wetness), "fog_density": float(weather.fog_density), "fog_distance": float(weather.fog_distance), "sun_azimuth_angle": float(weather.sun_azimuth_angle), "sun_altitude_angle": float(weather.sun_altitude_angle), "sun_azimuth_relative_to_road_deg": relative_azimuth, "road_reference_yaw_carla_deg": road_yaw}


def _evaluate_r4_geometry(config: dict[str, Any], row: dict[str, Any], site: dict[str, Any], polygons: list[list[list[float]]], instances: list[dict[str, Any]], condition: dict[str, Any], actual_camera_geometry: dict[str, Any]) -> dict[str, Any]:
    policy = deepcopy(config["_qa_policy"])
    context = {"junction_centered": bool(site.get("junction_centered", False)), "merge_conflict_centered": bool(site.get("merge_conflict_centered", False)), "corridor_touches_conflict_zone": bool(site.get("corridor_touches_conflict_zone", False)), "target_lane_count": int(row["traffic_plan"]["valid_lane_count"]), "recipe_actual_projection": deepcopy(row["recipe_expected_projection"])}
    if not row["is_reference"]:
        strict = dict(policy.get("strict_geometry", {}))
        strict["default_area_threshold"] = 0.0
        strict["structural_cell_area_thresholds"] = {}
        policy["strict_geometry"] = strict
    result = evaluate_geometry({"camera": {"image_width": int(condition["image_width"]), "image_height": int(condition["image_height"])}, "labels": {"lane_polyline_projected": {"target_corridor_polygon": polygons, "instances": instances}}, "structural_cell": row["structural_cell"], "qa_strict_geometry": context}, tier="strict", policy_version=config["qa"]["policy_version"], policy=policy, recipe_expected_projection=row["recipe_expected_projection"])
    expected = row["recipe_expected_projection"]["derived_projection_expectation"]
    angle = max(1.0, min(89.0, abs(float(condition["pitch_deg"]))))
    actual_intersection = float(condition["height_m"]) / math.tan(math.radians(angle))
    result["reference_area_gate"] = bool(row["is_reference"])
    result["area_gate_disposition"] = "reference_policy" if row["is_reference"] else "recipe_frozen_projection_non_default"
    result["recipe_actual_projection"] = deepcopy(row["recipe_expected_projection"])
    result["projection_audit"] = {"expected_forward_ground_intersection_from_camera_m": expected["forward_ground_intersection_from_camera_m"], "actual_forward_ground_intersection_from_camera_m": round(actual_intersection, 6), "forward_ground_intersection_delta_m": round(actual_intersection - float(expected["forward_ground_intersection_from_camera_m"]), 6), "actual_camera_geometry": actual_camera_geometry, "status": "pass" if abs(actual_intersection - float(expected["forward_ground_intersection_from_camera_m"])) < 0.01 else "fail"}
    if result["projection_audit"]["status"] != "pass":
        result["status"] = "fail"
        result.setdefault("rejection_reasons", []).append("recipe_projection_runtime_mismatch")
    return result


def _actual_world_state(row: dict[str, Any], config: dict[str, Any], weather: dict[str, Any], planned_actors: list[dict[str, Any]], actual_actors: list[dict[str, Any]], camera_metadata: dict[str, Any], actual_camera_geometry: dict[str, Any], simulation_tick: int, *, replay_audit: dict[str, Any] | None = None) -> dict[str, Any]:
    actors = [{"actor_id": item["actor_id"], "vehicle_blueprint_id": item["vehicle_blueprint_id"], "transform": item["resolved_transform"]} for item in actual_actors]
    hash_input = {"protocol_version": R4_PROTOCOL_VERSION, "map_id": row["map_id"], "sample_id": row["sample_id"], "requested_conditions": row["requested_conditions"], "fixed_delta_seconds": float(config["camera"]["fixed_delta_seconds"]), "weather": weather, "camera": camera_metadata, "camera_geometry": actual_camera_geometry, "actors": actors}
    encoded = json.dumps(hash_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    world_hash = hashlib.sha256(encoded).hexdigest()
    if replay_audit is not None:
        replay_hash = replay_audit["actual_hash"]
        replay_input = replay_audit["actual_payload"]
    else:
        replay_input = {"master_layout_hash": row["traffic_plan"]["master_layout_hash"], "density": row["traffic_plan"]["requested_density"], "actors": actors}
        replay_hash = hashlib.sha256(json.dumps(replay_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"map_id": row["map_id"], "sample_id": row["sample_id"], "synchronous_mode": True, "fixed_delta_seconds": float(config["camera"]["fixed_delta_seconds"]), "warmup_ticks": int(row["requested_conditions"]["warmup_ticks"]), "capture_tick": int(simulation_tick), "weather": weather, "planned_actor_count": len(planned_actors), "actual_actor_count": len(actual_actors), "actor_ids": [item["actor_id"] for item in actual_actors], "actor_blueprints": [item["vehicle_blueprint_id"] for item in actual_actors], "camera_world_z_m": actual_camera_geometry["camera_world_z_m"], "road_reference_z_m": actual_camera_geometry["road_reference_z_m"], "world_state_hash_input": hash_input, "world_state_hash": world_hash, "traffic_replay_hash": replay_hash, "traffic_replay_canonical_payload": replay_input, "hash_rule_version": "benchmark_v3_r4_actual_world_state_0.1"}


def _failure_reason(exc: BaseException) -> str:
    text = str(exc)
    if "sensor_timeout_or_frame_mismatch" in text:
        return "sensor_timeout_or_frame_mismatch"
    if "topology" in text or "waypoint" in text or "lane" in text:
        return "traffic_topology_capacity_failure"
    if "cleanup" in text or "actor" in text or "spawn" in text or "collision" in text:
        return "actor_or_sensor_cleanup_failure"
    if "warmup" in text:
        return "warmup_failure"
    return "r4_generation_exception"


def _write_worker_result(result_path: Path, args: argparse.Namespace, expected_ids: list[str], *, completed: list[str], reused: list[str], failed: list[str], rows: list[dict[str, Any]], cleanup_errors: list[str]) -> dict[str, Any]:
    payload = {"schema_version": "benchmark_v3_worker_result_1.0", "run_id": args.run_id, "map_id": args.map_id, "shard_index": int(args.shard_index), "expected_sample_ids": expected_ids, "completed_sample_ids": completed, "reused_sample_ids": reused, "failed_sample_ids": failed, "rows": rows, "worker_map_load": "not_performed", "cleanup_errors": cleanup_errors, "pilot_evidence": True, "canonical_dataset": False, "registry_write": False, "split_write": False, "updated_at": utc_now()}
    validate_schema_instance(payload, load_json(V3_SCHEMA_ROOT / "worker_result.schema.json"), f"R4 worker result {args.map_id}")
    write_json(result_path, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = run_worker(args)
    except (ConfigError, FileNotFoundError, ValueError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
