"""CARLA sensor, projection and overlay helpers used by the capture worker.
It keeps the first real benchmark_v1 generation pass deliberately small:
Town04_Opt, traffic=none, at most 32 static corridor-centered RGB frames.
"""

from __future__ import annotations

import json
import math
import queue
import hashlib
import time
from itertools import product
from pathlib import Path
from typing import Any, Iterable

from benchmark_generator.config import ARTIFACTS_ROOT, ConfigError, ensure_path_within
from benchmark_generator.manifests import build_preflight_run_manifest
from benchmark_generator.opendrive.segment_registry import build_segment_registry
from benchmark_generator.qa import expected_contact_sheet_layers
from benchmark_generator.validation import (
    validate_frame_label,
    validate_qa_summary,
    validate_run_manifest,
)


IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 720
VERIFIED_ROAD_LABEL_IDS = {"Roads": 1, "RoadLines": 24}
ROAD_MASK_LABEL_IDS = tuple(VERIFIED_ROAD_LABEL_IDS.values())
CITYSCAPES_ROAD_RGB_COLORS = ((128, 64, 128), (157, 234, 50))
WEATHER_NAMES = {
    "clear_noon": "ClearNoon",
    "cloudy_noon": "CloudyNoon",
    "wet_noon": "WetNoon",
}
MIN_LANE_POINTS = 3
MIN_LANE_ARC_LENGTH_PX = 20.0
VISIBLE_MARGIN_PX = 80.0
LANE_MASK_CONTAINMENT_TOLERANCE_PX = 5
LANE_MASK_MAX_OUTSIDE_FRACTION = 0.02
NEAR_DUPLICATE_POLYLINE_DISTANCE_PX = 3.0


def _execution_semantics(config: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "stage_name": "Phase D devtest",
        "run_id_suffix": "phase_d_devtest",
        "run_status_schema_version": "benchmark_v1_phase_d_run_status_0.1",
        "source_policy_phase": "phase_d_approved_devtest",
        "manifest_run_stage": "phase_d_devtest",
        "carla_connection_boundary": "approved_phase_d_devtest_used",
        "frame_generation_boundary": "approved_phase_d_devtest_used",
        "qa_mode": "phase_d_approved_devtest",
        "camera_grid_name": "town04_opt_phase_d_devtest",
        "script_entrypoint": "BenchmarkProject/tools/run_carla_devtest.py",
        "min_frames": 16,
        "max_frames": 32,
    }
    overrides = config.get("execution_semantics", {})
    if isinstance(overrides, dict):
        defaults.update(overrides)
    return defaults


def _validate_carla_semantic_label_ids(carla: Any) -> None:
    """Reject generation if the imported CARLA API disagrees with local semantic IDs."""

    city_labels = getattr(carla, "CityObjectLabel", None)
    if city_labels is None:
        raise ConfigError("CARLA CityObjectLabel enum is unavailable; cannot verify semantic road IDs")
    actual = {
        "Roads": _enum_int_value(getattr(city_labels, "Roads", None), "Roads"),
        "RoadLines": _enum_int_value(getattr(city_labels, "RoadLines", None), "RoadLines"),
    }
    if actual != VERIFIED_ROAD_LABEL_IDS:
        raise ConfigError(
            "CARLA semantic label ID mismatch for Phase D road proxy: "
            f"expected {VERIFIED_ROAD_LABEL_IDS}, got {actual}"
        )


def _enum_int_value(value: Any, name: str) -> int:
    if value is None:
        raise ConfigError(f"CARLA CityObjectLabel.{name} is unavailable")
    try:
        return int(value)
    except TypeError:
        pass
    for attr in ("value", "id"):
        if hasattr(value, attr):
            return int(getattr(value, attr))
    raise ConfigError(f"CARLA CityObjectLabel.{name} cannot be converted to an integer ID")


def _make_output_dirs(run_dir: Path) -> dict[str, Path]:
    dirs = {
        "root": run_dir,
        "rgb": run_dir / "rgb",
        "road_mask_full": run_dir / "road_mask_full",
        "semantic_road_mask_proxy": run_dir / "semantic_road_mask_proxy",
        "frame_labels": run_dir / "frame_labels",
        "metadata": run_dir / "metadata",
        "qa_overlays": run_dir / "qa_overlays",
    }
    for path in dirs.values():
        ensure_path_within(path, [ARTIFACTS_ROOT], "Phase D output directory")
        path.mkdir(parents=True, exist_ok=False)
    return dirs


def _selected_segments(registry: dict[str, Any], corridor_ids: Iterable[str]) -> list[dict[str, Any]]:
    wanted = list(corridor_ids)
    by_id = {segment["corridor_id"]: segment for segment in registry["segments"]}
    selected = [by_id[corridor_id] for corridor_id in wanted if corridor_id in by_id]
    if len(selected) != len(wanted):
        missing = sorted(set(wanted) - set(by_id))
        raise ConfigError(f"selected corridor ids missing from registry: {missing}")
    return selected


def _candidate_segment_pool(registry: dict[str, Any], priority_corridor_ids: Iterable[str]) -> list[dict[str, Any]]:
    priority = _selected_segments(registry, priority_corridor_ids)
    priority_ids = {segment["corridor_id"] for segment in priority}
    remaining = [
        segment
        for segment in registry["segments"]
        if segment["registry_status"] == "candidate" and segment["corridor_id"] not in priority_ids
    ]
    return priority + remaining


