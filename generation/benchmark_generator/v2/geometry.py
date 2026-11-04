"""V2-native anchor, lane envelope, and clipped projection geometry."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class ResolvedAnchor:
    x: float
    y: float
    z: float
    yaw_deg: float
    along_track_m: float
    centerline_length_m: float


def polyline_length(points: list[Any]) -> float:
    return sum(math.dist(_xyz(first), _xyz(second)) for first, second in zip(points, points[1:]))


def resolve_anchor(points: list[Any], fraction: float) -> ResolvedAnchor:
    if len(points) < 2:
        raise ValueError("anchor centerline requires at least two points")
    clamped = min(1.0, max(0.0, float(fraction)))
    total = polyline_length(points)
    x, y, z, yaw = point_and_heading(points, total * clamped)
    return ResolvedAnchor(x, y, z, yaw, total * clamped, total)


def point_and_heading(points: list[Any], distance: float) -> tuple[float, float, float, float]:
    if len(points) < 2:
        raise ValueError("polyline requires at least two points")
    total = polyline_length(points)
    target = min(max(0.0, float(distance)), max(0.0, total - 1e-6))
    walked = 0.0
    for first_raw, second_raw in zip(points, points[1:]):
        first, second = _xyz(first_raw), _xyz(second_raw)
        length = math.dist(first, second)
        if length <= 1e-9:
            continue
        if walked + length >= target:
            ratio = (target - walked) / length
            x = first[0] + ratio * (second[0] - first[0])
            y = first[1] + ratio * (second[1] - first[1])
            z = first[2] + ratio * (second[2] - first[2])
            yaw = math.degrees(math.atan2(second[1] - first[1], second[0] - first[0]))
            return x, y, z, yaw
        walked += length
    first, second = _xyz(points[-2]), _xyz(points[-1])
    return second[0], second[1], second[2], math.degrees(math.atan2(second[1] - first[1], second[0] - first[0]))


def camera_transform_for_sample(carla: Any, carla_map: Any, segment: dict[str, Any], sample: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    centerline = segment.get("_carla_centerline_world") or segment["centerline_world"]
    anchor = resolve_anchor(centerline, float(sample["anchor_fraction"]))
    waypoint = carla_map.get_waypoint(
        carla.Location(x=anchor.x, y=anchor.y, z=anchor.z + 2.0),
        project_to_road=True,
        lane_type=carla.LaneType.Driving,
    )
    ground_z = float(waypoint.transform.location.z) if waypoint is not None else anchor.z
    camera = sample["camera"]
    transform = carla.Transform(
        carla.Location(x=anchor.x, y=anchor.y, z=ground_z + float(camera["height_m"])),
        carla.Rotation(
            pitch=float(camera["pitch_deg"]),
            yaw=anchor.yaw_deg + float(camera["yaw_relative_deg"]),
            roll=0.0,
        ),
    )
    return transform, {
        "requested_anchor_fraction": float(sample["anchor_fraction"]),
        "resolved_world": {"x": anchor.x, "y": anchor.y, "z": ground_z},
        "resolved_local_yaw_deg": anchor.yaw_deg,
        "resolved_along_track_m": anchor.along_track_m,
        "centerline_length_m": anchor.centerline_length_m,
    }


def project_local_heading(
    camera_transform: Any,
    segment: dict[str, Any],
    anchor_fraction: float,
    fov_degrees: float,
    image_width: int,
    image_height: int,
) -> dict[str, Any]:
    import numpy as np

    centerline = segment.get("_carla_centerline_world") or segment["centerline_world"]
    anchor = resolve_anchor(centerline, anchor_fraction)
    first = point_and_heading(centerline, max(0.0, anchor.along_track_m - 5.0))
    second = point_and_heading(centerline, min(anchor.centerline_length_m, anchor.along_track_m + 5.0))
    matrix = np.asarray(camera_transform.get_inverse_matrix(), dtype=float)
    intrinsic = _camera_intrinsic(image_width, image_height, fov_degrees)
    a = _project_world_point((first[0], first[1], first[2] + 0.1), matrix, intrinsic)
    b = _project_world_point((second[0], second[1], second[2] + 0.1), matrix, intrinsic)
    if a is None or b is None:
        return {
            "world_yaw_deg": anchor.yaw_deg,
            "image_angle_deg": None,
            "image_vector": None,
            "image_points": [],
        }
    dx, dy = b[0] - a[0], b[1] - a[1]
    norm = math.hypot(dx, dy)
    if norm <= 1e-6:
        vector = None
        angle = None
    else:
        vector = [dx / norm, dy / norm]
        angle = math.degrees(math.atan2(dy, dx))
    return {
        "world_yaw_deg": anchor.yaw_deg,
        "image_angle_deg": angle,
        "image_vector": vector,
        "image_points": [[a[0], a[1]], [b[0], b[1]]],
    }


def collect_local_lane_markings(
    carla: Any,
    waypoints: list[Any],
    segment: dict[str, Any],
    anchor_fraction: float,
    half_window_m: float,
) -> list[dict[str, Any]]:
    lane_ids = set(int(value) for value in segment["driving_lane_ids"])
    s_start, s_end = [float(value) for value in segment["s_range"]]
    anchor_s = s_start + (s_end - s_start) * float(anchor_fraction)
    local_start, local_end = max(s_start, anchor_s - half_window_m), min(s_end, anchor_s + half_window_m)
    buckets: dict[tuple[Any, ...], list[tuple[float, Any]]] = {}
    for waypoint in waypoints:
        if int(waypoint.road_id) != int(segment["road_id"]) or int(waypoint.section_id) != int(segment["section_id"]):
            continue
        if int(waypoint.lane_id) not in lane_ids or not local_start <= float(waypoint.s) <= local_end:
            continue
        for side, marking in (("left", waypoint.left_lane_marking), ("right", waypoint.right_lane_marking)):
            if marking is None or getattr(marking, "type", None) in (None, carla.LaneMarkingType.NONE, carla.LaneMarkingType.Other):
                continue
            key = (int(waypoint.road_id), int(waypoint.section_id), int(waypoint.lane_id), side, str(marking.type))
            buckets.setdefault(key, []).append((float(waypoint.s), _lane_edge_location(carla, waypoint, side)))
    boundaries: list[dict[str, Any]] = []
    for key, samples in sorted(buckets.items()):
        current: list[tuple[float, Any]] = []
        for sample in sorted(samples, key=lambda item: item[0]):
            if current and sample[0] - current[-1][0] > 5.0:
                _append_marking_boundary(boundaries, key, current)
                current = []
            current.append(sample)
        _append_marking_boundary(boundaries, key, current)
    return boundaries


def build_target_corridor(
    waypoint_cache: list[Any],
    segment: dict[str, Any],
    anchor_fraction: float,
    camera_transform: Any,
    fov_degrees: float,
    image_width: int,
    image_height: int,
    half_window_m: float = 180.0,
    near_plane_m: float = 1.0,
) -> dict[str, Any]:
    import numpy as np

    lane_groups = _lane_groups(segment.get("driving_lane_ids", []))
    s_start, s_end = [float(value) for value in segment["s_range"]]
    anchor_s = s_start + (s_end - s_start) * float(anchor_fraction)
    local_start, local_end = max(s_start, anchor_s - half_window_m), min(s_end, anchor_s + half_window_m)
    lane_sections = _lane_strip_sections(waypoint_cache, segment, local_start, local_end)
    matrix = np.asarray(camera_transform.get_inverse_matrix(), dtype=float)
    fx = image_width / (2.0 * math.tan(math.radians(fov_degrees) / 2.0))
    fy = fx
    cx, cy = image_width / 2.0, image_height / 2.0
    polygons: list[list[list[float]]] = []
    component_counts: dict[str, int] = {}
    world_strip_count = 0
    invalid_strip_count = 0
    for lane_id, sections in lane_sections.items():
        component_counts[str(lane_id)] = 0
        for first, second in zip(sections, sections[1:]):
            if second["s"] - first["s"] > 5.0:
                continue
            world_strip_count += 1
            world_polygon = [first["left"], first["right"], second["right"], second["left"]]
            camera_polygon = [_world_to_camera_cv(point, matrix) for point in world_polygon]
            clipped = _clip_camera_polygon(camera_polygon, near_plane_m, fx, fy, cx, cy, image_width, image_height)
            if len(clipped) < 3:
                continue
            image_polygon = [
                [min(image_width - 1.0, max(0.0, fx * point[0] / point[2] + cx)),
                 min(image_height - 1.0, max(0.0, fy * point[1] / point[2] + cy))]
                for point in clipped
            ]
            if not _finite_polygon(image_polygon) or _polygon_area(image_polygon) <= 1.0:
                invalid_strip_count += 1
                continue
            polygons.append([[round(point[0], 3), round(point[1], 3)] for point in image_polygon])
            component_counts[str(lane_id)] += 1
    largest = max(polygons, key=_polygon_area) if polygons else []
    max_abs = max((abs(value) for polygon in polygons for point in polygon for value in point), default=0.0)
    return {
        "left": [],
        "right": [],
        "polygon": largest,
        "polygons": polygons,
        "components": [],
        "construction": {
            "mode": "lane_width_topology_local_strip_frustum_clipped",
            "anchor_s": anchor_s,
            "local_s_range": [local_start, local_end],
            "half_window_m": half_window_m,
            "near_plane_m": near_plane_m,
            "lane_groups": lane_groups,
            "world_strip_count": world_strip_count,
            "clipped_polygon_count": len(polygons),
            "invalid_strip_count": invalid_strip_count,
            "component_strip_counts": component_counts,
            "missing_lane_strip_count": sum(1 for count in component_counts.values() if count == 0),
            "projected_max_abs_coord_px": max_abs,
        },
    }


def polygon_quality(corridor: dict[str, Any], image_width: int, image_height: int) -> dict[str, Any]:
    polygons = corridor.get("polygons", [])
    nonfinite = 0
    out_of_bounds = 0
    degenerate = 0
    self_intersections = 0
    max_abs = 0.0
    for polygon in polygons:
        if not _finite_polygon(polygon):
            nonfinite += 1
        if len({(round(float(point[0]), 6), round(float(point[1]), 6)) for point in polygon}) < 3:
            degenerate += 1
        if _polygon_self_intersects(polygon):
            self_intersections += 1
        for x, y in polygon:
            max_abs = max(max_abs, abs(float(x)), abs(float(y)))
            if not (0.0 <= float(x) <= image_width - 1 and 0.0 <= float(y) <= image_height - 1):
                out_of_bounds += 1
    construction = corridor.get("construction", {})
    return {
        "polygon_count": len(polygons),
        "nonfinite_polygon_count": nonfinite,
        "out_of_bounds_vertex_count": out_of_bounds,
        "degenerate_polygon_count": degenerate,
        "self_intersection_count": self_intersections,
        "projected_max_abs_coord_px": round(max_abs, 4),
        "world_strip_count": int(construction.get("world_strip_count", 0)),
        "invalid_strip_count": int(construction.get("invalid_strip_count", 0)),
        "missing_lane_strip_count": int(construction.get("missing_lane_strip_count", 0)),
        "construction_mode": construction.get("mode"),
    }


def mask_overlap_metrics(target_mask: Any, semantic_road_mask: Any, tolerance_px: int = 5) -> dict[str, float]:
    import cv2
    import numpy as np

    target = np.asarray(target_mask) > 0
    semantic = np.asarray(semantic_road_mask) > 0
    kernel = np.ones((2 * tolerance_px + 1, 2 * tolerance_px + 1), dtype=np.uint8)
    semantic_dilated = cv2.dilate(semantic.astype(np.uint8), kernel) > 0
    target_count = int(target.sum())
    semantic_count = int(semantic.sum())
    intersection = int((target & semantic).sum())
    outside = int((target & ~semantic_dilated).sum())
    return {
        "target_outside_semantic_road_fraction": round(outside / target_count, 6) if target_count else 1.0,
        "target_visible_semantic_precision": round(intersection / target_count, 6) if target_count else 0.0,
        "target_visible_semantic_recall": round(intersection / semantic_count, 6) if semantic_count else 0.0,
    }


def _lane_strip_sections(
    waypoints: list[Any],
    segment: dict[str, Any],
    local_start: float,
    local_end: float,
) -> dict[int, list[dict[str, Any]]]:
    lane_ids = {int(value) for value in segment.get("driving_lane_ids", [])}
    result: dict[int, list[dict[str, Any]]] = {lane_id: [] for lane_id in lane_ids}
    for waypoint in waypoints:
        if int(waypoint.road_id) != int(segment["road_id"]) or int(waypoint.section_id) != int(segment["section_id"]):
            continue
        if int(waypoint.lane_id) not in lane_ids or not local_start <= float(waypoint.s) <= local_end:
            continue
        lane_id = int(waypoint.lane_id)
        result[lane_id].append({
            "s": float(waypoint.s),
            "left": _lane_edge_xyz(waypoint, "left"),
            "right": _lane_edge_xyz(waypoint, "right"),
        })
    for lane_id in result:
        by_s = {round(item["s"], 3): item for item in result[lane_id]}
        result[lane_id] = [by_s[key] for key in sorted(by_s)]
    return result


def _lane_edge_xyz(waypoint: Any, side: str) -> tuple[float, float, float]:
    right = waypoint.transform.get_right_vector()
    sign = -1.0 if side == "left" else 1.0
    center = waypoint.transform.location
    half = float(waypoint.lane_width) * 0.5
    return (
        float(center.x) + sign * float(right.x) * half,
        float(center.y) + sign * float(right.y) * half,
        float(center.z) + 0.08,
    )


def _lane_edge_location(carla: Any, waypoint: Any, side: str) -> Any:
    x, y, z = _lane_edge_xyz(waypoint, side)
    return carla.Location(x=x, y=y, z=z)


def _append_marking_boundary(boundaries: list[dict[str, Any]], key: tuple[Any, ...], samples: list[tuple[float, Any]]) -> None:
    if len(samples) >= 2:
        boundaries.append({
            "source_key": key,
            "locations": [location for _, location in samples],
            "source_s_range": [samples[0][0], samples[-1][0]],
        })


def _clip_camera_polygon(
    polygon: list[tuple[float, float, float]],
    near: float,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    width: int,
    height: int,
) -> list[tuple[float, float, float]]:
    planes = [
        lambda p: p[2] - near,
        lambda p: p[0] + (cx / fx) * p[2],
        lambda p: -p[0] + ((width - 1.0 - cx) / fx) * p[2],
        lambda p: p[1] + (cy / fy) * p[2],
        lambda p: -p[1] + ((height - 1.0 - cy) / fy) * p[2],
    ]
    clipped = list(polygon)
    for evaluate in planes:
        if not clipped:
            break
        output: list[tuple[float, float, float]] = []
        previous = clipped[-1]
        previous_value = evaluate(previous)
        for current in clipped:
            current_value = evaluate(current)
            previous_inside = previous_value >= 0.0
            current_inside = current_value >= 0.0
            if current_inside != previous_inside:
                denominator = previous_value - current_value
                ratio = previous_value / denominator if abs(denominator) > 1e-12 else 0.0
                output.append(tuple(previous[index] + ratio * (current[index] - previous[index]) for index in range(3)))
            if current_inside:
                output.append(current)
            previous, previous_value = current, current_value
        clipped = output
    return clipped


def _world_to_camera_cv(point: tuple[float, float, float], world_to_camera: Any) -> tuple[float, float, float]:
    import numpy as np

    world = np.asarray([point[0], point[1], point[2], 1.0], dtype=float)
    camera = world_to_camera @ world
    return float(camera[1]), float(-camera[2]), float(camera[0])


def _camera_intrinsic(width: int, height: int, fov: float) -> Any:
    import numpy as np

    focal = width / (2.0 * math.tan(math.radians(fov) / 2.0))
    return np.asarray([[focal, 0.0, width / 2.0], [0.0, focal, height / 2.0], [0.0, 0.0, 1.0]], dtype=float)


def _project_world_point(point: tuple[float, float, float], matrix: Any, intrinsic: Any) -> tuple[float, float] | None:
    import numpy as np

    cv_point = np.asarray(_world_to_camera_cv(point, matrix), dtype=float)
    if cv_point[2] <= 1.0:
        return None
    projected = intrinsic @ cv_point
    return float(projected[0] / projected[2]), float(projected[1] / projected[2])


def _lane_groups(lane_ids: list[Any]) -> list[list[int]]:
    ids = sorted(int(value) for value in lane_ids)
    if not ids:
        return []
    groups = [[ids[0]]]
    for lane_id in ids[1:]:
        previous = groups[-1][-1]
        if lane_id - previous == 1 or (previous == -1 and lane_id == 1):
            groups[-1].append(lane_id)
        else:
            groups.append([lane_id])
    return groups


def _polygon_area(polygon: list[list[float]]) -> float:
    if len(polygon) < 3:
        return 0.0
    return abs(sum(
        float(first[0]) * float(second[1]) - float(second[0]) * float(first[1])
        for first, second in zip(polygon, polygon[1:] + polygon[:1])
    )) * 0.5


def _finite_polygon(polygon: list[list[float]]) -> bool:
    return all(math.isfinite(float(value)) for point in polygon for value in point)


def _polygon_self_intersects(polygon: list[list[float]]) -> bool:
    if len(polygon) < 4:
        return False
    edges = list(zip(polygon, polygon[1:] + polygon[:1]))
    for first_index, first in enumerate(edges):
        for second_index, second in enumerate(edges[first_index + 1:], start=first_index + 1):
            if second_index in {first_index, first_index + 1} or (first_index == 0 and second_index == len(edges) - 1):
                continue
            if _segments_intersect(first[0], first[1], second[0], second[1]):
                return True
    return False


def _segments_intersect(a: list[float], b: list[float], c: list[float], d: list[float]) -> bool:
    def orientation(p: list[float], q: list[float], r: list[float]) -> float:
        return (float(q[0]) - float(p[0])) * (float(r[1]) - float(p[1])) - (float(q[1]) - float(p[1])) * (float(r[0]) - float(p[0]))

    first = orientation(a, b, c)
    second = orientation(a, b, d)
    third = orientation(c, d, a)
    fourth = orientation(c, d, b)
    return first * second < -1e-9 and third * fourth < -1e-9


def _location_xyz(location: Any) -> tuple[float, float, float]:
    return float(location.x), float(location.y), float(location.z)


def _xyz(point: Any) -> tuple[float, float, float]:
    if isinstance(point, (list, tuple)):
        return float(point[0]), float(point[1]), float(point[2])
    return float(point.x), float(point.y), float(point.z)
