#!/usr/bin/env python3
"""Serialise a Benchmark-V3 capture run into the public dataset package.

Deterministic, CARLA-free serialiser: every asset is resolved through the run
manifests (never by scanning directories), re-serialised with the public naming
contract, and sealed with package-level checksums.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Iterable

GENERATION_ROOT = Path(__file__).resolve().parent

BUILDER_VERSION = "benchmark_v3_package_1.0"
EXPORT_PROTOCOL = "benchmark_v3_dataset_1.0"
CARD_SCHEMA_VERSION = "benchmark_v3_dataset_card_1.0"
PACKAGE_SCHEMA_VERSION = "benchmark_v3_package_manifest_1.0"
PROTOCOL_VERSION = "Benchmark-V3-1.0"
DATASET_NAME = "Benchmark-V3"
DATASET_VERSION = "1.0"
PUBLICATION_STATUS = "released"
DATASET_LICENSE = "CC-BY-NC-4.0"
DATASET_CITATION = (
    "Wang, Z., Zhao, J., Liu, X., Gao, X., & Ye, K. SO-RoadUAV: A Scene-Observation "
    "Diagnostic Benchmark for UAV Traffic-Monitoring Road Perception (2026)."
)

LABEL_RULE_MAP = {
    "historical_v2_r4_project_then_full_polyline_dedup_default": "lane_marking_projection_default_1.0",
    "benchmark_v3_lane_physical_marking_cross_segment_merge_0.1": "lane_marking_physical_merge_1.0",
}
RECIPE_FAMILIES = ("development_stratified", "shared_reference", "ofat", "l18", "axis")

GENERATION_STAGE = "benchmark_v3_generation"

EXPECTED_SUBSETS = {"train": 12000, "val": 1584, "test": 5040}
SOURCE_SUBSETS = {"train": "train", "val": "val", "test_shared": "test"}
EXPECTED_VIEWS = {
    "core_scene": 5040,
    "core_observation": 1890,
    "core_axis": 882,
    "challenge_l18": 2268,
}
ASSET_KINDS = ("rgb", "road_mask_visible", "lane", "metadata")
PUBLIC_METADATA_KEYS = {
    "schema_version", "sample_id", "display_name", "subset", "site_id",
    "leakage_group_id", "anchor_id", "map_id", "scene", "road_layout",
    "lane_configuration", "structural_cell", "requested_conditions",
    "actual_conditions", "observation_recipe_id", "control_bundle_id",
    "reference_sample_id", "is_reference", "changed_factor", "factor_level",
    "factor_levels", "challenge_row_id", "challenge_level_tuple",
    "challenge_axis_id", "labels", "traffic_summary", "qa",
    "strict_geometry", "protocol_version", "label_rule_version", "paths",
}
FORBIDDEN_KEYS = {
    "source_uri", "pilot_kind", "p0", "p1", "rc", "full",
    "generation_runtime", "warn_adjudication", "warn_unresolved", "replay",
    "world_state", "breadcrumbs", "failures", "retries", "label_fix",
    "physical_marking_merge_provenance", "master_layout_hash",
    "master_layout_seed", "planned_world_state_hash", "actual_world_state_hash",
}
FORBIDDEN_PATH_PARTS = {"overlay", "review", "breadcrumbs", "failures", "retries", "staging"}


class BuildError(RuntimeError):
    """Fail-closed publication build error."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for row in rows
    )


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == payload:
        return
    path.write_bytes(payload)