def _select_usable_segments(
    carla: Any,
    waypoint_cache: list[Any],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    selected: list[dict[str, Any]] = []
    lane_boundaries: dict[str, list[dict[str, Any]]] = {}
    for segment in candidates:
        segment = dict(segment)
        centerline = _collect_segment_centerline(waypoint_cache, segment)
        if len(centerline) < 2:
            continue
        segment["_carla_centerline_world"] = centerline
        boundaries = _collect_lane_boundary_segments(carla, waypoint_cache, segment)
        if not boundaries:
            continue
        selected.append(segment)
        lane_boundaries[segment["corridor_id"]] = boundaries
        if len(selected) >= 12:
            break
    if not selected:
        raise ConfigError("no candidate segment has CARLA lane markings for Phase D devtest")
    return selected, lane_boundaries


def _filter_viable_specs(
    carla: Any,
    carla_map: Any,
    specs: list[dict[str, Any]],
    lane_boundaries: dict[str, list[dict[str, Any]]],
    max_frames: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    viable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for spec in specs:
        segment = spec["segment"]
        camera_transform = _camera_transform_for_spec(carla, carla_map, segment, spec)
        lane_instances = _project_lane_instances(
            lane_boundaries[segment["corridor_id"]],
            camera_transform,
            spec["fov_degrees"],
        )
        corridor = _project_corridor(
            carla,
            segment,
            camera_transform,
            spec["fov_degrees"],
            lane_boundaries[segment["corridor_id"]],
        )
        mask = _corridor_mask_array(corridor)
        containment = _lane_mask_containment_from_mask(mask, lane_instances)
        heading = _project_heading(carla, segment, camera_transform, spec["fov_degrees"])
        rejection_reasons: list[str] = []
        if len(corridor["polygon"]) < 3:
            rejection_reasons.append("road_mask_full_polygon_too_small")
        if not _lane_marking_coverage_satisfied(segment, lane_instances):
            rejection_reasons.append("lane_polyline_projected_missing_expected_physical_markings")
        if containment["lane_outside_road_mask_fraction"] > LANE_MASK_MAX_OUTSIDE_FRACTION:
            rejection_reasons.append("lane_polyline_projected_outside_road_mask_full")
        if heading["image_vector"] is None:
            rejection_reasons.append("road_heading_invalid")
        if not rejection_reasons:
            viable.append(spec)
        else:
            excluded.append(_excluded_spec_record(spec, rejection_reasons, lane_instances, containment))
        if len(viable) >= max_frames:
            break
    return viable, excluded


def _excluded_spec_record(
    spec: dict[str, Any],
    rejection_reasons: list[str],
    lane_instances: list[dict[str, Any]],
    containment: dict[str, Any],
) -> dict[str, Any]:
    segment = spec["segment"]
    return {
        "corridor_id": segment["corridor_id"],
        "road_id": segment["road_id"],
        "section_id": segment["section_id"],
        "lane_ids": segment.get("driving_lane_ids", []),
        "height_m": spec["height_m"],
        "pitch_deg": spec["pitch_deg"],
        "yaw_relative_deg": spec["yaw_relative_deg"],
        "lateral_offset_m": spec["lateral_offset_m"],
        "rejection_reasons": rejection_reasons,
        "expected_physical_marking_count": _expected_physical_marking_count(segment),
        "actual_physical_marking_count": len(lane_instances),
        "lane_group_count": _lane_group_count(segment.get("driving_lane_ids", [])),
        "lane_outside_road_mask_fraction": containment["lane_outside_road_mask_fraction"],
    }


def _build_sample_specs(segments: list[dict[str, Any]], grid: dict[str, Any], max_frames: int) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    combos = list(
        product(
            grid["heights_m"],
            grid["pitch_degrees"],
            grid["yaw_relative_degrees"],
            grid["lateral_offsets_m"],
            grid["fov_degrees"],
        )
    )
    for segment in segments:
        for height, pitch, yaw_rel, lateral_offset, fov in combos:
            specs.append(
                {
                    "segment": segment,
                    "height_m": float(height),
                    "pitch_deg": float(pitch),
                    "yaw_relative_deg": float(yaw_rel),
                    "lateral_offset_m": float(lateral_offset),
                    "fov_degrees": float(fov),
                }
            )
            if len(specs) >= max_frames:
                return specs
    return specs


def _load_target_world(client: Any, map_name: str) -> Any:
    world = client.get_world()
    current_map = world.get_map().name.split("/")[-1]
    if current_map != map_name:
        world = client.load_world(map_name)
    return world


def _weather_parameters(carla: Any, name: str) -> Any:
    if name not in WEATHER_NAMES:
        raise ConfigError(f"weather preset is not approved for Phase D devtest: {name}")
    return getattr(carla.WeatherParameters, WEATHER_NAMES[name])


def _camera_blueprint(blueprints: Any, name: str, width: int, height: int, fov: float) -> Any:
    bp = blueprints.find(name)
    bp.set_attribute("image_size_x", str(width))
    bp.set_attribute("image_size_y", str(height))
    bp.set_attribute("fov", str(fov))
    return bp


def _camera_transform_for_spec(carla: Any, carla_map: Any, segment: dict[str, Any], spec: dict[str, Any]) -> Any:
    centerline = _segment_centerline(segment)
    mid_idx = len(centerline) // 2
    x, y, z = centerline[mid_idx]
    heading_deg = _segment_heading_deg(segment)
    normal_x, normal_y = _normal_from_heading(math.radians(heading_deg))
    x += spec["lateral_offset_m"] * normal_x
    y += spec["lateral_offset_m"] * normal_y
    ground_z = _ground_z(carla, carla_map, x, y, z)
    return carla.Transform(
        carla.Location(x=float(x), y=float(y), z=float(ground_z + spec["height_m"])),
        carla.Rotation(
            pitch=float(spec["pitch_deg"]),
            yaw=float(heading_deg + spec["yaw_relative_deg"]),
            roll=0.0,
        ),
    )


def _ground_z(carla: Any, carla_map: Any, x: float, y: float, fallback_z: float) -> float:
    waypoint = carla_map.get_waypoint(
        carla.Location(x=float(x), y=float(y), z=float(fallback_z + 2.0)),
        project_to_road=True,
        lane_type=carla.LaneType.Driving,
    )
    if waypoint is None:
        return float(fallback_z)
    return float(waypoint.transform.location.z)


def _segment_heading_deg(segment: dict[str, Any]) -> float:
    points = _segment_centerline(segment)
    start = points[0]
    end = points[-1]
    return math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))


def _segment_centerline(segment: dict[str, Any]) -> list[list[float]]:
    return segment.get("_carla_centerline_world") or segment["centerline_world"]


def _normal_from_heading(heading_rad: float) -> tuple[float, float]:
    return -math.sin(heading_rad), math.cos(heading_rad)


def _camera_intrinsic(width: int, height: int, fov: float) -> Any:
    import numpy as np  # type: ignore

    focal = width / (2.0 * math.tan(math.radians(fov) / 2.0))
    return np.array([[focal, 0.0, width / 2.0], [0.0, focal, height / 2.0], [0.0, 0.0, 1.0]], dtype=float)


def _project_xyz(x: float, y: float, z: float, world_to_camera: Any, intrinsic: Any) -> tuple[float, float, float] | None:
    import numpy as np  # type: ignore

    point = np.array([x, y, z, 1.0], dtype=float)
    point_camera = world_to_camera @ point
    point_cv = np.array([point_camera[1], -point_camera[2], point_camera[0]], dtype=float)
    if point_cv[2] <= 0.05:
        return None
    point_img = intrinsic @ point_cv
    return float(point_img[0] / point_img[2]), float(point_img[1] / point_img[2]), float(point_cv[2])


def _save_rgb(image: Any, path: Path, width: int, height: int) -> None:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    bgra = np.frombuffer(image.raw_data, dtype=np.uint8).reshape((height, width, 4))
    cv2.imwrite(str(path), bgra[:, :, :3], [int(cv2.IMWRITE_JPEG_QUALITY), 95])


def _save_semantic_proxy(image: Any, path: Path, width: int, height: int) -> dict[str, Any]:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    bgra = np.frombuffer(image.raw_data, dtype=np.uint8).reshape((height, width, 4))
    direct_masks = [np.isin(bgra[:, :, ch], ROAD_MASK_LABEL_IDS) for ch in range(3)]
    direct_counts = [int(mask.sum()) for mask in direct_masks]
    selected_channel = _select_semantic_label_channel(bgra, direct_counts)
    mask = direct_masks[selected_channel]
    color_mask = np.zeros(bgra.shape[:2], dtype=bool)
    color_image = bgra[:, :, :3]
    for rgb_color in CITYSCAPES_ROAD_RGB_COLORS:
        rgb = np.asarray(rgb_color, dtype=np.uint8)
        color_mask |= np.all(color_image == rgb, axis=2)
        color_mask |= np.all(color_image == rgb[::-1], axis=2)
    cv2.imwrite(str(path), mask.astype(np.uint8) * 255)
    return {
        "mask_ratio": round(float(mask.sum()) / float(width * height), 6),
        "mask_source": "carla_semantic_raw_label_ids",
        "decode_policy": "verified_road_and_roadline_ids_only",
        "accepted_class_ids": [{"name": name, "id": value} for name, value in VERIFIED_ROAD_LABEL_IDS.items()],
        "direct_counts": direct_counts,
        "selected_channel": selected_channel,
        "selected_channel_class_histogram": _channel_histogram(bgra[:, :, selected_channel]),
        "palette_match_count": int(color_mask.sum()),
        "palette_fallback_used": False,
    }


