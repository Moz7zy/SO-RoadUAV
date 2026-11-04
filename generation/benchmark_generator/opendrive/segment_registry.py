"""Offline OpenDRIVE segment registry generation.

This module intentionally uses only the Python standard library and never imports
or connects to CARLA. It is the Phase A static entry point for inspecting cached
OpenDRIVE files and drafting road segment candidates.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable
import xml.etree.ElementTree as ET


SCHEMA_VERSION = "benchmark_v1_segment_registry_0.1"
DEFAULT_OPENDRIVE_ROOT = Path(os.environ.get("CARLA_OPENDRIVE_ROOT", "/opt/Carla/OpenDrive"))
DEFAULT_OUTPUT_ROOT = Path(os.environ.get("BENCHMARK_V3_OUTPUT_ROOT", "."))


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_path_within(path: Path, allowed_roots: Iterable[Path], label: str) -> Path:
    resolved = path.expanduser().resolve()
    allowed = [root.expanduser().resolve() for root in allowed_roots]
    if not any(is_relative_to(resolved, root) for root in allowed):
        roots = ", ".join(str(root) for root in allowed)
        raise ValueError(f"{label} path is outside allowed roots: {resolved} not in [{roots}]")
    return resolved


def _float_attr(element: ET.Element, name: str, default: float = 0.0) -> float:
    value = element.get(name)
    if value is None:
        return default
    return float(value)


def _sample_line(x0: float, y0: float, hdg: float, length: float, spacing: float) -> list[list[float]]:
    count = max(2, int(math.ceil(length / spacing)) + 1)
    points: list[list[float]] = []
    for idx in range(count):
        s_pos = min(length, idx * spacing)
        x = x0 + s_pos * math.cos(hdg)
        y = y0 + s_pos * math.sin(hdg)
        points.append([round(x, 4), round(y, 4), 0.0])
    return points


def _sample_arc(
    x0: float,
    y0: float,
    hdg: float,
    length: float,
    curvature: float,
    spacing: float,
) -> list[list[float]]:
    if abs(curvature) < 1e-9:
        return _sample_line(x0, y0, hdg, length, spacing)
    count = max(2, int(math.ceil(length / spacing)) + 1)
    points: list[list[float]] = []
    radius = 1.0 / curvature
    for idx in range(count):
        s_pos = min(length, idx * spacing)
        theta = hdg + s_pos * curvature
        x = x0 + radius * (math.sin(theta) - math.sin(hdg))
        y = y0 - radius * (math.cos(theta) - math.cos(hdg))
        points.append([round(x, 4), round(y, 4), 0.0])
    return points


def sample_plan_view(road: ET.Element, spacing_m: float) -> tuple[list[list[float]], list[str]]:
    plan_view = road.find("planView")
    if plan_view is None:
        return [], ["missing_plan_view"]

    centerline: list[list[float]] = []
    unsupported: list[str] = []
    for geometry in plan_view.findall("geometry"):
        x0 = _float_attr(geometry, "x")
        y0 = _float_attr(geometry, "y")
        hdg = _float_attr(geometry, "hdg")
        length = _float_attr(geometry, "length")
        if geometry.find("line") is not None:
            points = _sample_line(x0, y0, hdg, length, spacing_m)
        elif geometry.find("arc") is not None:
            arc = geometry.find("arc")
            assert arc is not None
            points = _sample_arc(x0, y0, hdg, length, _float_attr(arc, "curvature"), spacing_m)
        else:
            child_tags = [child.tag for child in list(geometry)]
            unsupported.append("unsupported_geometry:" + ",".join(child_tags or ["unknown"]))
            points = _sample_line(x0, y0, hdg, length, spacing_m)

        if centerline and points and centerline[-1] == points[0]:
            centerline.extend(points[1:])
        else:
            centerline.extend(points)
    return centerline, unsupported


def _driving_lane_ids(lane_section: ET.Element) -> list[int]:
    lane_ids: list[int] = []
    for side_name in ("left", "right"):
        side = lane_section.find(side_name)
        if side is None:
            continue
        for lane in side.findall("lane"):
            if lane.get("type") == "driving":
                lane_id = lane.get("id")
                if lane_id is not None:
                    lane_ids.append(int(lane_id))
    return sorted(lane_ids)


def _section_s_ranges(road: ET.Element, road_length: float) -> list[tuple[int, float, float, ET.Element]]:
    lanes = road.find("lanes")
    if lanes is None:
        return []
    sections = lanes.findall("laneSection")
    section_starts = [_float_attr(section, "s") for section in sections]
    ranges: list[tuple[int, float, float, ET.Element]] = []
    for idx, section in enumerate(sections):
        start = section_starts[idx]
        end = section_starts[idx + 1] if idx + 1 < len(section_starts) else road_length
        ranges.append((idx, start, end, section))
    return ranges


def _curvature_summary(centerline: list[list[float]]) -> dict[str, Any]:
    if len(centerline) < 3:
        return {"availability": "not_available", "reason": "too_few_centerline_points"}

    headings: list[float] = []
    for a, b in zip(centerline, centerline[1:]):
        headings.append(math.atan2(b[1] - a[1], b[0] - a[0]))
    deltas: list[float] = []
    for a, b in zip(headings, headings[1:]):
        delta = (b - a + math.pi) % (2.0 * math.pi) - math.pi
        deltas.append(abs(delta))
    if not deltas:
        return {"availability": "not_available", "reason": "too_few_heading_samples"}
    return {
        "availability": "program_derived_rough",
        "mean_abs_heading_delta_deg": round(math.degrees(sum(deltas) / len(deltas)), 4),
        "max_abs_heading_delta_deg": round(math.degrees(max(deltas)), 4),
    }


def _slice_centerline(
    centerline: list[list[float]],
    s_start: float,
    s_end: float,
    road_length: float,
) -> list[list[float]]:
    if not centerline or road_length <= 0.0:
        return []
    start_ratio = max(0.0, min(1.0, s_start / road_length))
    end_ratio = max(0.0, min(1.0, s_end / road_length))
    start_idx = int(start_ratio * max(0, len(centerline) - 1))
    end_idx = int(math.ceil(end_ratio * max(0, len(centerline) - 1)))
    sliced = centerline[start_idx : end_idx + 1]
    return sliced if len(sliced) >= 2 else centerline[:2]


def _topology_tags(junction: bool, lane_count: int, length_m: float, unsupported_geometry: list[str]) -> list[str]:
    tags: list[str] = []
    tags.append("junction" if junction else "non_junction")
    if lane_count >= 4:
        tags.append("multi_lane_candidate")
    elif lane_count >= 1:
        tags.append("main_or_secondary_candidate")
    if length_m < 30.0:
        tags.append("short_segment")
    if unsupported_geometry:
        tags.append("contains_unsupported_geometry_approximated")
    return tags


def _unsupported_geometry_reasons(unsupported_geometry: list[str]) -> list[str]:
    return [f"unsupported_geometry_rejected:{item}" for item in unsupported_geometry]


def build_segment_registry(
    opendrive_path: Path,
    map_name: str | None = None,
    min_segment_length_m: float = 20.0,
    sample_spacing_m: float = 5.0,
    allowed_opendrive_roots: Iterable[Path] = (DEFAULT_OPENDRIVE_ROOT,),
) -> dict[str, Any]:
    """Build a draft road segment registry from an OpenDRIVE file offline."""

    xodr_path = ensure_path_within(opendrive_path, allowed_opendrive_roots, "OpenDRIVE")
    if not xodr_path.is_file():
        raise FileNotFoundError(f"OpenDRIVE file not found: {xodr_path}")

    inferred_map_name = map_name or xodr_path.stem
    tree = ET.parse(xodr_path)
    root = tree.getroot()

    segments: list[dict[str, Any]] = []
    for road in root.findall("road"):
        road_id_raw = road.get("id", "unknown")
        road_length = float(road.get("length", "0"))
        junction = road.get("junction", "-1") != "-1"
        full_centerline, unsupported_geometry = sample_plan_view(road, sample_spacing_m)
        section_ranges = _section_s_ranges(road, road_length)

        if not section_ranges:
            section_ranges = [(0, 0.0, road_length, ET.Element("laneSection"))]

        for section_idx, s_start, s_end, section in section_ranges:
            driving_lane_ids = _driving_lane_ids(section)
            section_length = max(0.0, s_end - s_start)
            rejection_reasons: list[str] = []
            if not driving_lane_ids:
                rejection_reasons.append("no_driving_lanes")
            if section_length < min_segment_length_m:
                rejection_reasons.append("below_min_segment_length")
            if len(full_centerline) < 2:
                rejection_reasons.append("missing_or_invalid_centerline")
            if unsupported_geometry:
                rejection_reasons.extend(_unsupported_geometry_reasons(unsupported_geometry))

            status = "rejected" if rejection_reasons else "candidate"
            section_centerline = _slice_centerline(full_centerline, s_start, s_end, road_length)
            corridor_id = f"{inferred_map_name}_road{road_id_raw}_sec{section_idx:02d}_all"
            segment = {
                "corridor_id": corridor_id,
                "road_id": int(road_id_raw) if road_id_raw.lstrip("-").isdigit() else road_id_raw,
                "section_id": section_idx,
                "driving_lane_ids": driving_lane_ids,
                "s_range": [round(s_start, 4), round(s_end, 4)],
                "centerline_world": section_centerline,
                "corridor_polygon_world": [],
                "corridor_polygon_world_status": "future_extension",
                "length_m": round(section_length, 4),
                "lane_count": len(driving_lane_ids),
                "junction": junction,
                "curvature_summary": _curvature_summary(section_centerline),
                "topology_tags": _topology_tags(junction, len(driving_lane_ids), section_length, unsupported_geometry),
                "registry_status": status,
                "rejection_reasons": rejection_reasons,
                "promotion_blockers": _unsupported_geometry_reasons(unsupported_geometry),
                "not_available_fields": [],
                "future_extension_fields": [
                    "corridor_polygon_world",
                    "precise_topology_class",
                    "merge_split_nodes"
                ],
            }
            segments.append(segment)

    summary = {
        "total_segments": len(segments),
        "candidate_segments": sum(1 for item in segments if item["registry_status"] == "candidate"),
        "rejected_segments": sum(1 for item in segments if item["registry_status"] == "rejected"),
        "accepted_segments": sum(1 for item in segments if item["registry_status"] == "accepted"),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "map_name": inferred_map_name,
        "opendrive_path": str(xodr_path),
        "protocol_contract": {
            "required_frame_fields": [
                "environment.weather_preset",
                "camera.mount_mode",
                "road_mask_full",
                "road_boundary_full",
                "road_heading",
                "lane_polyline_projected",
                "semantic_road_mask_proxy",
                "sample_metadata",
                "qa_summary"
            ],
            "not_available_fields": [
                "lane_polyline_visible_true",
                "road_mask_visible_true",
                "SUMO",
                "asset_level_appearance",
                "Sim2Real",
                "formal_split",
                "training"
            ],
            "future_extension_fields": [
                "corridor_polygon_world",
                "true_visible_labels",
                "asset_level_marking_wear",
                "complex_interchange_asset_expansion"
            ],
        },
        "generation": {
            "mode": "offline_opendrive_parse",
            "carla_connection": "not_used",
            "min_segment_length_m": min_segment_length_m,
            "sample_spacing_m": sample_spacing_m,
            "unsupported_geometry_policy": "reject_by_default",
        },
        "segments": segments,
        "summary": summary,
    }


def write_registry(registry: dict[str, Any], output_path: Path, allowed_output_roots: Iterable[Path]) -> Path:
    output = ensure_path_within(output_path, allowed_output_roots, "registry output")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a benchmark_v1 OpenDRIVE segment registry offline.")
    parser.add_argument("--opendrive-path", type=Path, required=True)
    parser.add_argument("--map-name", type=str, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--min-segment-length-m", type=float, default=20.0)
    parser.add_argument("--sample-spacing-m", type=float, default=5.0)
    parser.add_argument("--allowed-opendrive-root", type=Path, action="append", default=[DEFAULT_OPENDRIVE_ROOT])
    parser.add_argument("--allowed-output-root", type=Path, action="append", default=[DEFAULT_OUTPUT_ROOT])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print summary only. No registry file is written.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    registry = build_segment_registry(
        opendrive_path=args.opendrive_path,
        map_name=args.map_name,
        min_segment_length_m=args.min_segment_length_m,
        sample_spacing_m=args.sample_spacing_m,
        allowed_opendrive_roots=args.allowed_opendrive_root,
    )
    if args.output is not None and not args.dry_run:
        output = write_registry(registry, args.output, args.allowed_output_root)
        print(f"wrote registry: {output}")
    else:
        print(json.dumps(registry["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
