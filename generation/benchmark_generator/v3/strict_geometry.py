"""Deterministic strict/development geometry QA and offline ramp preselection.

The module intentionally has no CARLA dependency.  It mirrors the camera
projection used by the CARLA worker for the fixed V3-P0 condition so frozen
OpenDRIVE anchors can be screened before a corrective capture is authorized.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable
import xml.etree.ElementTree as ET

from .opendrive import lane_section_at_s, pose_at_s


DEFAULT_QA_POLICY_VERSION = "benchmark_v3_qa_policy_20260722"
RAMP_QA_POLICY_VERSION = "benchmark_v3_qa_policy_20260727_ramp_area_v2"
IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 720
IMAGE_DIAGONAL = math.hypot(IMAGE_WIDTH, IMAGE_HEIGHT)


def polygon_area(points: Iterable[Iterable[float]]) -> float:
    values = [(float(p[0]), float(p[1])) for p in points]
    if len(values) < 3:
        return 0.0
    return abs(sum(
        values[i][0] * values[(i + 1) % len(values)][1]
        - values[(i + 1) % len(values)][0] * values[i][1]
        for i in range(len(values))
    ) / 2.0)


def _clip_rect(points: list[tuple[float, float]], width: int, height: int) -> list[tuple[float, float]]:
    """Clip a projected polygon to the continuous image rectangle.

    Image coordinates are a continuous rectangle ``[0,width] x [0,height]``.
    The upper edge is deliberately *not* ``width - 1``/``height - 1``: the
    latter is a pixel-raster convention and changes a 10x10 rectangle into an
    11x11 closed-endpoint object.
    """
    result = list(points)
    for axis, value, sign in ((0, 0.0, 1.0), (0, float(width), -1.0),
                              (1, 0.0, 1.0), (1, float(height), -1.0)):
        if not result:
            return []
        output: list[tuple[float, float]] = []
        previous = result[-1]
        previous_value = (previous[axis] - value) * sign
        for current in result:
            current_value = (current[axis] - value) * sign
            previous_inside = previous_value >= 0.0
            current_inside = current_value >= 0.0
            if current_inside != previous_inside:
                denominator = previous_value - current_value
                fraction = previous_value / denominator if denominator else 0.0
                output.append((
                    previous[0] + fraction * (current[0] - previous[0]),
                    previous[1] + fraction * (current[1] - previous[1]),
                ))
            if current_inside:
                output.append(current)
            previous = current
            previous_value = current_value
        result = output
    return [(float(point[0]), float(point[1])) for point in result]


def _polygon_edges(polygon: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return list(zip(polygon, polygon[1:] + polygon[:1])) if len(polygon) >= 3 else []


def _edge_x_intersection(first: tuple[tuple[float, float], tuple[float, float]], second: tuple[tuple[float, float], tuple[float, float]]) -> float | None:
    """Return x at which two non-parallel edge segments cross, if any."""
    (x1, y1), (x2, y2) = first
    (x3, y3), (x4, y4) = second
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denominator) < 1e-12:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denominator
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denominator
    if -1e-10 <= t <= 1.0 + 1e-10 and -1e-10 <= u <= 1.0 + 1e-10:
        return x1 + t * (x2 - x1)
    return None


def _edge_y_at_x(edge: tuple[tuple[float, float], tuple[float, float]], x: float) -> float | None:
    (x1, y1), (x2, y2) = edge
    if abs(x2 - x1) < 1e-12:
        return None
    lo, hi = sorted((x1, x2))
    if x <= lo + 1e-10 or x >= hi - 1e-10:
        # Slab midpoints never lie on a vertex.  Keeping the endpoint guard
        # makes this helper safe for direct tests and avoids double counting.
        if x < lo - 1e-10 or x > hi + 1e-10:
            return None
    return y1 + (x - x1) * (y2 - y1) / (x2 - x1)


def continuous_union_area(
    polygons: Iterable[Iterable[Iterable[float]]],
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
) -> float:
    """Return the continuous union area of clipped polygons.

    This dependency-free vertical sweep is exact for piecewise-linear simple
    polygons (up to floating point round-off).  Slabs are split at every
    vertex and every segment intersection, so the ordering of edge crossings
    is constant within a slab.  The union length at the slab midpoint is then
    integrated with the trapezoid rule, which is exact for the resulting
    piecewise-linear boundary.  Polygon boundaries have zero measure.
    """
    width_f, height_f = float(width), float(height)
    if width_f <= 0.0 or height_f <= 0.0:
        raise ValueError("image dimensions must be positive")
    clipped_polygons: list[list[tuple[float, float]]] = []
    for polygon in polygons:
        values = [(float(p[0]), float(p[1])) for p in polygon]
        clipped = _clip_rect(values, width_f, height_f)
        if len(clipped) >= 3 and polygon_area(clipped) > 1e-12:
            clipped_polygons.append(clipped)
    if not clipped_polygons:
        return 0.0
    edges = [edge for polygon in clipped_polygons for edge in _polygon_edges(polygon)]
    xs = {0.0, width_f}
    for edge in edges:
        xs.add(max(0.0, min(width_f, edge[0][0])))
        xs.add(max(0.0, min(width_f, edge[1][0])))
    for index, first in enumerate(edges):
        for second in edges[index + 1:]:
            crossing = _edge_x_intersection(first, second)
            if crossing is not None and -1e-9 <= crossing <= width_f + 1e-9:
                xs.add(max(0.0, min(width_f, crossing)))
    ordered_x = sorted(xs)

    def union_length_at(x: float) -> float:
        intervals: list[tuple[float, float]] = []
        for polygon in clipped_polygons:
            crossings = sorted(
                value for edge in _polygon_edges(polygon)
                for value in [_edge_y_at_x(edge, x)]
                if value is not None and -1e-8 <= value <= height_f + 1e-8
            )
            # Even-odd pairing is valid for each simple polygon.  Duplicate
            # vertex hits at a non-vertex slab midpoint are not expected, but
            # deduplication keeps numerical noise from producing zero slivers.
            dedup: list[float] = []
            for value in crossings:
                value = max(0.0, min(height_f, value))
                if not dedup or abs(value - dedup[-1]) > 1e-9:
                    dedup.append(value)
            for first, second in zip(dedup[::2], dedup[1::2]):
                if second > first:
                    intervals.append((first, second))
        intervals.sort()
        merged = 0.0
        start = end = None
        for first, second in intervals:
            if start is None:
                start, end = first, second
            elif first <= end + 1e-10:
                end = max(end, second)
            else:
                merged += end - start
                start, end = first, second
        if start is not None:
            merged += end - start
        return merged

    area = 0.0
    for left, right in zip(ordered_x, ordered_x[1:]):
        if right - left <= 1e-10:
            continue
        mid = (left + right) / 2.0
        # Union cross-section length is piecewise linear on a slab.  Evaluate
        # both ends slightly inside and integrate their mean to avoid endpoint
        # vertex ambiguity while retaining exactness to floating precision.
        eps = min((right - left) * 1e-7, 1e-7)
        left_len = union_length_at(left + eps)
        right_len = union_length_at(right - eps)
        if abs(left_len - right_len) < 1e-8:
            slab_length = union_length_at(mid)
        else:
            slab_length = (left_len + right_len) / 2.0
        area += slab_length * (right - left)
    return max(0.0, min(width_f * height_f, area))


def continuous_union_area_ratio(
    polygons: Iterable[Iterable[Iterable[float]]],
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
) -> float:
    return continuous_union_area(polygons, width, height) / (float(width) * float(height))


def raster_union_area_ratio(
    polygons: Iterable[Iterable[Iterable[float]]],
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
) -> float:
    """Compatibility name; V3 now uses continuous geometry, not raster fill."""
    return continuous_union_area_ratio(polygons, width, height)


def polyline_length(points: Iterable[Iterable[float]]) -> float:
    values = [(float(p[0]), float(p[1])) for p in points]
    return sum(math.dist(first, second) for first, second in zip(values, values[1:]))


def geometry_metrics_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    camera = metadata.get("camera", {})
    width = int(camera.get("image_width", IMAGE_WIDTH))
    height = int(camera.get("image_height", IMAGE_HEIGHT))
    corridor = metadata.get("labels", {}).get("lane_polyline_projected", {}).get("target_corridor_polygon", [])
    ratio = continuous_union_area_ratio(corridor, width, height)
    instances = metadata.get("labels", {}).get("lane_polyline_projected", {}).get("instances", [])
    lengths = sorted((polyline_length(item.get("points_image", [])) / math.hypot(width, height) for item in instances), reverse=True)
    major = (lengths + [0.0, 0.0])[:2]
    context = metadata.get("qa_strict_geometry", {}).get("context", {})
    return {
        "target_corridor_area_ratio": round(ratio, 6),
        "projected_boundary_normalized_lengths": [round(value, 6) for value in major],
        "main_boundary_length_diagonal_ratio": round(min(major), 6),
        "junction_centered": bool(context.get("junction_centered", False)),
        "merge_conflict_centered": bool(context.get("merge_conflict_centered", False)),
        "corridor_touches_conflict_zone": bool(context.get("corridor_touches_conflict_zone", False)),
        "target_lane_count": context.get("target_lane_count"),
        "image_width": width,
        "image_height": height,
        "source": "metadata_projected_geometry_continuous_polygon_union_v2",
    }


def evaluate_geometry(
    metadata: dict[str, Any],
    *,
    tier: str,
    policy_version: str = DEFAULT_QA_POLICY_VERSION,
    strict_area_min: float = 0.05,
    development_area_min: float = 0.03,
    boundary_min: float = 0.15,
    policy: dict[str, Any] | None = None,
    recipe_expected_projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = geometry_metrics_from_metadata(metadata)
    structural_cell = metadata.get("structural_cell")
    policy_geometry = (policy or {}).get("strict_geometry", {})
    if tier == "strict":
        area_floor = float(policy_geometry.get("default_area_threshold", strict_area_min))
        overrides = policy_geometry.get("structural_cell_area_thresholds", {})
        area_floor = float(overrides.get(structural_cell, area_floor))
    else:
        area_floor = float(policy_geometry.get("development_area_threshold", development_area_min))
    reasons: list[str] = []
    if metrics["target_corridor_area_ratio"] < area_floor:
        reasons.append("target_corridor_area_ratio_below_threshold")
    if metrics["main_boundary_length_diagonal_ratio"] < boundary_min:
        reasons.append("projected_boundary_length_below_threshold")
    if metrics["junction_centered"]:
        reasons.append("junction_centered")
    if metrics["merge_conflict_centered"]:
        reasons.append("merge_conflict_centered")
    if tier == "strict" and structural_cell == "Ramp-1-to-Mainline-8" and metrics.get("target_lane_count") not in (None, 1):
        reasons.append("ramp_target_lane_count_not_one")
    if recipe_expected_projection is not None:
        actual = metadata.get("qa_strict_geometry", {}).get("recipe_actual_projection")
        if actual is not None and actual != recipe_expected_projection:
            reasons.append("recipe_projection_mismatch")
        if metadata.get("qa_strict_geometry", {}).get("target_fully_out_of_frame") is True:
            reasons.append("target_fully_out_of_frame")
    metrics.update({
        "tier": tier,
        "qa_policy_version": policy_version,
        "area_threshold": area_floor,
        "structural_cell": structural_cell,
        "boundary_threshold": boundary_min,
        "recipe_expected_projection": recipe_expected_projection,
        "status": "pass" if not reasons else "fail",
        "rejection_reasons": reasons,
    })
    return metrics


_XML_LANE_CACHE: dict[tuple[str, str, int], list[tuple[str, list[dict[str, float]], str]]] = {}


def _xml_lane_layout(path: Path, road_id: str, section_id: int, s_m: float) -> dict[int, tuple[str, float]]:
    """Return lane type/width at *s_m* for one OpenDRIVE lane section."""
    key = (str(path), str(road_id), int(section_id))
    cached = _XML_LANE_CACHE.get(key)
    if cached is None:
        root = ET.parse(path).getroot()
        road = next((item for item in root.findall("road") if str(item.get("id")) == str(road_id)), None)
        if road is None:
            return {}
        sections = road.findall("./lanes/laneSection")
        if not sections:
            return {}
        section = sections[min(max(int(section_id), 0), len(sections) - 1)]
        cached = []
        for side in ("left", "right"):
            for lane in section.findall(f"./{side}/lane"):
                widths = []
                for width in lane.findall("width"):
                    widths.append({
                        "sOffset": float(width.get("sOffset", "0")),
                        "a": float(width.get("a", "0")),
                        "b": float(width.get("b", "0")),
                        "c": float(width.get("c", "0")),
                        "d": float(width.get("d", "0")),
                    })
                cached.append((str(lane.get("id", "0")), widths, str(lane.get("type", "none"))))
        _XML_LANE_CACHE[key] = cached
    # Section start is not needed for the width polynomial in the usual V3
    # maps because sOffset is relative to the section.  The parser stores the
    # active lane section boundary in ``s_m``; use zero when unavailable.
    result: dict[int, tuple[str, float]] = {}
    for lane_id_text, widths, lane_type in cached:
        lane_id = int(lane_id_text)
        active = widths[0] if widths else None
        relative_s = max(0.0, float(s_m))
        for width in widths:
            if width["sOffset"] <= relative_s:
                active = width
        if active is None:
            value = 3.5
        else:
            ds = max(0.0, relative_s - active["sOffset"])
            value = sum(active[name] * ds ** power for power, name in enumerate(("a", "b", "c", "d")))
        result[lane_id] = (lane_type, max(0.1, value))
    return result


def _lane_offsets(layout: dict[int, tuple[str, float]], lane_id: int) -> tuple[float, float]:
    """Return inner/outer lateral offsets in OpenDRIVE standard coordinates."""
    if lane_id > 0:
        previous = sorted(value for value in layout if value > 0 and value < lane_id)
        inner = sum(layout[item][1] for item in previous)
        return inner, inner + layout[lane_id][1]
    previous = sorted((value for value in layout if value < 0 and value > lane_id), key=abs)
    inner = sum(layout[item][1] for item in previous)
    return -inner - layout[lane_id][1], -inner


def _camera_basis(yaw_deg: float, pitch_deg: float) -> tuple[tuple[float, float, float], ...]:
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
    forward = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
    right = (math.sin(yaw), -math.cos(yaw), 0.0)
    up = (right[1] * forward[2], -right[0] * forward[2], right[0] * forward[1] - right[1] * forward[0])
    return forward, right, up


def _project(point: tuple[float, float, float], camera: tuple[float, float, float], basis: tuple[Any, ...], focal: float, cx: float, cy: float) -> tuple[float, float] | None:
    vector = tuple(point[index] - camera[index] for index in range(3))
    forward, right, up = basis
    depth = sum(vector[index] * forward[index] for index in range(3))
    if depth <= 1.0:
        return None
    return (
        focal * sum(vector[index] * right[index] for index in range(3)) / depth + cx,
        focal * (-sum(vector[index] * up[index] for index in range(3))) / depth + cy,
    )


def offline_ramp_anchor_metrics(
    *,
    map_path: Path,
    map_document: dict[str, Any],
    site: dict[str, Any],
    anchor: dict[str, Any],
    height_m: float = 45.0,
    pitch_deg: float = -80.0,
    yaw_relative_deg: float = 0.0,
    fov_degrees: float = 90.0,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    spacing_m: float = 2.0,
    junction_margin_m: float = 20.0,
) -> dict[str, Any]:
    road = next((item for item in map_document["roads"] if str(item["road_id"]) == str(anchor["road_id"])), None)
    if road is None:
        raise ValueError(f"OpenDRIVE road {anchor['road_id']} is missing")
    anchor_s = float(anchor["locator"]["s_m"])
    anchor_pose = pose_at_s(road, anchor_s)
    span = next((item for item in site.get("source_spans", []) if item.get("source_ref") == anchor["locator"].get("source_ref")), None)
    orientation = int(span.get("orientation", 1)) if span else 1
    yaw = -float(anchor_pose["heading_deg"]) + (180.0 if orientation < 0 else 0.0)
    if str(anchor["locator"].get("target_direction")) != "along_corridor_reference":
        yaw += 180.0
    yaw += float(yaw_relative_deg)
    camera = (float(anchor_pose["x"]), -float(anchor_pose["y"]), float(anchor_pose["z"]) + float(height_m))
    basis = _camera_basis(yaw, pitch_deg)
    focal = float(width) / (2.0 * math.tan(math.radians(fov_degrees) / 2.0))
    local_start = max(0.0, anchor_s - 120.0)
    local_end = min(float(road["length_m"]), anchor_s + 120.0)
    samples = [local_start + spacing_m * index for index in range(int((local_end - local_start) / spacing_m) + 1)]
    samples.append(local_end)
    samples = sorted(set(round(value, 6) for value in samples))
    section = lane_section_at_s(road, anchor_s) or {"driving_lane_ids": []}
    driving = [int(item) for item in section.get("driving_lane_ids", [])]
    polygons: list[list[list[float]]] = []
    boundary_lines: dict[int, list[list[tuple[float, float] | None]]] = {lane_id: [[], []] for lane_id in driving}
    for lane_id in driving:
        for sample in samples:
            pose = pose_at_s(road, sample)
            layout = _xml_lane_layout(map_path, str(anchor["road_id"]), int(anchor["section_id"]), sample)
            inner, outer = _lane_offsets(layout, lane_id)
            heading = math.radians(float(pose["heading_deg"]))
            normal = (-math.sin(heading), math.cos(heading))
            for boundary_index, offset in enumerate((inner, outer)):
                point = (float(pose["x"]) + normal[0] * offset, -(float(pose["y"]) + normal[1] * offset), float(pose["z"]) + 0.08)
                boundary_lines[lane_id][boundary_index].append(_project(point, camera, basis, focal, width / 2.0, height / 2.0))
    # Build strips between the inner and outer edge of every driving lane.
    for first_s, second_s in zip(samples, samples[1:]):
        for lane_id in driving:
            points: list[tuple[float, float]] = []
            layout_first = _xml_lane_layout(map_path, str(anchor["road_id"]), int(anchor["section_id"]), first_s)
            layout_second = _xml_lane_layout(map_path, str(anchor["road_id"]), int(anchor["section_id"]), second_s)
            for sample, layout, side in ((first_s, layout_first, "inner"), (first_s, layout_first, "outer"), (second_s, layout_second, "outer"), (second_s, layout_second, "inner")):
                pose = pose_at_s(road, sample)
                inner, outer = _lane_offsets(layout, lane_id)
                offset = inner if side == "inner" else outer
                heading = math.radians(float(pose["heading_deg"]))
                normal = (-math.sin(heading), math.cos(heading))
                world = (float(pose["x"]) + normal[0] * offset, -(float(pose["y"]) + normal[1] * offset), float(pose["z"]) + 0.08)
                projected = _project(world, camera, basis, focal, width / 2.0, height / 2.0)
                if projected is None:
                    points = []
                    break
                points.append(projected)
            clipped = _clip_rect(points, width, height)
            if len(clipped) >= 3 and polygon_area(clipped) > 1.0:
                polygons.append([[round(x, 3), round(y, 3)] for x, y in clipped])
    lengths: list[float] = []
    for lane_boundaries in boundary_lines.values():
        for values in lane_boundaries:
            length = 0.0
            previous: tuple[float, float] | None = None
            for value in values:
                # Match the CARLA worker's audited projected-lane visibility
                # margin (64 px); boundary length is measured on the emitted
                # projected polyline, not silently truncated at the frame edge.
                if value is None or not (-64.0 <= value[0] < width + 64.0 and -64.0 <= value[1] < height + 64.0):
                    previous = None
                    continue
                if previous is not None:
                    length += math.dist(previous, value)
                previous = value
            lengths.append(length / math.hypot(width, height))
    section_start = float(section.get("s_start_m", 0.0))
    section_end = float(section.get("s_end_m", road["length_m"]))
    distance_to_endpoint = min(anchor_s - section_start, section_end - anchor_s)
    predecessor = road.get("links", {}).get("predecessor") or {}
    successor = road.get("links", {}).get("successor") or {}
    junction_linked = predecessor.get("element_type") == "junction" or successor.get("element_type") == "junction"
    centered = bool(junction_linked and distance_to_endpoint <= junction_margin_m)
    corridor_touches = bool(junction_linked and (local_start <= section_start + junction_margin_m or local_end >= section_end - junction_margin_m))
    return {
        "anchor_id": anchor["anchor_id"],
        "site_id": site["site_id"],
        "map_id": site["map_id"],
        "structural_cell": site["structural_cell"],
        "road_id": str(anchor["road_id"]),
        "section_id": int(anchor["section_id"]),
        "anchor_s_m": anchor_s,
        "target_lane_count": len(driving),
        "target_corridor_area_ratio": round(raster_union_area_ratio(polygons, width, height), 6),
        "projected_boundary_normalized_lengths": [round(value, 6) for value in sorted(lengths, reverse=True)[:2]],
        "main_boundary_length_diagonal_ratio": round(min((lengths + [0.0, 0.0])[:2]), 6),
        "junction_centered": centered,
        "merge_conflict_centered": centered,
        "corridor_touches_conflict_zone": corridor_touches,
        "junction_linked": junction_linked,
        "local_s_range_m": [round(local_start, 3), round(local_end, 3)],
        "orientation": orientation,
        "projection_condition": {"height_m": height_m, "pitch_deg": pitch_deg, "yaw_relative_deg": yaw_relative_deg, "fov_degrees": fov_degrees, "image_width": width, "image_height": height},
        "source_opendrive_sha256": map_document["opendrive_sha256"],
        "preselection_rule_version": "benchmark_v3_offline_ramp_projection_0.2_continuous_union",
        "qa_policy_version": RAMP_QA_POLICY_VERSION,
        "area_threshold": 0.04,
        "area_semantics": "continuous_polygon_union_clipped_to_[0,width]x[0,height]",
    }


__all__ = [
    "DEFAULT_QA_POLICY_VERSION", "RAMP_QA_POLICY_VERSION", "geometry_metrics_from_metadata", "evaluate_geometry",
    "offline_ramp_anchor_metrics", "polyline_length", "polygon_area", "raster_union_area_ratio",
    "continuous_union_area", "continuous_union_area_ratio",
]
