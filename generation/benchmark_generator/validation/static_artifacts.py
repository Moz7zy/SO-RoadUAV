"""Lightweight schema-specific validators for Phase A-C artifacts.

The project intentionally does not depend on the external ``jsonschema`` package
for these static gates. These validators enforce the protocol invariants that
matter before CARLA-generated artifacts are allowed.
"""

from __future__ import annotations

import math
from typing import Any


ALLOWED_SOURCE_TYPES = {
    "platform_direct",
    "program_derived",
    "proxy",
    "not_available",
    "future_extension",
}
ALLOWED_LABEL_ROLES = {
    "official_prediction_target",
    "proxy_label",
    "metadata",
    "not_available",
    "future_extension",
}
ALLOWED_AVAILABILITY = {"available", "not_available", "future_extension"}
REQUIRED_LABELS = {
    "road_mask_full",
    "road_boundary_full",
    "road_heading",
    "lane_polyline_projected",
    "semantic_road_mask_proxy",
}
EXPECTED_LABEL_MAPPING = {
    "road_mask_full": "labels.road_mask_full.path",
    "road_boundary_full": "labels.road_boundary_full",
    "road_heading": "labels.road_heading",
    "lane_polyline_projected": "labels.lane_polyline_projected.instances",
    "semantic_road_mask_proxy": "labels.semantic_road_mask_proxy.path",
}
LANE_MASK_MAX_OUTSIDE_FRACTION = 0.02
NEAR_DUPLICATE_POLYLINE_DISTANCE_PX = 3.0