def _contained(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise BuildError(f"{label} escapes allowed root: {resolved}")
    return resolved


def _public_label_rule(value: str) -> str:
    if value not in LABEL_RULE_MAP:
        raise BuildError(f"unmapped label rule version: {value}")
    return LABEL_RULE_MAP[value]


# The frozen reference capture run predates the public naming contract and still
# carries the internal recipe-family prefix inside `display_name`. Capture runs
# produced by this repository already emit the public family name, so this
# normalisation is a no-op for them and only keeps historical runs packageable.
INTERNAL_FAMILY_PREFIX = "full_3_"
RECIPE_FAMILIES = ("development_stratified", "shared_reference", "ofat", "l18", "axis")


def _public_display_name(value: str) -> str:
    for family in RECIPE_FAMILIES:
        value = value.replace(f"{INTERNAL_FAMILY_PREFIX}{family}", family)
    return value


def _public_paths(subset: str, sample_id: str) -> dict[str, str]:
    return {
        "rgb": f"{subset}/rgb/{sample_id}.jpg",
        "road_mask": f"{subset}/road_mask_visible/{sample_id}.png",
        "lane": f"{subset}/lane_polyline_projected/{sample_id}.json",
        "metadata": f"{subset}/metadata/{sample_id}.json",
    }


def _road_layout(scene: str) -> str:
    if scene.startswith("straight"):
        return "Straight"
    if scene.startswith("curve"):
        return "Curve"
    if scene == "intersection":
        return "Intersection-Approach"
    if scene == "ramp":
        return "Ramp-Transition"
    raise BuildError(f"unknown public scene: {scene}")


def _lane_configuration(structural_cell: str) -> str:
    if not isinstance(structural_cell, str) or not structural_cell:
        raise BuildError(f"cannot derive lane configuration from {structural_cell!r}")
    if structural_cell == "Ramp-1-to-Mainline-8":
        return structural_cell
    match = re.search(r"(\d+Lane)$", structural_cell)
    if not match:
        raise BuildError(f"cannot derive lane configuration from {structural_cell!r}")
    return match.group(1)


def _traffic_summary(source: dict[str, Any]) -> dict[str, Any]:
    traffic = source.get("traffic") or {}
    actors = traffic.get("actual_actors")
    if actors is None:
        actors = traffic.get("actors")
    if actors is None:
        actors = traffic.get("planned_actors") or []
    blueprints = sorted({str(row["vehicle_blueprint_id"]) for row in actors if row.get("vehicle_blueprint_id")})
    return {
        "lane_change_supported": bool(traffic.get("lane_change_supported", False)),
        "lane_change_actor_count": int(traffic.get("lane_change_actor_count", 0) or 0),
        "vehicle_pool_version": traffic.get("vehicle_pool_version"),
        "vehicle_blueprints_used": blueprints,
    }


def _strict_geometry(source: dict[str, Any]) -> dict[str, Any]:
    value = (source.get("qa") or {}).get("strict_geometry") or {}
    boundary = value.get("main_boundary_length_diagonal_ratio")
    if boundary is None:
        lengths = value.get("projected_boundary_normalized_lengths") or []
        boundary = min(lengths) if lengths else None
    return {
        "boundary_length_diagonal_ratio": boundary,
        "target_corridor_area_ratio": value.get("target_corridor_area_ratio"),
    }


def _requested_conditions(source: dict[str, Any]) -> dict[str, Any]:
    requested = deepcopy(source["requested_conditions"])
    yaw = float(requested.get("yaw_relative_deg", 0.0) or 0.0)
    requested["yaw_magnitude_deg"] = source.get("yaw_magnitude_deg", abs(yaw))
    requested["yaw_sign"] = source.get("yaw_sign", 0 if yaw == 0 else (1 if yaw > 0 else -1))
    requested["traffic_density"] = requested.get("traffic_density_vehicles_per_lane_km")
    return requested


def _public_metadata(source: dict[str, Any], row: dict[str, Any], subset: str, paths: dict[str, str]) -> dict[str, Any]:
    lane = source["labels"]["lane_polyline_projected"]
    road = source["labels"]["road_mask_visible"]
    qa = source.get("qa") or {}
    scene = row["scene"]
    structural_cell = source.get("structural_cell") or row.get("structural_cell")
    lane_label: dict[str, Any] = {
        "path": paths["lane"],
        "visibility_semantics": lane["visibility_semantics"],
        "instance_count": int(len(lane["instances"])),
        "target_corridor_polygon": deepcopy(lane["target_corridor_polygon"]),
    }
    if lane.get("expected_physical_marking_count") is not None:
        lane_label["expected_physical_marking_count"] = int(lane["expected_physical_marking_count"])
        lane_label["lane_group_count"] = int(lane["lane_group_count"])
    value = {
        "schema_version": EXPORT_PROTOCOL,
        "sample_id": source["sample_id"],
        "display_name": _public_display_name(source.get("display_name") or ""),
        "subset": subset,
        "site_id": source["site_id"],
        "leakage_group_id": source.get("leakage_group_id"),
        "anchor_id": source["anchor_id"],
        "map_id": source["map_id"],
        "scene": scene,
        "road_layout": _road_layout(scene),
        "lane_configuration": _lane_configuration(structural_cell),
        "structural_cell": structural_cell,
        "requested_conditions": _requested_conditions(source),
        "actual_conditions": deepcopy(source["actual_conditions"]),
        "observation_recipe_id": source.get("observation_recipe_id"),
        "control_bundle_id": source.get("control_bundle_id"),
        "reference_sample_id": source.get("reference_sample_id"),
        "is_reference": bool(source.get("is_reference")),
        "changed_factor": source.get("changed_factor"),
        "factor_level": source.get("factor_level"),
        "factor_levels": deepcopy(source.get("factor_levels") or {}),
        "challenge_row_id": source.get("challenge_row_id"),
        "challenge_level_tuple": deepcopy(source.get("challenge_level_tuple")),
        "challenge_axis_id": source.get("challenge_axis_id"),
        "labels": {
            "road_mask_visible": {
                "path": paths["road_mask"],
                "source_type": road.get("source_type"),
                "decode_policy": road.get("decode_policy"),
                "accepted_class_ids": deepcopy(road.get("accepted_class_ids", [1, 24])),
            },
            "lane_polyline_projected": lane_label,
        },
        "traffic_summary": _traffic_summary(source),
        "qa": {
            "status": qa.get("status"),
            "policy_version": qa.get("policy_version"),
            "development_qa_tier": qa.get("development_qa_tier", source.get("development_qa_tier")),
        },
        "strict_geometry": _strict_geometry(source),
        "protocol_version": PROTOCOL_VERSION,
        "label_rule_version": _public_label_rule(source.get("label_rule_version")),
        "paths": deepcopy(paths),
    }
    if set(value) != PUBLIC_METADATA_KEYS:
        raise BuildError(f"public metadata implementation whitelist drift: {sorted(set(value) ^ PUBLIC_METADATA_KEYS)}")
    return value


def _lane_payload(source: dict[str, Any]) -> dict[str, Any]:
    lane = source["labels"]["lane_polyline_projected"]
    instances = deepcopy(lane["instances"])
    return {
        "sample_id": source["sample_id"],
        "protocol_version": PROTOCOL_VERSION,
        "label_rule_version": _public_label_rule(source.get("label_rule_version")),
        "visibility_semantics": lane["visibility_semantics"],
        "lane_instance_count": len(instances),
        "instances": instances,
    }


def _condition_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    requested = metadata["requested_conditions"]
    weather = requested.get("weather") or {}
    return {
        "height_m": requested.get("height_m"),
        "pitch_deg": requested.get("pitch_deg"),
        "yaw_relative_deg": requested.get("yaw_relative_deg"),
        "yaw_magnitude_deg": requested.get("yaw_magnitude_deg"),
        "traffic_density": requested.get("traffic_density"),
        "weather": weather.get("family"),
        "illumination_sun_altitude_deg": requested.get("sun_altitude_deg"),
    }


def _subset_manifest_row(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": metadata["sample_id"],
        "rgb": metadata["paths"]["rgb"],
        "road_mask": metadata["paths"]["road_mask"],
        "lane": metadata["paths"]["lane"],
        "metadata": metadata["paths"]["metadata"],
        "scene": metadata["scene"],
        "subset": metadata["subset"],
        "conditions": _condition_summary(metadata),
        "control_bundle_id": metadata["control_bundle_id"],
        "reference_sample_id": metadata["reference_sample_id"],
        "is_reference": metadata["is_reference"],
        "changed_factor": metadata["changed_factor"],
        "factor_level": metadata["factor_level"],
        "factor_levels": metadata["factor_levels"],
        "qa_status": metadata["qa"]["status"],
        "label_rule_version": metadata["label_rule_version"],
    }


def _view_row(view: str, metadata: dict[str, Any]) -> dict[str, Any]:
    if view == "core_scene":
        return {key: metadata[key] for key in ("sample_id", "scene", "map_id", "site_id")}
    if view == "core_observation":
        return {
            "sample_id": metadata["sample_id"], "factor": metadata["changed_factor"],
            "level": metadata["factor_level"], "reference_sample_id": metadata["reference_sample_id"],
        }
    if view == "core_axis":
        return {"sample_id": metadata["sample_id"], "axis": metadata["challenge_axis_id"]}
    if view == "challenge_l18":
        levels = metadata["factor_levels"]
        return {
            "sample_id": metadata["sample_id"], "row_id": metadata["challenge_row_id"],
            "levels": {
                "height": levels.get("height"), "pitch": levels.get("pitch"),
                "yaw_magnitude": levels.get("relative_yaw"),
                "traffic": levels.get("traffic_density"), "weather": levels.get("weather"),
                "illumination": levels.get("illumination"),
            },
        }
    raise BuildError(f"unknown view: {view}")


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _contains_absolute_path(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_absolute_path(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(child) for child in value)
    if isinstance(value, str):
        return value.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", value))
    return False


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    source_sha = _sha256(source)
    if source_sha != expected_sha256:
        raise BuildError(f"source checksum mismatch: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and _sha256(destination) == expected_sha256:
        return
    shutil.copyfile(source, destination)
    if _sha256(destination) != expected_sha256:
        raise BuildError(f"copied checksum mismatch: {destination}")
    if source.stat().st_ino == destination.stat().st_ino and source.stat().st_dev == destination.stat().st_dev:
        raise BuildError(f"hard link is forbidden: {destination}")


def _validate_metadata(value: dict[str, Any]) -> None:
    if set(value) != PUBLIC_METADATA_KEYS:
        raise BuildError(f"metadata whitelist failure for {value.get('sample_id')}")
    forbidden = set(_walk_keys(value)) & FORBIDDEN_KEYS
    if forbidden:
        raise BuildError(f"forbidden metadata keys for {value['sample_id']}: {sorted(forbidden)}")
    if _contains_absolute_path(value):
        raise BuildError(f"absolute path in public metadata for {value['sample_id']}")
    lane = value["labels"]["lane_polyline_projected"]
    if not lane.get("target_corridor_polygon") or "actual_conditions" not in value:
        raise BuildError(f"required public metadata field missing for {value['sample_id']}")


def _iter_package_files(root: Path):
    """Yield package files, skipping the scratch ``_work`` subtree."""

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == "_work":
            continue
        yield path


def _text_checksums(root: Path) -> dict[str, str]:
    suffixes = {".json", ".jsonl", ".md"}
    result = {}
    if not root.exists():
        return result
    for path in _iter_package_files(root):
        relative = path.relative_to(root).as_posix()
        if path.suffix in suffixes or relative == "SHA256SUMS":
            result[relative] = _sha256(path)
    return result


def _profile_readme() -> str:
    return (GENERATION_ROOT / "assets" / "DATASET_README.md").read_text(encoding="utf-8")


def _profile_readme_cn() -> str:
    return (GENERATION_ROOT / "assets" / "DATASET_README_CN.md").read_text(encoding="utf-8")


def _profile_license() -> str:
    return (GENERATION_ROOT / "assets" / "DATASET_LICENSE").read_text(encoding="utf-8")


def _dataset_card(
    run_manifest: dict[str, Any], run_status: dict[str, Any], metadata_by_id: dict[str, dict[str, Any]],
    subset_ids: dict[str, list[str]], view_ids: dict[str, list[str]], opendrive: dict[str, str],
    label_rule: dict[str, Any], package_manifest_sha256: str,
) -> dict[str, Any]:
    scene_total = Counter(row["scene"] for row in metadata_by_id.values())
    scene_by_subset = {
        subset: dict(sorted(Counter(metadata_by_id[sid]["scene"] for sid in ids).items()))
        for subset, ids in subset_ids.items()
    }
    return {
        "schema_version": CARD_SCHEMA_VERSION,
        "publication_status": PUBLICATION_STATUS,
        "dataset_name": DATASET_NAME,
        "dataset_version": DATASET_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "sample_count": len(metadata_by_id),
        "subset_counts": {key: len(value) for key, value in subset_ids.items()},
        "logical_view_counts": {key: len(value) for key, value in view_ids.items()},
        "scene_distribution": dict(sorted(scene_total.items())),
        "scene_exposure_by_subset": scene_by_subset,
        "label_rule": label_rule,
        "maps": {
            map_id: {"opendrive_source": "frozen CARLA map OpenDRIVE", "sha256": checksum}
            for map_id, checksum in sorted(opendrive.items())
        },
        "label_semantics": {
            "road_mask_visible": "CARLA semantic Roads=1 plus RoadLines=24",
            "lane_polyline_projected": "projected_in_image_no_occlusion_test",
            "lane_evaluation_roi": "labels.lane_polyline_projected.target_corridor_polygon",
        },
        "package_manifest_sha256": package_manifest_sha256,
        "citation": DATASET_CITATION,
        "license": DATASET_LICENSE,
    }


def build(run_dir: Path, output_root: Path, report_path: Path, package_timestamp: str | None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    output_root = output_root.resolve()
    if not (run_dir / "run_manifest.json").is_file():
        raise BuildError(f"source run manifest is missing: {run_dir}")
    if run_dir == output_root or run_dir in output_root.parents or output_root in run_dir.parents:
        raise BuildError("source run and output package must be disjoint")
    previous_text = _text_checksums(output_root)

    run_manifest = _load_json(run_dir / "run_manifest.json")
    if run_manifest.get("run_id") != run_dir.name or not run_manifest.get("sample_ids"):
        raise BuildError("source is not a Benchmark-V3 run manifest")
    run_status = _load_json(run_dir / "run_status.json")
    if run_status.get("accepted") != 18624 or int(run_status.get("planned", 0)) != 18624:
        raise BuildError(f"source run is incomplete: {run_status.get('accepted')}/{run_status.get('planned')}")

    timestamp = package_timestamp

    manifest_rows: dict[str, dict[str, Any]] = {}
    for subset in SOURCE_SUBSETS:
        for row in _load_jsonl(run_dir / "manifests/subsets" / f"{subset}.jsonl"):
            if row["sample_id"] in manifest_rows:
                raise BuildError(f"duplicate manifest row: {row['sample_id']}")
            manifest_rows[row["sample_id"]] = row
    if len(manifest_rows) != 18624:
        raise BuildError(f"run manifest rows drifted: {len(manifest_rows)}")
    subset_ids: dict[str, list[str]] = {}
    for source_subset, public_subset in SOURCE_SUBSETS.items():
        ids = [row["sample_id"] for row in _load_jsonl(run_dir / "manifests/subsets" / f"{source_subset}.jsonl")]
        subset_ids[public_subset] = ids
    if {key: len(value) for key, value in subset_ids.items()} != EXPECTED_SUBSETS:
        raise BuildError("run subset counts do not match publication contract")
    subset_for_id = {sample_id: subset for subset, ids in subset_ids.items() for sample_id in ids}

    view_ids = {
        view: [row["sample_id"] for row in _load_jsonl(run_dir / f"manifests/test_core/{view}.jsonl" if view != "challenge_l18" else run_dir / "manifests/test_challenge/challenge_l18.jsonl")]
        for view in EXPECTED_VIEWS
    }
    if {key: len(value) for key, value in view_ids.items()} != EXPECTED_VIEWS:
        raise BuildError("run logical-view counts do not match publication contract")
    if any(not set(ids) <= set(subset_ids["test"]) for ids in view_ids.values()):
        raise BuildError("logical view contains a non-test sample")

    output_root.mkdir(parents=True, exist_ok=True)
    public_metadata: dict[str, dict[str, Any]] = {}
    opendrive_by_map: dict[str, set[str]] = defaultdict(set)
    rgb_mismatches = mask_mismatches = label_mismatches = 0
    merge_count = 0
    default_count = 0
    for index, sample_id in enumerate(sorted(manifest_rows)):
        row = manifest_rows[sample_id]
        subset = subset_for_id[sample_id]
        if row["physical_subset"] not in SOURCE_SUBSETS or SOURCE_SUBSETS[row["physical_subset"]] != subset:
            raise BuildError(f"subset routing mismatch for {sample_id}")
        paths = _public_paths(subset, sample_id)
        source_dir = run_dir / "canonical" / sample_id
        rgb_source = source_dir / "rgb.jpg"
        mask_source = source_dir / "road_mask_visible.png"
        metadata_source = source_dir / "metadata.json"
        for path, label in ((rgb_source, "rgb"), (mask_source, "road mask"), (metadata_source, "metadata")):
            if not path.is_file():
                raise BuildError(f"missing {label} asset: {path}")
        if _sha256(metadata_source) != row["metadata_sha256"]:
            raise BuildError(f"metadata checksum mismatch for {sample_id}")
        source = _load_json(metadata_source)
        if source.get("sample_id") != sample_id:
            raise BuildError(f"canonical metadata identity mismatch for {sample_id}")

        rgb_sha = _sha256(rgb_source)
        mask_sha = _sha256(mask_source)
        _copy_verified(rgb_source, output_root / paths["rgb"], rgb_sha)
        _copy_verified(mask_source, output_root / paths["road_mask"], mask_sha)
        rgb_mismatches += _sha256(output_root / paths["rgb"]) != rgb_sha
        mask_mismatches += _sha256(output_root / paths["road_mask"]) != mask_sha

        lane_payload = _lane_payload(source)
        if len(lane_payload["instances"]) != int(row["lane_instance_count"]):
            raise BuildError(f"lane instance count mismatch for {sample_id}")
        if any(not item.get("points_image") for item in lane_payload["instances"]):
            raise BuildError(f"empty lane instance for {sample_id}")
        _write(output_root / paths["lane"], _json_bytes(lane_payload))
        label_mismatches += _canonical_sha256(lane_payload["instances"]) != _canonical_sha256(source["labels"]["lane_polyline_projected"]["instances"])
        metadata = _public_metadata(source, row, subset, paths)
        _validate_metadata(metadata)
        _write(output_root / paths["metadata"], _json_bytes(metadata))
        public_metadata[sample_id] = metadata
        if source.get("label_rule_version") == "benchmark_v3_lane_physical_marking_cross_segment_merge_0.1":
            merge_count += 1
        else:
            default_count += 1

        strict = (source.get("qa") or {}).get("strict_geometry") or {}
        recipe = strict.get("recipe_actual_projection") or strict.get("recipe_expected_projection") or {}
        checksum = recipe.get("source_opendrive_sha256")
        if checksum:
            opendrive_by_map[source["map_id"]].add(checksum)
        if index and index % 2000 == 0:
            print(f"serialized {index}/{len(manifest_rows)} samples", flush=True)

    if (merge_count, default_count) != (2824, 15800):
        raise BuildError(f"label-rule routing mismatch: merge={merge_count}, default={default_count}")
    if rgb_mismatches or mask_mismatches or label_mismatches:
        raise BuildError("asset or label validation mismatch")
    if set(opendrive_by_map) != {"Town04_Opt", "Town05_Opt", "Town10HD_Opt"}:
        raise BuildError(f"incomplete OpenDRIVE map checksum coverage: {sorted(opendrive_by_map)}")
    if any(len(values) != 1 for values in opendrive_by_map.values()):
        raise BuildError("a map has multiple OpenDRIVE checksums")
    opendrive = {key: next(iter(value)) for key, value in opendrive_by_map.items()}

    subset_manifest_rows = {
        subset: [_subset_manifest_row(public_metadata[sid]) for sid in ids]
        for subset, ids in subset_ids.items()
    }
    for subset, rows in subset_manifest_rows.items():
        _write(output_root / f"manifests/subsets/{subset}.jsonl", _jsonl_bytes(rows))
    for view, ids in view_ids.items():
        _write(
            output_root / f"manifests/views/{view}.jsonl",
            _jsonl_bytes(_view_row(view, public_metadata[sid]) for sid in ids),
        )

    view_checksums = {
        view: _sha256(output_root / f"manifests/views/{view}.jsonl") for view in EXPECTED_VIEWS
    }
    subset_checksums = {
        subset: _sha256(output_root / f"manifests/subsets/{subset}.jsonl") for subset in EXPECTED_SUBSETS
    }
    package_manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "publication_status": PUBLICATION_STATUS,
        "dataset_name": DATASET_NAME,
        "builder_version": BUILDER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "sample_count": 18624,
        "subset_counts": EXPECTED_SUBSETS,
        "logical_view_counts": EXPECTED_VIEWS,
        "label_rule_routing": {"merge": merge_count, "default": default_count},
        "asset_counts": {kind: 18624 for kind in ASSET_KINDS},
        "subset_manifest_checksums": subset_checksums,
        "view_manifest_checksums": view_checksums,
        "sha256sums_excludes_itself": True,
    }
    _write(output_root / "package_manifest.json", _json_bytes(package_manifest))

    label_rule = {
        "generation_time_merge": True,
        "rule_version": LABEL_RULE_MAP["benchmark_v3_lane_physical_marking_cross_segment_merge_0.1"],
        "default_rule_version": LABEL_RULE_MAP["historical_v2_r4_project_then_full_polyline_dedup_default"],
        "confirmed_sites": 24, "merge_samples": merge_count, "default_samples": default_count,
        "special_site": "site_a0973528659b89f0",
        "special_drop_source_ref": [11, 0, -2, "right", "Solid"],
        "offline_override_fix": "none_merged_at_generation",
    }
    card = _dataset_card(run_manifest, run_status, public_metadata, subset_ids, view_ids, opendrive, label_rule, _sha256(output_root / "package_manifest.json"))
    _write(output_root / "dataset_card.json", _json_bytes(card))
    _write(output_root / "README.md", _profile_readme().encode("utf-8"))
    _write(output_root / "README_CN.md", _profile_readme_cn().encode("utf-8"))
    _write(output_root / "LICENSE", _profile_license().encode("utf-8"))

    expected_files = {
        "README.md", "README_CN.md", "LICENSE", "dataset_card.json", "package_manifest.json",
        *(f"manifests/subsets/{name}.jsonl" for name in EXPECTED_SUBSETS),
        *(f"manifests/views/{name}.jsonl" for name in EXPECTED_VIEWS),
        *(path for metadata in public_metadata.values() for path in metadata["paths"].values()),
    }
    actual_payload = {
        path.relative_to(output_root).as_posix()
        for path in _iter_package_files(output_root) if path.name != "SHA256SUMS"
    }
    if actual_payload != expected_files:
        extra = sorted(actual_payload - expected_files)[:10]
        missing = sorted(expected_files - actual_payload)[:10]
        raise BuildError(f"package payload inventory mismatch; extra={extra}, missing={missing}")
    for relative in actual_payload:
        lowered = relative.lower()
        if any(part in lowered for part in FORBIDDEN_PATH_PARTS):
            raise BuildError(f"forbidden development artifact in package: {relative}")

    checksum_rows = [f"{_sha256(output_root / relative)}  {relative}\n" for relative in sorted(actual_payload)]
    _write(output_root / "SHA256SUMS", "".join(checksum_rows).encode("utf-8"))
    for line in (output_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        expected_sha, relative = line.split("  ", 1)
        if relative == "SHA256SUMS" or _sha256(output_root / relative) != expected_sha:
            raise BuildError(f"SHA256SUMS verification failed: {relative}")

    for subset, rows in subset_manifest_rows.items():
        for row in rows:
            if row["sample_id"] not in public_metadata:
                raise BuildError(f"unresolvable subset manifest sample: {row['sample_id']}")
            for kind in ASSET_KINDS:
                if not (output_root / row[kind if kind != "road_mask_visible" else "road_mask"]).is_file():
                    raise BuildError(f"unresolvable asset in {subset} manifest: {row['sample_id']} {kind}")
    for ids in view_ids.values():
        if any(sid not in public_metadata for sid in ids):
            raise BuildError("unresolvable sample in view manifest")

    current_text = _text_checksums(output_root)
    deterministic_rerun = bool(previous_text) and previous_text == current_text
    stable_text_paths = {
        relative: checksum for relative, checksum in current_text.items()
        if relative == "SHA256SUMS" or relative.startswith("manifests/")
        or "/" not in relative
    }
    report = {
        "schema_version": "benchmark_v3_package_report_1.0",
        "publication_status": PUBLICATION_STATUS,
        "package_path": str(output_root),
        "source_run_path": str(run_dir),
        "builder_version": BUILDER_VERSION,
        "package_timestamp": timestamp,
        "package_manifest_sha256": _sha256(output_root / "package_manifest.json"),
        "sha256sums_sha256": _sha256(output_root / "SHA256SUMS"),
        "payload_file_count": len(actual_payload),
        "directory_counts": {"samples": 18624, "assets_per_sample": 4, "subset_manifests": 3, "view_manifests": 4},
        "gates": {
            "sample_and_membership_counts": "pass",
            "four_assets_per_sample": "pass",
            "rgb_checksum_mismatch_count": rgb_mismatches,
            "road_mask_checksum_mismatch_count": mask_mismatches,
            "lane_label_checksum_mismatch_count": label_mismatches,
            "label_rule_routing": {"merge": merge_count, "default": default_count},
            "metadata_whitelist": "pass",
            "internal_absolute_paths": 0,
            "development_artifacts": 0,
            "sha256sums_check": "pass",
            "manifest_assets_resolvable": "pass",
            "deterministic_text_rerun": deterministic_rerun,
        },
        "text_payload_file_count": len(current_text),
        "text_payload_inventory_sha256": _canonical_sha256(current_text),
        "root_and_manifest_text_checksums": stable_text_paths,
        "source_run_manifest_sha256": _sha256(run_dir / "run_manifest.json"),
        "carla_server_started": False,
        "data_generated_or_captured": False,
        "training_or_evaluation_run": False,
        "source_artifacts_modified": False,
    }
    _write(report_path, _json_bytes(report))
    return report


def _finalized_timestamp(run_dir: Path) -> str | None:
    for line in reversed((run_dir / "execution_breadcrumbs.jsonl").read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event") == "capture_finalized":
            return str(event.get("timestamp"))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="capture run directory")
    parser.add_argument("--output-root", type=Path, required=True, help="dataset package directory")
    parser.add_argument("--build-report", type=Path, help="defaults to <output-root>/_work/package_report.json")
    parser.add_argument("--package-timestamp", help="optional fixed ISO-8601 timestamp for the report")
    args = parser.parse_args()
    report_path = args.build_report or (args.output_root / "_work" / "package_report.json")
    try:
        report = build(args.run_dir, args.output_root, report_path, args.package_timestamp)
    except (BuildError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