def _select_semantic_label_channel(bgra: Any, direct_counts: list[int]) -> int:
    # In CARLA 0.9.15 semantic camera raw output, class IDs are carried in one
    # color channel. Prefer a channel that contains both verified Roads and
    # RoadLines. Fall back to the non-empty verified channel with the largest
    # road/roadline count, but never expand the class ID set.
    import numpy as np  # type: ignore

    road_id = VERIFIED_ROAD_LABEL_IDS["Roads"]
    roadline_id = VERIFIED_ROAD_LABEL_IDS["RoadLines"]
    candidates: list[tuple[int, int, int]] = []
    for channel in range(3):
        values = bgra[:, :, channel]
        road_count = int(np.count_nonzero(values == road_id))
        roadline_count = int(np.count_nonzero(values == roadline_id))
        candidates.append((channel, road_count, roadline_count))
    for channel, road_count, roadline_count in candidates:
        if road_count > 0 and roadline_count > 0:
            return channel
    return int(max(range(3), key=lambda channel: direct_counts[channel]))


def _channel_histogram(channel_values: Any) -> dict[str, int]:
    import numpy as np  # type: ignore

    unique, counts = np.unique(channel_values, return_counts=True)
    return {str(int(value)): int(count) for value, count in zip(unique, counts)}


def _semantic_corridor_overlap(semantic_path: Path, road_mask_path: Path) -> dict[str, float]:
    import cv2  # type: ignore

    semantic = cv2.imread(str(semantic_path), cv2.IMREAD_GRAYSCALE)
    road = cv2.imread(str(road_mask_path), cv2.IMREAD_GRAYSCALE)
    if semantic is None or road is None:
        raise ConfigError("cannot compute semantic/full mask overlap metadata")
    semantic_bool = semantic > 0
    road_bool = road > 0
    extra = semantic_bool & ~road_bool
    missing = road_bool & ~semantic_bool
    semantic_count = int(semantic_bool.sum())
    road_count = int(road_bool.sum())
    return {
        "extra_outside_road_mask_full_ratio": round(float(extra.sum()) / float(IMAGE_WIDTH * IMAGE_HEIGHT), 6),
        "extra_fraction_of_semantic": round(float(extra.sum()) / float(semantic_count), 6) if semantic_count else 0.0,
        "missing_fraction_of_road_mask_full": round(float(missing.sum()) / float(road_count), 6) if road_count else 0.0,
    }