class ValidationError(AssertionError):
    """Raised when a static artifact violates the Phase A-C contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _require_keys(obj: dict[str, Any], keys: set[str], context: str) -> None:
    missing = sorted(keys - set(obj))
    _require(not missing, f"{context} missing required keys: {missing}")


def _validate_source_type(value: str, context: str) -> None:
    _require(value in ALLOWED_SOURCE_TYPES, f"{context} has invalid source_type: {value!r}")


def _validate_label_role(value: str, context: str) -> None:
    _require(value in ALLOWED_LABEL_ROLES, f"{context} has invalid label_role: {value!r}")


def _validate_availability(value: str, context: str) -> None:
    _require(value in ALLOWED_AVAILABILITY, f"{context} has invalid availability: {value!r}")


def _validate_not_available_reason(label: dict[str, Any], context: str, data_key: str = "path") -> None:
    if label.get(data_key) is None or label.get("availability") in {"not_available", "future_extension"}:
        reason = label.get("not_available_reason")
        _require(isinstance(reason, str) and bool(reason.strip()), f"{context} needs not_available_reason")


def _has_payload(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return bool(value)
    return True


def _validate_label_state(
    label: dict[str, Any],
    context: str,
    productive_source_type: str,
    payload_keys: tuple[str, ...],
) -> None:
    source_type = label.get("source_type")
    availability = label.get("availability")
    payload_present = any(_has_payload(label.get(key)) for key in payload_keys)

    if availability == "available":
        _require(
            source_type == productive_source_type,
            f"{context} available label must use {productive_source_type} source_type",
        )
        _require(payload_present, f"{context} available label must include output payload")
        _require("not_available_reason" not in label, f"{context} available label must not carry not_available_reason")
        return

    if source_type in {"not_available", "future_extension"}:
        _require(availability == source_type, f"{context} source_type and availability must match")
    if availability in {"not_available", "future_extension"}:
        _require(
            source_type == availability,
            f"{context} unavailable/future label must use matching source_type",
        )
        _require(not payload_present, f"{context} unavailable/future label must not include output payload")
        _validate_not_available_reason(label, context)


def _geometry_key(points: list[Any]) -> tuple[tuple[float, float], ...]:
    key: list[tuple[float, float]] = []
    for point in points:
        _require(isinstance(point, list) and len(point) >= 2, "lane geometry point must be [x, y]")
        key.append((round(float(point[0]), 1), round(float(point[1]), 1)))
    return tuple(key)


def _lane_groups(lane_ids: list[Any]) -> list[list[int]]:
    ids = sorted(int(lane_id) for lane_id in lane_ids)
    if not ids:
        return []
    groups: list[list[int]] = [[ids[0]]]
    for lane_id in ids[1:]:
        prev = groups[-1][-1]
        adjacent = (lane_id - prev == 1) or (prev == -1 and lane_id == 1)
        if adjacent:
            groups[-1].append(lane_id)
        else:
            groups.append([lane_id])
    return groups


def _expected_physical_marking_count_from_lane_ids(lane_ids: list[Any]) -> int | None:
    if not lane_ids:
        return None
    groups = _lane_groups(lane_ids)
    return len(lane_ids) + max(1, len(groups))


def _polylines_near_duplicate(points_a: list[Any], points_b: list[Any]) -> bool:
    if len(points_a) < 2 or len(points_b) < 2:
        return False
    samples_a = _resample_polyline(points_a, 16)
    samples_b = _resample_polyline(points_b, 16)
    forward = _mean_point_distance(samples_a, samples_b)
    reverse = _mean_point_distance(samples_a, list(reversed(samples_b)))
    return min(forward, reverse) <= NEAR_DUPLICATE_POLYLINE_DISTANCE_PX


def _resample_polyline(points: list[Any], sample_count: int) -> list[list[float]]:
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        lengths.append(lengths[-1] + math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1])))
    total = lengths[-1]
    if total <= 1e-6:
        return [[float(points[0][0]), float(points[0][1])] for _ in range(sample_count)]
    samples: list[list[float]] = []
    for idx in range(sample_count):
        target = total * idx / float(sample_count - 1)
        seg_idx = 0
        while seg_idx < len(lengths) - 2 and lengths[seg_idx + 1] < target:
            seg_idx += 1
        start = points[seg_idx]
        end = points[seg_idx + 1]
        seg_len = max(1e-6, lengths[seg_idx + 1] - lengths[seg_idx])
        alpha = (target - lengths[seg_idx]) / seg_len
        samples.append(
            [
                float(start[0]) + alpha * (float(end[0]) - float(start[0])),
                float(start[1]) + alpha * (float(end[1]) - float(start[1])),
            ]
        )
    return samples


def _mean_point_distance(points_a: list[list[float]], points_b: list[list[float]]) -> float:
    return sum(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(points_a, points_b)) / len(points_a)


def _excluded_reason_counts(excluded_specs: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for spec in excluded_specs:
        if not isinstance(spec, dict):
            continue
        for reason in spec.get("rejection_reasons", []):
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return counts


def _validate_excluded_specs(artifact: dict[str, Any], context: str) -> None:
    excluded_specs = artifact.get("excluded_specs")
    excluded_count = artifact.get("excluded_spec_count")
    excluded_reason_counts = artifact.get("excluded_reason_counts")
    _require(isinstance(excluded_specs, list), f"{context} missing excluded_specs list")
    _require(excluded_count == len(excluded_specs), f"{context} excluded_spec_count drift")
    expected_counts = _excluded_reason_counts(excluded_specs)
    _require(excluded_reason_counts == expected_counts, f"{context} excluded_reason_counts drift")
    for idx, spec in enumerate(excluded_specs):
        _require(isinstance(spec, dict), f"{context}.excluded_specs[{idx}] must be an object")
        _require(isinstance(spec.get("corridor_id"), str) and spec["corridor_id"], f"{context}.excluded_specs[{idx}] missing corridor_id")
        reasons = spec.get("rejection_reasons")
        _require(
            isinstance(reasons, list) and all(isinstance(reason, str) and reason for reason in reasons),
            f"{context}.excluded_specs[{idx}] missing rejection_reasons",
        )


def _validate_planned_corridors(artifact: dict[str, Any], context: str) -> None:
    planned_corridors = artifact.get("planned_corridors")
    planned_count = artifact.get("planned_corridor_count")
    _require(isinstance(planned_corridors, list) and planned_corridors, f"{context} missing planned_corridors list")
    _require(planned_count == len(planned_corridors), f"{context} planned_corridor_count drift")
    for idx, corridor in enumerate(planned_corridors):
        item_context = f"{context}.planned_corridors[{idx}]"
        _require(isinstance(corridor, dict), f"{item_context} must be an object")
        _require(isinstance(corridor.get("corridor_id"), str) and corridor["corridor_id"], f"{item_context} missing corridor_id")
        _require(isinstance(corridor.get("road_id"), int), f"{item_context} missing road_id")
        _require(isinstance(corridor.get("section_id"), int), f"{item_context} missing section_id")
        lane_ids = corridor.get("lane_ids")
        _require(isinstance(lane_ids, list) and lane_ids, f"{item_context} missing lane_ids")
        groups = _lane_groups(lane_ids)
        _require(corridor.get("lane_group_count") == max(1, len(groups)), f"{item_context} lane_group_count drift")
        _require(corridor.get("lane_groups") == groups, f"{item_context} lane_groups drift")


def _validate_lane_coordinate_domain(points: list[Any], context: str) -> None:
    for point in points:
        x = float(point[0])
        y = float(point[1])
        _require(math.isfinite(x) and math.isfinite(y), f"{context} has non-finite point")
        _require(-80.0 <= x < 1360.0 and -80.0 <= y < 800.0, f"{context} point exceeds declared 80px margin")


def _validate_road_boundary_components(road_boundary: dict[str, Any], lane_ids: Any) -> None:
    _require(isinstance(lane_ids, list) and lane_ids, "road_boundary_full requires map.lane_ids for component validation")
    expected_groups = _lane_groups(lane_ids)
    expected_count = max(1, len(expected_groups))
    components = road_boundary.get("components")
    _require(isinstance(components, list), "road_boundary_full missing components")
    _require(len(components) == expected_count, "road_boundary_full component count must match lane_group_count")
    actual_groups = []
    for idx, component in enumerate(components):
        context = f"road_boundary_full.components[{idx}]"
        _require(isinstance(component, dict), f"{context} must be an object")
        _require(isinstance(component.get("component_id"), str) and component["component_id"], f"{context} missing component_id")
        lane_group = component.get("lane_group")
        _require(isinstance(lane_group, list) and lane_group, f"{context} missing lane_group")
        actual_groups.append([int(lane_id) for lane_id in lane_group])
        for key in ("left", "right"):
            value = component.get(key)
            _require(isinstance(value, list) and len(value) >= 2, f"{context}.{key} must be a non-empty polyline")
        polygon = component.get("polygon")
        _require(isinstance(polygon, list) and len(polygon) >= 3, f"{context}.polygon must be a valid polygon")
    _require(actual_groups == expected_groups, "road_boundary_full component lane groups drift")
    if expected_count == 1:
        _require(road_boundary.get("left") == components[0]["left"], "single-group road_boundary_full left drift")
        _require(road_boundary.get("right") == components[0]["right"], "single-group road_boundary_full right drift")
    else:
        _require(road_boundary.get("left") == [], "multi-group road_boundary_full top-level left must be empty")
        _require(road_boundary.get("right") == [], "multi-group road_boundary_full top-level right must be empty")


def _validate_common_label(
    label: dict[str, Any],
    protocol_name: str,
    role: str,
    source_types: set[str],
    context: str,
) -> None:
    _require(label.get("protocol_name") == protocol_name, f"{context} protocol_name drift")
    _validate_label_role(label.get("label_role"), context)
    _require(label.get("label_role") == role, f"{context} label_role drift")
    _validate_source_type(label.get("source_type"), context)
    _require(label.get("source_type") in source_types, f"{context} source_type not allowed")
    _validate_availability(label.get("availability"), context)


def validate_frame_label(artifact: dict[str, Any]) -> None:
    _require(artifact.get("schema_version") == "benchmark_v1_frame_label_0.1", "frame schema_version mismatch")
    _require_keys(artifact, {"camera", "environment", "labels", "sample_metadata", "qa"}, "frame")
    _require("mount_mode" in artifact["camera"], "frame.camera missing mount_mode")
    _require("weather_preset" in artifact["environment"], "frame.environment missing weather_preset")

    labels = artifact["labels"]
    _require(set(labels) == REQUIRED_LABELS, f"frame.labels unexpected fields: {sorted(set(labels) - REQUIRED_LABELS)}")

    road_mask = labels["road_mask_full"]
    _validate_common_label(
        road_mask,
        "road_mask_full",
        "official_prediction_target",
        {"program_derived", "not_available", "future_extension"},
        "labels.road_mask_full",
    )
    _require(road_mask.get("storage_path_field") == "road_mask_full_path", "road_mask_full storage mapping drift")
    _validate_label_state(road_mask, "labels.road_mask_full", "program_derived", ("path",))

    road_boundary = labels["road_boundary_full"]
    _validate_common_label(
        road_boundary,
        "road_boundary_full",
        "official_prediction_target",
        {"program_derived", "not_available", "future_extension"},
        "labels.road_boundary_full",
    )
    _require(road_boundary.get("storage_data_field") == "road_boundary_full", "road_boundary_full mapping drift")
    _require("left" in road_boundary and "right" in road_boundary, "road_boundary_full missing boundary arrays")
    _validate_label_state(road_boundary, "labels.road_boundary_full", "program_derived", ("left", "right", "components"))
    if road_boundary.get("availability") == "available":
        _validate_road_boundary_components(road_boundary, artifact.get("map", {}).get("lane_ids", []))

    road_heading = labels["road_heading"]
    _validate_common_label(
        road_heading,
        "road_heading",
        "official_prediction_target",
        {"program_derived", "not_available", "future_extension"},
        "labels.road_heading",
    )
    _require(road_heading.get("storage_data_field") == "road_heading", "road_heading mapping drift")
    _validate_label_state(
        road_heading,
        "labels.road_heading",
        "program_derived",
        ("world_yaw_deg", "image_angle_deg", "image_vector"),
    )

    lane = labels["lane_polyline_projected"]
    _validate_common_label(
        lane,
        "lane_polyline_projected",
        "official_prediction_target",
        {"program_derived", "not_available", "future_extension"},
        "labels.lane_polyline_projected",
    )
    _require(lane.get("storage_data_field") == "lane_polylines_projected", "lane polyline mapping drift")
    _require("instances" in lane and isinstance(lane["instances"], list), "lane polyline missing instances")
    _validate_label_state(lane, "labels.lane_polyline_projected", "program_derived", ("instances",))
    for idx, instance in enumerate(lane["instances"]):
        context = f"labels.lane_polyline_projected.instances[{idx}]"
        _require(instance.get("source_type") == "program_derived", f"{context} source_type must be program_derived")
        _require(
            instance.get("source_detail") in {"opendrive_waypoint_lane_marking", "opendrive_lane_boundary"},
            f"{context} source_detail is not approved",
        )
        _require(
            instance.get("visibility_semantics") == "projected_in_image_no_occlusion_test",
            f"{context} must not claim true visible semantics",
        )
        _require(
            instance.get("instance_semantics") == "unique_physical_marking",
            f"{context} must declare unique physical marking semantics",
        )
        _require(
            isinstance(instance.get("physical_marking_key"), str) and bool(instance["physical_marking_key"].strip()),
            f"{context} missing physical_marking_key",
        )
        refs = instance.get("source_lane_side_refs")
        _require(isinstance(refs, list) and refs, f"{context} missing source_lane_side_refs")
        _validate_lane_coordinate_domain(instance.get("points_image", []), context)
    lane_keys = [_geometry_key(instance.get("points_image", [])) for instance in lane["instances"]]
    _require(len(lane_keys) == len(set(lane_keys)), "lane_polyline_projected has duplicate physical geometry")
    for idx, instance in enumerate(lane["instances"]):
        for other in lane["instances"][idx + 1 :]:
            _require(
                not _polylines_near_duplicate(instance.get("points_image", []), other.get("points_image", [])),
                "lane_polyline_projected has near-duplicate physical geometry",
            )
    physical_keys = [instance.get("physical_marking_key") for instance in lane["instances"]]
    _require(len(physical_keys) == len(set(physical_keys)), "lane_polyline_projected physical_marking_key must be unique")

    proxy = labels["semantic_road_mask_proxy"]
    _validate_common_label(
        proxy,
        "semantic_road_mask_proxy",
        "proxy_label",
        {"proxy", "not_available", "future_extension"},
        "labels.semantic_road_mask_proxy",
    )
    _require(proxy.get("storage_path_field") == "semantic_road_mask_proxy_path", "semantic proxy mapping drift")
    _validate_label_state(proxy, "labels.semantic_road_mask_proxy", "proxy", ("path",))

    metadata = artifact["sample_metadata"]
    _require_keys(metadata, {"label_sources", "label_field_mapping", "source_policy"}, "sample_metadata")
    _require(set(metadata["label_sources"]) == REQUIRED_LABELS, "sample_metadata.label_sources drift")
    for name, source_type in metadata["label_sources"].items():
        _validate_source_type(source_type, f"sample_metadata.label_sources.{name}")
        _require(
            source_type == labels[name].get("source_type"),
            f"sample_metadata.label_sources.{name} must match labels.{name}.source_type",
        )
    _require(metadata["label_field_mapping"] == EXPECTED_LABEL_MAPPING, "sample_metadata.label_field_mapping drift")
    source_policy = metadata["source_policy"]
    _require(
        source_policy.get("lane_polyline_instance_semantics") == "unique_physical_marking",
        "sample_metadata.source_policy must declare unique physical marking lane semantics",
    )
    _require(
        source_policy.get("road_edge_marking_policy") == "included_once_as_unique_physical_marking",
        "sample_metadata.source_policy must declare road edge marking policy",
    )
    expected_count = source_policy.get("expected_physical_marking_count")
    if expected_count is None and source_policy.get("road_edge_marking_policy") == "included_once_as_unique_physical_marking":
        lane_ids = artifact.get("map", {}).get("lane_ids", [])
        if isinstance(lane_ids, list) and lane_ids:
            expected_count = _expected_physical_marking_count_from_lane_ids(lane_ids)
    if expected_count is not None:
        _require(
            isinstance(expected_count, int) and expected_count > 0,
            "sample_metadata.source_policy expected_physical_marking_count must be a positive integer",
        )
        lane_ids = artifact.get("map", {}).get("lane_ids", [])
        derived_expected = _expected_physical_marking_count_from_lane_ids(lane_ids) if isinstance(lane_ids, list) else None
        if derived_expected is not None:
            _require(
                expected_count == derived_expected,
                "sample_metadata.source_policy expected_physical_marking_count must be lane-group-aware",
            )
        _require(
            len(lane["instances"]) >= expected_count,
            "lane_polyline_projected has fewer instances than expected physical marking count",
        )
    lane_meta = metadata.get("lane_polyline_projected_meta")
    if lane.get("availability") == "available":
        _require(isinstance(lane_meta, dict), "lane_polyline_projected_meta must be an object")
        meta_expected = lane_meta.get("expected_physical_marking_count")
        meta_actual = lane_meta.get("actual_physical_marking_count")
        _require(meta_expected == expected_count, "lane_polyline_projected_meta expected count drift")
        _require(meta_actual == len(lane["instances"]), "lane_polyline_projected_meta actual count drift")
        _require(
            lane_meta.get("coverage_status") == "pass",
            "lane_polyline_projected_meta coverage_status must pass for accepted labels",
        )
        lane_ids = artifact.get("map", {}).get("lane_ids", [])
        if isinstance(lane_ids, list) and lane_ids:
            groups = _lane_groups(lane_ids)
            _require(
                lane_meta.get("lane_group_count") == max(1, len(groups)),
                "lane_polyline_projected_meta lane group count drift",
            )
            _require(lane_meta.get("lane_groups") == groups, "lane_polyline_projected_meta lane groups drift")
        containment = lane_meta.get("road_mask_containment")
        _require(isinstance(containment, dict), "lane_polyline_projected_meta missing road_mask_containment")
        _require(containment.get("status") == "pass", "lane_polyline_projected road mask containment must pass")
        max_outside = float(containment.get("max_outside_fraction", LANE_MASK_MAX_OUTSIDE_FRACTION))
        outside_fraction = float(containment.get("lane_outside_road_mask_fraction", 1.0))
        _require(max_outside <= LANE_MASK_MAX_OUTSIDE_FRACTION, "lane containment threshold exceeds protocol maximum")
        _require(
            outside_fraction <= max_outside,
            "lane_polyline_projected outside road_mask_full exceeds containment threshold",
        )
    semantic_meta = metadata.get("semantic_road_mask_proxy_meta")
    if labels["semantic_road_mask_proxy"].get("availability") == "available":
        _require(isinstance(semantic_meta, dict), "sample_metadata missing semantic proxy decode metadata")
        _require(
            semantic_meta.get("mask_source") == "carla_semantic_raw_label_ids",
            "semantic proxy mask_source must be raw CARLA label IDs",
        )
        accepted_ids = semantic_meta.get("accepted_class_ids")
        _require(isinstance(accepted_ids, list), "semantic proxy accepted_class_ids missing")
        accepted = {item.get("name"): item.get("id") for item in accepted_ids if isinstance(item, dict)}
        _require(accepted == {"Roads": 1, "RoadLines": 24}, f"semantic proxy accepted IDs drift: {accepted}")
        _require(semantic_meta.get("palette_fallback_used") is False, "semantic proxy must not use palette fallback")
        _require(
            "selected_channel_class_histogram" in semantic_meta,
            "semantic proxy missing selected channel class histogram",
        )
        extra_fraction = float(semantic_meta.get("extra_fraction_of_semantic", 0.0))
        _require(0.0 <= extra_fraction <= 1.0, "semantic proxy extra area fraction is out of range")


def validate_run_manifest(artifact: dict[str, Any]) -> None:
    _require(artifact.get("schema_version") == "benchmark_v1_run_manifest_0.1", "run manifest schema_version mismatch")
    _require("weather_preset" in artifact.get("environment", {}), "run manifest missing environment.weather_preset")
    _require("mount_mode" in artifact.get("camera", {}), "run manifest missing camera.mount_mode")
    implemented = set(artifact.get("implemented_fields", []))
    required = REQUIRED_LABELS | {"weather_preset", "camera_mount_mode", "sample_metadata", "qa_summary"}
    missing = sorted(required - implemented)
    _require(not missing, f"run manifest missing implemented field declarations: {missing}")
    command_boundary = artifact.get("command_boundary", {})
    if command_boundary.get("carla_connection") in {"approved_phase_d_devtest_used", "approved_v1rc_smoke_used"}:
        _validate_excluded_specs(artifact, "run_manifest")
    if artifact.get("run_stage") == "v1rc_smoke":
        _validate_planned_corridors(artifact, "run_manifest")
        _validate_excluded_specs(artifact, "run_manifest")
        _require(artifact.get("excluded_spec_count", 0) > 0, "v1rc_smoke run_manifest must preserve exclusion audit")


def validate_qa_summary(artifact: dict[str, Any]) -> None:
    _require(artifact.get("schema_version") == "benchmark_v1_qa_summary_0.1", "QA schema_version mismatch")
    _require("weather_preset" in artifact.get("environment", {}), "QA missing environment.weather_preset")
    _require("mount_mode" in artifact.get("camera", {}), "QA missing camera.mount_mode")
    _require(artifact.get("status") in {"pass", "warn", "fail", "not_run"}, "QA status has invalid vocabulary")
    checks = set(artifact.get("required_checks", {}))
    required = REQUIRED_LABELS | {"sample_metadata", "qa_summary"}
    missing = sorted(required - checks)
    _require(not missing, f"QA missing required checks: {missing}")
    frame_counts = artifact.get("frame_counts", {})
    _require(set(frame_counts) >= {"total", "pass", "warn", "reject", "not_run"}, "QA frame_counts missing fields")
    if artifact.get("status") in {"pass", "warn"}:
        _require(
            "unique_physical_marking" in artifact["required_checks"]["lane_polyline_projected"],
            "QA summary lane check must document unique physical marking duplicate check",
        )
        _require(
            "verified_road_roadline_ids" in artifact["required_checks"]["semantic_road_mask_proxy"],
            "QA summary semantic check must document verified road/roadline ID decode",
        )
    if artifact.get("status") == "pass":
        _require(artifact.get("frame_counts", {}).get("reject", 0) == 0, "QA pass cannot include rejected frames")
        _require(artifact.get("frame_counts", {}).get("warn", 0) == 0, "QA pass cannot include warning frames")
    if artifact.get("status") == "warn":
        _require(frame_counts.get("reject", 0) == 0, "QA warn cannot include rejected frames")
        _require(frame_counts.get("warn", 0) > 0, "QA warn must include warning frames")
        _require(artifact.get("warning_reasons"), "QA warn must surface warning reasons")
    if artifact.get("status") == "fail":
        _require(frame_counts.get("reject", 0) > 0, "QA fail must include rejected frames")

    qa_scope = artifact.get("qa_scope", {})
    if qa_scope.get("mode") in {"phase_d_approved_devtest", "v1rc_smoke_approved_run"}:
        _require(
            qa_scope.get("carla_connection") in {"approved_phase_d_devtest_used", "approved_v1rc_smoke_used"},
            "approved QA summary must record CARLA connection boundary",
        )
        _require(
            qa_scope.get("frame_generation") in {"approved_phase_d_devtest_used", "approved_v1rc_smoke_used"},
            "approved QA summary must record frame generation boundary",
        )
        _require(isinstance(qa_scope.get("run_dir"), str) and qa_scope["run_dir"], "approved QA summary missing run_dir")
        _require(
            isinstance(qa_scope.get("contact_sheet"), str) and qa_scope["contact_sheet"],
            "approved QA summary missing contact_sheet",
        )
        thresholds = artifact.get("qa_thresholds", {})
        _require(
            "semantic_extra_fraction_of_semantic_warn_gt" in thresholds,
            "approved QA summary missing semantic extra warning threshold",
        )
        _require(
            "road_mask_full_outside_semantic_proxy_warn_gt" in thresholds,
            "approved QA summary missing road mask over-semantic warning threshold",
        )
        _require(
            "semantic_extra_fraction_of_semantic_reject_gt" not in thresholds,
            "approved QA summary still uses stale semantic extra reject threshold",
        )
        _validate_excluded_specs(artifact, "qa_summary")
    if qa_scope.get("mode") == "v1rc_smoke_preflight":
        _validate_planned_corridors(artifact, "qa_summary")
        _validate_excluded_specs(artifact, "qa_summary")
        _require(artifact.get("excluded_spec_count", 0) > 0, "v1rc_smoke QA must preserve exclusion audit")
    if qa_scope.get("mode") == "v1rc_smoke_approved_run":
        _validate_planned_corridors(artifact, "qa_summary")
        _require(artifact.get("excluded_spec_count", 0) > 0, "v1rc_smoke QA must preserve exclusion audit")