def _project_corridor(
    carla: Any,
    segment: dict[str, Any],
    camera_transform: Any,
    fov: float,
    lane_boundaries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    import numpy as np  # type: ignore

    centerline = _segment_centerline(segment)
    half_width = max(4.0, 0.5 * float(segment["lane_count"]) * 3.75 + 1.5)
    world_to_camera = np.array(camera_transform.get_inverse_matrix(), dtype=float)
    intrinsic = _camera_intrinsic(IMAGE_WIDTH, IMAGE_HEIGHT, fov)
    left_world, right_world = _offset_centerline(centerline, half_width)
    left_img = _project_polyline(left_world, world_to_camera, intrinsic)
    right_img = _project_polyline(right_world, world_to_camera, intrinsic)
    polygon_img = left_img + list(reversed(right_img))
    polygons = [polygon_img] if len(polygon_img) >= 3 else []
    components: list[dict[str, Any]] = []
    if lane_boundaries:
        projected_boundaries = _project_boundary_polylines(lane_boundaries, world_to_camera, intrinsic)
        components = _lane_group_envelope_components(projected_boundaries, segment.get("driving_lane_ids", []))
        if components:
            polygons = [component["polygon"] for component in components]
            largest_component = max(components, key=lambda component: _polygon_area(component["polygon"]))
            polygon_img = largest_component["polygon"]
            if len(components) == 1:
                left_img = components[0]["left"]
                right_img = components[0]["right"]
            else:
                left_img = []
                right_img = []
    return {
        "left": left_img,
        "right": right_img,
        "polygon": polygon_img,
        "polygons": polygons,
        "components": components,
    }


def _project_boundary_polylines(
    lane_boundaries: list[dict[str, Any]],
    world_to_camera: Any,
    intrinsic: Any,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for boundary in lane_boundaries:
        points: list[list[float]] = []
        for location in boundary["locations"]:
            point = _project_xyz(location.x, location.y, location.z, world_to_camera, intrinsic)
            if point is not None:
                points.append([round(point[0], 3), round(point[1], 3)])
        points = _simplify_points(points)
        if len(points) >= 2:
            road_id, section_id, lane_id, side, marking_type = boundary["source_key"]
            projected.append(
                {
                    "road_id": road_id,
                    "section_id": section_id,
                    "lane_id": int(lane_id),
                    "side": side,
                    "marking_type": marking_type,
                    "points_image": points,
                }
            )
    return projected


def _lane_group_envelope_polygons(
    projected_boundaries: list[dict[str, Any]],
    lane_ids: list[int],
) -> list[list[list[float]]]:
    return [component["polygon"] for component in _lane_group_envelope_components(projected_boundaries, lane_ids)]


def _lane_group_envelope_components(
    projected_boundaries: list[dict[str, Any]],
    lane_ids: list[int],
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for group in _lane_groups(lane_ids):
        group_lane_ids = set(group)
        candidates = [item for item in projected_boundaries if int(item["lane_id"]) in group_lane_ids]
        pair = _outer_boundary_pair(candidates)
        if pair is None:
            continue
        first, second = pair
        first_points = first["points_image"]
        second_points = _aligned_polyline(first_points, second["points_image"])
        polygon = first_points + list(reversed(second_points))
        if len(polygon) >= 3 and _polygon_area(polygon) > 1.0:
            components.append(
                {
                    "component_id": f"road_boundary_component_{len(components):04d}",
                    "lane_group": group,
                    "left": first_points,
                    "right": second_points,
                    "polygon": polygon,
                    "source_boundary_refs": [
                        _projected_boundary_ref(first),
                        _projected_boundary_ref(second),
                    ],
                }
            )
    return components


def _projected_boundary_ref(boundary: dict[str, Any]) -> dict[str, Any]:
    return {
        "road_id": boundary.get("road_id"),
        "section_id": boundary.get("section_id"),
        "lane_id": boundary["lane_id"],
        "side": boundary.get("side"),
        "marking_type": boundary.get("marking_type"),
    }


def _outer_boundary_pair(boundaries: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if len(boundaries) < 2:
        return None
    best_pair: tuple[dict[str, Any], dict[str, Any]] | None = None
    best_distance = -1.0
    for idx, first in enumerate(boundaries):
        for second in boundaries[idx + 1 :]:
            distance = _polyline_separation(first["points_image"], second["points_image"])
            if distance > best_distance:
                best_distance = distance
                best_pair = (first, second)
    return best_pair


def _aligned_polyline(reference: list[list[float]], points: list[list[float]]) -> list[list[float]]:
    same_direction = _point_distance(reference[0], points[0]) + _point_distance(reference[-1], points[-1])
    reversed_direction = _point_distance(reference[0], points[-1]) + _point_distance(reference[-1], points[0])
    if reversed_direction < same_direction:
        return list(reversed(points))
    return points


def _polyline_separation(points_a: list[list[float]], points_b: list[list[float]]) -> float:
    samples_a = _resample_polyline(points_a, 16)
    samples_b = _resample_polyline(_aligned_polyline(points_a, points_b), 16)
    return _mean_point_distance(samples_a, samples_b)


def _point_distance(first: list[float], second: list[float]) -> float:
    return math.hypot(float(first[0]) - float(second[0]), float(first[1]) - float(second[1]))


def _polygon_area(polygon: list[list[float]]) -> float:
    if len(polygon) < 3:
        return 0.0
    area = 0.0
    for first, second in zip(polygon, polygon[1:] + polygon[:1]):
        area += float(first[0]) * float(second[1]) - float(second[0]) * float(first[1])
    return abs(area) * 0.5


def _offset_centerline(points: list[list[float]], half_width: float) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    left: list[tuple[float, float, float]] = []
    right: list[tuple[float, float, float]] = []
    for idx, point in enumerate(points):
        prev_point = points[max(0, idx - 1)]
        next_point = points[min(len(points) - 1, idx + 1)]
        heading = math.atan2(next_point[1] - prev_point[1], next_point[0] - prev_point[0])
        normal_x, normal_y = _normal_from_heading(heading)
        x, y, z = point
        left.append((x + normal_x * half_width, y + normal_y * half_width, z + 0.08))
        right.append((x - normal_x * half_width, y - normal_y * half_width, z + 0.08))
    return left, right


def _project_polyline(points: list[tuple[float, float, float]], world_to_camera: Any, intrinsic: Any) -> list[list[float]]:
    projected: list[list[float]] = []
    for x, y, z in points:
        point = _project_xyz(x, y, z, world_to_camera, intrinsic)
        if point is None:
            continue
        projected.append([round(point[0], 3), round(point[1], 3)])
    return projected


def _save_corridor_mask(corridor: dict[str, Any], path: Path) -> dict[str, Any]:
    import cv2  # type: ignore

    mask = _corridor_mask_array(corridor)
    cv2.imwrite(str(path), mask)
    return {"mask_ratio": round(float((mask > 0).sum()) / float(IMAGE_WIDTH * IMAGE_HEIGHT), 6)}


def _corridor_mask_array(corridor: dict[str, Any]) -> Any:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    mask = np.zeros((IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.uint8)
    polygons = corridor.get("polygons") or [corridor["polygon"]]
    for polygon in polygons:
        if len(polygon) >= 3:
            arr = np.asarray([[int(round(x)), int(round(y))] for x, y in polygon], dtype=np.int32)
            cv2.fillPoly(mask, [arr], 255)
    return mask


def _lane_mask_containment(road_mask_path: Path, lane_instances: list[dict[str, Any]]) -> dict[str, Any]:
    import cv2  # type: ignore

    road = cv2.imread(str(road_mask_path), cv2.IMREAD_GRAYSCALE)
    if road is None:
        raise ConfigError(f"cannot read road mask for lane containment: {road_mask_path}")
    return _lane_mask_containment_from_mask(road, lane_instances)


def _lane_mask_containment_from_mask(road_mask: Any, lane_instances: list[dict[str, Any]]) -> dict[str, Any]:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    lane_mask = np.zeros((IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.uint8)
    for instance in lane_instances:
        points = instance.get("points_image", [])
        if len(points) < 2:
            continue
        arr = np.asarray([[int(round(x)), int(round(y))] for x, y in points], dtype=np.int32)
        cv2.polylines(lane_mask, [arr], isClosed=False, color=255, thickness=3)
    kernel_size = 2 * LANE_MASK_CONTAINMENT_TOLERANCE_PX + 1
    kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
    road_dilated = cv2.dilate((road_mask > 0).astype(np.uint8), kernel)
    lane_bool = lane_mask > 0
    outside = lane_bool & (road_dilated == 0)
    lane_pixels = int(lane_bool.sum())
    outside_pixels = int(outside.sum())
    return {
        "lane_mask_containment_tolerance_px": LANE_MASK_CONTAINMENT_TOLERANCE_PX,
        "lane_mask_pixels": lane_pixels,
        "lane_outside_road_mask_pixels": outside_pixels,
        "lane_outside_road_mask_fraction": round(float(outside_pixels) / float(lane_pixels), 6) if lane_pixels else 0.0,
    }


def _collect_lane_boundary_segments(carla: Any, waypoints: list[Any], segment: dict[str, Any]) -> list[dict[str, Any]]:
    boundaries: dict[tuple[Any, ...], list[tuple[float, Any]]] = {}
    lane_ids = set(segment["driving_lane_ids"])
    s_start, s_end = segment["s_range"]
    for waypoint in waypoints:
        if int(waypoint.road_id) != int(segment["road_id"]):
            continue
        if int(waypoint.section_id) != int(segment["section_id"]):
            continue
        if int(waypoint.lane_id) not in lane_ids:
            continue
        if float(waypoint.s) < float(s_start) or float(waypoint.s) > float(s_end):
            continue
        for side, marking in (("left", waypoint.left_lane_marking), ("right", waypoint.right_lane_marking)):
            if not _is_visible_lane_marking(marking, carla):
                continue
            key = (int(waypoint.road_id), int(waypoint.section_id), int(waypoint.lane_id), side, str(marking.type))
            boundaries.setdefault(key, []).append((float(waypoint.s), _boundary_location(carla, waypoint, side)))
    segments: list[dict[str, Any]] = []
    for key, samples in sorted(boundaries.items()):
        locations = [location for _, location in sorted(samples, key=lambda item: item[0])]
        if len(locations) >= 2:
            segments.append({"source_key": key, "locations": locations})
    return segments


def _collect_segment_centerline(waypoints: list[Any], segment: dict[str, Any]) -> list[list[float]]:
    lane_ids = set(segment["driving_lane_ids"])
    s_start, s_end = segment["s_range"]
    buckets: dict[float, list[Any]] = {}
    for waypoint in waypoints:
        if int(waypoint.road_id) != int(segment["road_id"]):
            continue
        if int(waypoint.section_id) != int(segment["section_id"]):
            continue
        if int(waypoint.lane_id) not in lane_ids:
            continue
        if float(waypoint.s) < float(s_start) or float(waypoint.s) > float(s_end):
            continue
        buckets.setdefault(round(float(waypoint.s), 1), []).append(waypoint.transform.location)
    centerline: list[list[float]] = []
    for _s, locations in sorted(buckets.items(), key=lambda item: item[0]):
        if not locations:
            continue
        centerline.append(
            [
                sum(float(location.x) for location in locations) / len(locations),
                sum(float(location.y) for location in locations) / len(locations),
                sum(float(location.z) for location in locations) / len(locations),
            ]
        )
    return centerline


def _is_visible_lane_marking(marking: Any, carla: Any) -> bool:
    if marking is None:
        return False
    return getattr(marking, "type", None) not in (None, carla.LaneMarkingType.NONE, carla.LaneMarkingType.Other)


def _boundary_location(carla: Any, waypoint: Any, side: str) -> Any:
    right_vec = waypoint.transform.get_right_vector()
    sign = -1.0 if side == "left" else 1.0
    center = waypoint.transform.location
    half_width = waypoint.lane_width * 0.5
    return carla.Location(
        x=center.x + sign * right_vec.x * half_width,
        y=center.y + sign * right_vec.y * half_width,
        z=center.z + 0.08,
    )


def _project_lane_instances(boundaries: list[dict[str, Any]], camera_transform: Any, fov: float) -> list[dict[str, Any]]:
    import numpy as np  # type: ignore

    world_to_camera = np.array(camera_transform.get_inverse_matrix(), dtype=float)
    intrinsic = _camera_intrinsic(IMAGE_WIDTH, IMAGE_HEIGHT, fov)
    projected_candidates: list[dict[str, Any]] = []
    for boundary in boundaries:
        points: list[list[float]] = []
        for location in boundary["locations"]:
            projected = _project_xyz(location.x, location.y, location.z, world_to_camera, intrinsic)
            if projected is None:
                continue
            x, y, _depth = projected
            if -VISIBLE_MARGIN_PX <= x < IMAGE_WIDTH + VISIBLE_MARGIN_PX and -VISIBLE_MARGIN_PX <= y < IMAGE_HEIGHT + VISIBLE_MARGIN_PX:
                points.append([round(x, 3), round(y, 3)])
        points = _simplify_points(points)
        if len(points) < MIN_LANE_POINTS or _arc_length(points) < MIN_LANE_ARC_LENGTH_PX:
            continue
        road_id, section_id, lane_id, side, marking_type = boundary["source_key"]
        projected_candidates.append(
            {
                "source_type": "program_derived",
                "source_detail": "opendrive_waypoint_lane_marking",
                "road_id": road_id,
                "section_id": section_id,
                "lane_id": lane_id,
                "side": side,
                "marking_type": marking_type,
                "points_image": points,
                "visibility_semantics": "projected_in_image_no_occlusion_test",
            }
        )
    return _deduplicate_physical_markings(projected_candidates)


def _deduplicate_physical_markings(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: list[dict[str, Any]] = []
    for candidate in candidates:
        group = _matching_physical_marking_group(grouped, candidate)
        if group is None:
            group = {
                "source_type": "program_derived",
                "source_detail": "opendrive_waypoint_lane_marking",
                "road_id": candidate["road_id"],
                "section_id": candidate["section_id"],
                "lane_id": candidate["lane_id"],
                "side": candidate["side"],
                "marking_type": candidate["marking_type"],
                "points_image": candidate["points_image"],
                "visibility_semantics": "projected_in_image_no_occlusion_test",
                "instance_semantics": "unique_physical_marking",
                "physical_marking_key": _physical_marking_key(candidate),
                "source_lane_side_refs": [],
            }
            grouped.append(group)
        group["source_lane_side_refs"].append(_lane_side_ref(candidate))

    instances: list[dict[str, Any]] = []
    for index, item in enumerate(grouped):
        item["instance_id"] = f"lane_marking_{index:04d}"
        instances.append(item)
    return instances


def _matching_physical_marking_group(grouped: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any] | None:
    candidate_key = _geometry_key(candidate["points_image"])
    for item in grouped:
        if candidate_key == _geometry_key(item["points_image"]):
            return item
        if _polylines_near_duplicate(item["points_image"], candidate["points_image"]):
            return item
    return None


def _geometry_key(points: list[list[float]]) -> tuple[tuple[float, float], ...]:
    return tuple((round(float(point[0]), 1), round(float(point[1]), 1)) for point in points)


def _polylines_near_duplicate(points_a: list[list[float]], points_b: list[list[float]]) -> bool:
    if len(points_a) < 2 or len(points_b) < 2:
        return False
    samples_a = _resample_polyline(points_a, 16)
    samples_b = _resample_polyline(points_b, 16)
    forward = _mean_point_distance(samples_a, samples_b)
    reverse = _mean_point_distance(samples_a, list(reversed(samples_b)))
    return min(forward, reverse) <= NEAR_DUPLICATE_POLYLINE_DISTANCE_PX


def _resample_polyline(points: list[list[float]], sample_count: int) -> list[list[float]]:
    if len(points) == 1:
        return [[float(points[0][0]), float(points[0][1])] for _ in range(sample_count)]
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
    if len(points_a) != len(points_b):
        raise ValueError("resampled polylines must have equal lengths")
    return sum(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(points_a, points_b)) / len(points_a)


def _physical_marking_key(candidate: dict[str, Any]) -> str:
    geometry = _geometry_key(candidate["points_image"])
    digest = hashlib.sha1(repr(geometry).encode("utf-8")).hexdigest()[:16]
    return (
        f"road{candidate['road_id']}_sec{candidate['section_id']}_"
        f"type{candidate['marking_type']}_geom{digest}"
    )


def _lane_side_ref(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "road_id": candidate["road_id"],
        "section_id": candidate["section_id"],
        "lane_id": candidate["lane_id"],
        "side": candidate["side"],
        "marking_type": candidate["marking_type"],
    }


def _simplify_points(points: list[list[float]], min_step: float = 2.0) -> list[list[float]]:
    simplified: list[list[float]] = []
    last: list[float] | None = None
    for point in points:
        if last is None or math.hypot(point[0] - last[0], point[1] - last[1]) >= min_step:
            simplified.append(point)
            last = point
    return simplified


def _arc_length(points: list[list[float]]) -> float:
    if len(points) < 2:
        return 0.0
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def _project_heading(carla: Any, segment: dict[str, Any], camera_transform: Any, fov: float) -> dict[str, Any]:
    import numpy as np  # type: ignore

    world_to_camera = np.array(camera_transform.get_inverse_matrix(), dtype=float)
    intrinsic = _camera_intrinsic(IMAGE_WIDTH, IMAGE_HEIGHT, fov)
    points = _segment_centerline(segment)
    start = points[max(0, len(points) // 2 - 1)]
    end = points[min(len(points) - 1, len(points) // 2 + 1)]
    a = _project_xyz(start[0], start[1], start[2] + 0.1, world_to_camera, intrinsic)
    b = _project_xyz(end[0], end[1], end[2] + 0.1, world_to_camera, intrinsic)
    world_yaw_deg = _segment_heading_deg(segment)
    if a is None or b is None:
        return {
            "world_yaw_deg": round(world_yaw_deg, 6),
            "image_angle_deg": None,
            "image_vector": None,
            "image_points": [],
        }
    vx = b[0] - a[0]
    vy = b[1] - a[1]
    norm = math.hypot(vx, vy)
    if norm < 1e-6:
        return {
            "world_yaw_deg": round(world_yaw_deg, 6),
            "image_angle_deg": None,
            "image_vector": None,
            "image_points": [[round(a[0], 3), round(a[1], 3)], [round(b[0], 3), round(b[1], 3)]],
        }
    vector = [round(vx / norm, 6), round(vy / norm, 6)]
    return {
        "world_yaw_deg": round(world_yaw_deg, 6),
        "image_angle_deg": round(math.degrees(math.atan2(vy, vx)), 6),
        "image_vector": vector,
        "image_points": [[round(a[0], 3), round(a[1], 3)], [round(b[0], 3), round(b[1], 3)]],
    }


def _frame_qa(
    segment: dict[str, Any],
    road_mask_meta: dict[str, Any],
    semantic_meta: dict[str, Any],
    lane_instances: list[dict[str, Any]],
    heading: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    status = "pass"
    if road_mask_meta["mask_ratio"] <= 0.002:
        reasons.append("road_mask_full_empty_or_too_small")
    if semantic_meta["mask_ratio"] <= 0.001:
        reasons.append("semantic_road_mask_proxy_empty_or_too_small")
    if not lane_instances:
        reasons.append("lane_polyline_projected_empty")
    if not _lane_marking_coverage_satisfied(segment, lane_instances):
        reasons.append("lane_polyline_projected_missing_expected_physical_markings")
    if road_mask_meta.get("lane_outside_road_mask_fraction", 0.0) > LANE_MASK_MAX_OUTSIDE_FRACTION:
        reasons.append("lane_polyline_projected_outside_road_mask_full")
    if _has_duplicate_lane_geometries(lane_instances):
        reasons.append("lane_polyline_projected_duplicate_physical_geometry")
    if heading["image_vector"] is None:
        reasons.append("road_heading_invalid")
    if semantic_meta.get("extra_fraction_of_semantic", 0.0) > 0.35:
        warnings.append("semantic_road_mask_proxy_excess_extra_area")
    if semantic_meta.get("missing_fraction_of_road_mask_full", 0.0) > 0.35:
        warnings.append("road_mask_full_excess_outside_semantic_proxy")
    if reasons:
        status = "reject"
    elif warnings:
        status = "warn"
    return {"status": status, "rejection_reasons": reasons, "warning_reasons": warnings}


def _has_duplicate_lane_geometries(lane_instances: list[dict[str, Any]]) -> bool:
    keys = [_geometry_key(instance["points_image"]) for instance in lane_instances]
    if len(keys) != len(set(keys)):
        return True
    for idx, instance in enumerate(lane_instances):
        for other in lane_instances[idx + 1 :]:
            if _polylines_near_duplicate(instance["points_image"], other["points_image"]):
                return True
    return False


def _expected_physical_marking_count(segment: dict[str, Any]) -> int:
    lane_ids = list(segment.get("driving_lane_ids", []))
    lane_count = int(segment.get("lane_count") or len(lane_ids))
    if lane_count <= 0:
        return 1
    return lane_count + _lane_group_count(lane_ids)


def _lane_groups(lane_ids: list[int]) -> list[list[int]]:
    ids = sorted(int(lane_id) for lane_id in lane_ids)
    if not ids:
        return []
    groups: list[list[int]] = [[ids[0]]]
    for lane_id in ids[1:]:
        prev = groups[-1][-1]
        # OpenDRIVE has no lane 0. Treat -1 and +1 as adjacent for a single
        # continuous bidirectional carriageway, but split larger gaps such as
        # -1 -> +3 in separated lane groups.
        adjacent = (lane_id - prev == 1) or (prev == -1 and lane_id == 1)
        if adjacent:
            groups[-1].append(lane_id)
        else:
            groups.append([lane_id])
    return groups


def _lane_group_count(lane_ids: list[int]) -> int:
    return max(1, len(_lane_groups(lane_ids)))


def _lane_marking_coverage_satisfied(segment: dict[str, Any], lane_instances: list[dict[str, Any]]) -> bool:
    return len(lane_instances) >= _expected_physical_marking_count(segment)


def _build_frame_label(
    sample_id: str,
    stem: str,
    config: dict[str, Any],
    segment: dict[str, Any],
    spec: dict[str, Any],
    camera_transform: Any,
    rgb_frame: int,
    semantic_frame: int,
    road_mask_meta: dict[str, Any],
    semantic_meta: dict[str, Any],
    lane_instances: list[dict[str, Any]],
    heading: dict[str, Any],
    qa: dict[str, Any],
    semantics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    semantics = semantics or _execution_semantics(config)
    return {
        "schema_version": "benchmark_v1_frame_label_0.1",
        "sample_id": sample_id,
        "image": {"rgb_path": f"rgb/{stem}.jpg", "width": IMAGE_WIDTH, "height": IMAGE_HEIGHT},
        "camera": {
            "mount_mode": config["camera"]["mount_mode"],
            "sampling_mode": config["camera"]["sampling_mode"],
            "intrinsics": _intrinsics_dict(spec["fov_degrees"]),
            "transform_world": _transform_to_dict(camera_transform),
            "height_m": spec["height_m"],
            "pitch_deg": spec["pitch_deg"],
            "yaw_deg": float(camera_transform.rotation.yaw),
            "relative_to_corridor": {
                "lateral_offset_m": spec["lateral_offset_m"],
                "heading_bias_deg": spec["yaw_relative_deg"],
                "along_track_s_m": None,
            },
        },
        "environment": {
            "weather_preset": config["environment"]["weather_preset"],
            "lighting_preset": config["environment"]["lighting_preset"],
            "traffic_profile": config["traffic"]["profile"],
            "traffic_source": config["traffic"]["source"],
        },
        "map": {
            "map_name": config["maps"][0]["map_name"],
            "opendrive_source": Path(config["maps"][0]["opendrive_path"]).name,
            "corridor_id": segment["corridor_id"],
            "road_ids": [segment["road_id"]],
            "section_ids": [segment["section_id"]],
            "lane_ids": segment["driving_lane_ids"],
        },
        "labels": {
            "road_mask_full": _available_path_label(
                "road_mask_full",
                "official_prediction_target",
                "road_mask_full_path",
                f"road_mask_full/{stem}.png",
                "program_derived",
            ),
            "road_boundary_full": {
                "protocol_name": "road_boundary_full",
                "label_role": "official_prediction_target",
                "source_type": "program_derived",
                "availability": "available",
                "storage_data_field": "road_boundary_full",
                "left": spec.get("road_boundary_left", []),
                "right": spec.get("road_boundary_right", []),
                "components": spec.get("road_boundary_components", []),
            },
            "road_heading": {
                "protocol_name": "road_heading",
                "label_role": "official_prediction_target",
                "source_type": "program_derived",
                "availability": "available",
                "storage_data_field": "road_heading",
                "world_yaw_deg": heading["world_yaw_deg"],
                "image_angle_deg": heading["image_angle_deg"],
                "image_vector": heading["image_vector"],
            },
            "lane_polyline_projected": {
                "protocol_name": "lane_polyline_projected",
                "label_role": "official_prediction_target",
                "source_type": "program_derived",
                "availability": "available",
                "storage_data_field": "lane_polylines_projected",
                "instances": lane_instances,
            },
            "semantic_road_mask_proxy": _available_path_label(
                "semantic_road_mask_proxy",
                "proxy_label",
                "semantic_road_mask_proxy_path",
                f"semantic_road_mask_proxy/{stem}.png",
                "proxy",
            ),
        },
        "sample_metadata": {
            "label_sources": {
                "road_mask_full": "program_derived",
                "road_boundary_full": "program_derived",
                "road_heading": "program_derived",
                "lane_polyline_projected": "program_derived",
                "semantic_road_mask_proxy": "proxy",
            },
            "label_field_mapping": {
                "road_mask_full": "labels.road_mask_full.path",
                "road_boundary_full": "labels.road_boundary_full",
                "road_heading": "labels.road_heading",
                "lane_polyline_projected": "labels.lane_polyline_projected.instances",
                "semantic_road_mask_proxy": "labels.semantic_road_mask_proxy.path",
            },
            "source_policy": {
                "phase": semantics["source_policy_phase"],
                "true_visible": "not_available",
                "road_mask_full": "corridor_polygon_projection_no_occlusion_test",
                "lane_polyline_projected": "opendrive_waypoint_lane_marking_projection_no_occlusion_test",
                "lane_polyline_instance_semantics": "unique_physical_marking",
                "road_edge_marking_policy": "included_once_as_unique_physical_marking",
                "expected_physical_marking_count": _expected_physical_marking_count(segment),
                "lane_group_count": _lane_group_count(segment.get("driving_lane_ids", [])),
                "lane_groups": _lane_groups(segment.get("driving_lane_ids", [])),
                "lane_marking_coverage_policy": "reject_segments_below_expected_physical_marking_count",
                "lane_road_mask_containment_policy": (
                    f"reject_if_more_than_{LANE_MASK_MAX_OUTSIDE_FRACTION:.2f}_lane_mask_fraction_outside_"
                    f"road_mask_full_after_{LANE_MASK_CONTAINMENT_TOLERANCE_PX}px_tolerance"
                ),
                "coordinate_domain": "image_projection_with_bounded_margin_for_lane_polylines_unclipped_corridor_boundaries",
            },
            "frame_sync": {"rgb_frame": rgb_frame, "semantic_frame": semantic_frame},
            "road_mask_meta": road_mask_meta,
            "semantic_road_mask_proxy_meta": semantic_meta,
            "lane_polyline_projected_meta": {
                "expected_physical_marking_count": _expected_physical_marking_count(segment),
                "actual_physical_marking_count": len(lane_instances),
                "lane_group_count": _lane_group_count(segment.get("driving_lane_ids", [])),
                "lane_groups": _lane_groups(segment.get("driving_lane_ids", [])),
                "coverage_status": (
                    "pass"
                    if _lane_marking_coverage_satisfied(segment, lane_instances)
                    else "source_incomplete_for_declared_road_edge_marking_policy"
                ),
                "road_mask_containment": {
                    "status": (
                        "pass"
                        if road_mask_meta.get("lane_outside_road_mask_fraction", 1.0) <= LANE_MASK_MAX_OUTSIDE_FRACTION
                        else "fail"
                    ),
                    "max_outside_fraction": LANE_MASK_MAX_OUTSIDE_FRACTION,
                    "tolerance_px": LANE_MASK_CONTAINMENT_TOLERANCE_PX,
                    "lane_mask_pixels": road_mask_meta.get("lane_mask_pixels", 0),
                    "lane_outside_road_mask_pixels": road_mask_meta.get("lane_outside_road_mask_pixels", 0),
                    "lane_outside_road_mask_fraction": road_mask_meta.get("lane_outside_road_mask_fraction", 0.0),
                },
            },
            "road_heading_image_points": heading["image_points"],
            "not_available_fields": [
                "lane_polyline_visible_true",
                "road_mask_visible_true",
                "actor_occlusion_ratio",
                "sumo_lane_occupancy",
                "asset_level_appearance",
                "sim2real_split",
                "formal_train_val_test_split",
            ],
            "future_extension_fields": config.get("future_extension_fields", []),
        },
        "difficulty": {
            "road_area_ratio_full": road_mask_meta["mask_ratio"],
            "lane_instance_count": len(lane_instances),
            "mean_projected_lane_width_px": None,
            "curvature_abs_mean": segment["curvature_summary"].get("mean_abs_heading_delta_deg"),
            "heading_bias_deg": spec["yaw_relative_deg"],
        },
        "qa": qa,
    }


def _available_path_label(protocol_name: str, role: str, storage_path_field: str, path: str, source_type: str) -> dict[str, Any]:
    return {
        "protocol_name": protocol_name,
        "label_role": role,
        "source_type": source_type,
        "availability": "available",
        "storage_path_field": storage_path_field,
        "path": path,
    }


def _intrinsics_dict(fov: float) -> dict[str, float]:
    focal = IMAGE_WIDTH / (2.0 * math.tan(math.radians(fov) / 2.0))
    return {"fx": focal, "fy": focal, "cx": IMAGE_WIDTH / 2.0, "cy": IMAGE_HEIGHT / 2.0, "fov_degrees": fov}


def _transform_to_dict(transform: Any) -> dict[str, dict[str, float]]:
    return {
        "location": {
            "x": float(transform.location.x),
            "y": float(transform.location.y),
            "z": float(transform.location.z),
        },
        "rotation": {
            "pitch": float(transform.rotation.pitch),
            "yaw": float(transform.rotation.yaw),
            "roll": float(transform.rotation.roll),
        },
    }


def _write_overlay(
    rgb_path: Path,
    road_mask_path: Path,
    semantic_path: Path,
    lane_instances: list[dict[str, Any]],
    heading: dict[str, Any],
    output_path: Path,
) -> None:
    import cv2  # type: ignore

    image = cv2.imread(str(rgb_path), cv2.IMREAD_COLOR)
    road = cv2.imread(str(road_mask_path), cv2.IMREAD_GRAYSCALE)
    semantic = cv2.imread(str(semantic_path), cv2.IMREAD_GRAYSCALE)
    if image is None or road is None or semantic is None:
        raise ConfigError(f"cannot build QA overlay for {rgb_path}")
    overlay = image.copy()
    import numpy as np  # type: ignore

    overlay[road > 0] = (0.55 * overlay[road > 0] + 0.45 * np.array([0, 255, 0])).astype("uint8")
    overlay[semantic > 0] = (0.75 * overlay[semantic > 0] + 0.25 * np.array([255, 0, 0])).astype("uint8")
    for instance in lane_instances:
        pts = instance["points_image"]
        for a, b in zip(pts, pts[1:]):
            cv2.line(overlay, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), (0, 255, 255), 2, cv2.LINE_AA)
    if heading.get("image_points"):
        a, b = heading["image_points"]
        cv2.arrowedLine(overlay, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), (0, 0, 255), 3, cv2.LINE_AA)
    cv2.imwrite(str(output_path), overlay, [int(cv2.IMWRITE_JPEG_QUALITY), 92])


def _write_contact_sheet(image_paths: list[Path], output_path: Path) -> None:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    thumbs = []
    for path in image_paths:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            continue
        thumbs.append(cv2.resize(image, (320, 180)))
    if not thumbs:
        raise ConfigError("no QA overlays available for contact sheet")
    cols = 4
    rows = int(math.ceil(len(thumbs) / cols))
    sheet = np.zeros((rows * 180, cols * 320, 3), dtype=np.uint8)
    for idx, thumb in enumerate(thumbs):
        row = idx // cols
        col = idx % cols
        sheet[row * 180 : (row + 1) * 180, col * 320 : (col + 1) * 320] = thumb
    cv2.imwrite(str(output_path), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 92])


def _write_run_status(path: Path, status: str, message: str, semantics: dict[str, Any] | None = None) -> None:
    semantics = semantics or _execution_semantics({})
    payload = {
        "schema_version": semantics["run_status_schema_version"],
        "status": status,
        "message": message,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _build_run_manifest(
    config: dict[str, Any],
    run_id: str,
    argv: list[str],
    run_dir: Path,
    sample_rows: list[dict[str, Any]],
    request: Any,
    excluded_specs: list[dict[str, Any]],
    plan: dict[str, Any] | None = None,
    semantics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    semantics = semantics or _execution_semantics(config)
    merged_excluded_specs = _merge_plan_and_runtime_excluded_specs(plan, excluded_specs)
    manifest = build_preflight_run_manifest(config, run_id, argv)
    manifest["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest["run_stage"] = semantics["manifest_run_stage"]
    manifest["script"]["entrypoint"] = semantics["script_entrypoint"]
    manifest["command_boundary"]["carla_connection"] = semantics["carla_connection_boundary"]
    manifest["command_boundary"]["frame_generation"] = semantics["frame_generation_boundary"]
    manifest["output"]["root"] = str(run_dir)
    manifest["sample_count"] = len(sample_rows)
    manifest["sample_index"] = "samples.json"
    manifest["carla"] = {"host": request.host, "port": request.port, "tm_port": request.tm_port}
    manifest["seed"] = request.seed
    manifest["limits"] = {"max_frames": request.max_frames, "generated_frames": len(sample_rows)}
    manifest["lane_polyline_policy"] = {
        "official_instance_semantics": "unique_physical_marking",
        "road_edge_marking_policy": "included_once_as_unique_physical_marking",
        "duplicate_lane_side_refs": "collapsed_into_source_lane_side_refs_with_distance_aware_near_duplicate_folding",
        "near_duplicate_polyline_distance_px": NEAR_DUPLICATE_POLYLINE_DISTANCE_PX,
        "expected_physical_marking_count": "lane_count_plus_lane_group_count",
        "single_continuous_lane_group_equivalence": "lane_count_plus_one",
        "lane_group_definition": "contiguous_opendrive_lane_ids_with_-1_to_1_treated_as_adjacent_only",
        "coverage_gate": "reject_segments_below_expected_physical_marking_count",
        "containment_gate": (
            f"reject_if_lane_mask_outside_road_mask_full_fraction_gt_{LANE_MASK_MAX_OUTSIDE_FRACTION:.2f}_"
            f"after_{LANE_MASK_CONTAINMENT_TOLERANCE_PX}px_tolerance"
        ),
    }
    manifest["excluded_specs"] = merged_excluded_specs
    manifest["excluded_spec_count"] = len(merged_excluded_specs)
    manifest["excluded_reason_counts"] = _excluded_reason_counts(merged_excluded_specs)
    if semantics["manifest_run_stage"] == "v1rc_smoke" and plan is not None:
        manifest["planned_corridors"] = plan.get("planned_corridors", [])
        manifest["planned_corridor_count"] = len(manifest["planned_corridors"])
    manifest["semantic_road_mask_proxy_decode"] = {
        "mask_source": "carla_semantic_raw_label_ids",
        "accepted_class_ids": [{"name": name, "id": value} for name, value in VERIFIED_ROAD_LABEL_IDS.items()],
        "palette_fallback_allowed": False,
    }
    manifest["coordinate_policy"] = {
        "lane_polyline_projected": f"image_projection_within_{VISIBLE_MARGIN_PX:.0f}px_margin",
        "road_boundary_full": "unclipped_full_projection_may_extend_outside_image",
    }
    manifest["safety"] = {
        "no_training": True,
        "no_formal_split": True,
        "no_baseline": True,
        "no_commit": True,
        "output_under_artifacts": True,
    }
    return manifest


def _build_qa_summary(
    config: dict[str, Any],
    run_id: str,
    run_dir: Path,
    sample_rows: list[dict[str, Any]],
    sample_specs: list[dict[str, Any]],
    excluded_specs: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
    semantics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    semantics = semantics or _execution_semantics(config)
    excluded_specs = excluded_specs or []
    merged_excluded_specs = _merge_plan_and_runtime_excluded_specs(plan, excluded_specs)
    counts = _qa_counts(sample_rows)
    warning_reason_counts = _reason_counts(sample_rows, "warning_reasons")
    rejection_reason_counts = _reason_counts(sample_rows, "rejection_reasons")
    summary = {
        "schema_version": "benchmark_v1_qa_summary_0.1",
        "run_id": run_id,
        "environment": {
            "weather_preset": config["environment"]["weather_preset"],
            "traffic_profile": config["traffic"]["profile"],
        },
        "camera": {
            "mount_mode": config["camera"]["mount_mode"],
            "camera_grid_name": semantics["camera_grid_name"],
            "grid": config["camera"]["grid"],
        },
        "qa_scope": {
            "mode": semantics["qa_mode"],
            "carla_connection": semantics["carla_connection_boundary"],
            "frame_generation": semantics["frame_generation_boundary"],
            "run_dir": str(run_dir),
            "contact_sheet": "qa_contact_sheet.jpg",
        },
        "frame_counts": {
            "total": len(sample_rows),
            "pass": counts["pass"],
            "warn": counts["warn"],
            "reject": counts["reject"],
            "not_run": 0,
        },
        "required_checks": {
            "road_mask_full": "pass",
            "road_boundary_full": "pass",
            "road_heading": "pass",
            "lane_polyline_projected": (
                "pass:non_empty_unique_physical_marking_no_duplicate_or_near_duplicate_geometry_"
                "lane_group_expected_count_and_road_mask_containment"
            ),
            "semantic_road_mask_proxy": "pass_or_warn:verified_road_roadline_ids_and_corridor_extra_area_review",
            "sample_metadata": "pass",
            "qa_summary": "pass",
        },
        "qa_thresholds": {
            "semantic_extra_fraction_of_semantic_warn_gt": 0.35,
            "road_mask_full_outside_semantic_proxy_warn_gt": 0.35,
            "road_mask_full_min_ratio": 0.002,
            "semantic_road_mask_proxy_min_ratio": 0.001,
            "lane_polyline_projected_min_instances": 1,
            "lane_mask_containment_tolerance_px": LANE_MASK_CONTAINMENT_TOLERANCE_PX,
            "lane_mask_outside_road_mask_full_fraction_reject_gt": LANE_MASK_MAX_OUTSIDE_FRACTION,
            "near_duplicate_polyline_distance_px": NEAR_DUPLICATE_POLYLINE_DISTANCE_PX,
        },
        "warning_reasons": sorted(warning_reason_counts),
        "warning_reason_counts": warning_reason_counts,
        "rejection_reason_counts": rejection_reason_counts,
        "excluded_spec_count": len(merged_excluded_specs),
        "excluded_reason_counts": _excluded_reason_counts(merged_excluded_specs),
        "excluded_specs": merged_excluded_specs,
        "contact_sheet_layers": expected_contact_sheet_layers(),
        "sample_specs": [
            {
                "corridor_id": spec["segment"]["corridor_id"],
                "height_m": spec["height_m"],
                "pitch_deg": spec["pitch_deg"],
                "yaw_relative_deg": spec["yaw_relative_deg"],
                "lateral_offset_m": spec["lateral_offset_m"],
            }
            for spec in sample_specs[: len(sample_rows)]
        ],
        "status": "fail" if counts["reject"] else ("warn" if counts["warn"] else "pass"),
        "rejection_reasons": sorted(rejection_reason_counts),
    }
    if semantics["manifest_run_stage"] == "v1rc_smoke" and plan is not None:
        summary["planned_corridors"] = plan.get("planned_corridors", [])
        summary["planned_corridor_count"] = len(summary["planned_corridors"])
    return summary


def _merge_plan_and_runtime_excluded_specs(
    plan: dict[str, Any] | None,
    runtime_excluded_specs: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in list((plan or {}).get("excluded_specs", [])) + list(runtime_excluded_specs or []):
        if not isinstance(spec, dict):
            continue
        key = json.dumps(spec, sort_keys=True, ensure_ascii=True)
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(spec))
    return merged


def _excluded_reason_counts(excluded_specs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for spec in excluded_specs:
        for reason in spec.get("rejection_reasons", []):
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return counts


def _qa_counts(sample_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"pass": 0, "warn": 0, "reject": 0}
    for row in sample_rows:
        status = row.get("qa_status", "reject")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _reason_counts(sample_rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in sample_rows:
        for reason in row.get(key, []):
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return counts
