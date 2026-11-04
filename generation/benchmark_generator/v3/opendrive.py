"""Dependency-free OpenDRIVE parsing for the V3 Road Library inventory."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Iterable
import xml.etree.ElementTree as ET

from benchmark_generator.config import ensure_path_within, forbid_legacy_source_path

from .constants import OPENDRIVE_INVENTORY_RULE_VERSION


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_opendrive_map(
    opendrive_path: Path,
    map_id: str,
    allowed_roots: Iterable[Path],
    bbox_spacing_m: float = 10.0,
) -> dict[str, Any]:
    path = ensure_path_within(Path(opendrive_path), [Path(item) for item in allowed_roots], "benchmark_v3 OpenDRIVE")
    forbid_legacy_source_path(path, "benchmark_v3 OpenDRIVE")
    if not path.is_file() or path.name != f"{map_id}.xodr":
        raise FileNotFoundError(f"missing exact benchmark_v3 OpenDRIVE file: {path}")
    root = ET.parse(path).getroot()
    roads = [_parse_road(item, bbox_spacing_m) for item in root.findall("road")]
    junctions = [_parse_junction(item) for item in root.findall("junction")]
    geometry_types: dict[str, int] = {}
    for road in roads:
        for name in road["geometry_types"]:
            geometry_types[name] = geometry_types.get(name, 0) + 1
    header = root.find("header")
    return {
        "map_id": map_id,
        "opendrive_path": str(path),
        "opendrive_sha256": file_sha256(path),
        "parser_rule_version": OPENDRIVE_INVENTORY_RULE_VERSION,
        "header": dict(sorted(header.attrib.items())) if header is not None else {},
        "roads": sorted(roads, key=lambda item: _numeric_key(item["road_id"])),
        "junctions": sorted(junctions, key=lambda item: _numeric_key(item["junction_id"])),
        "summary": {
            "road_count": len(roads),
            "junction_count": len(junctions),
            "junction_internal_road_count": sum(item["is_junction_internal"] for item in roads),
            "lane_section_count": sum(len(item["lane_sections"]) for item in roads),
            "geometry_type_counts": dict(sorted(geometry_types.items())),
            "unsupported_geometry_count": sum(bool(item["unsupported_geometry_types"]) for item in roads),
        },
    }


def pose_at_s(road: dict[str, Any], s_m: float) -> dict[str, float]:
    s_value = max(0.0, min(float(road["length_m"]), float(s_m)))
    geometries = road["plan_geometries"]
    if not geometries:
        return {"x": 0.0, "y": 0.0, "z": _elevation_at(road["elevation_profiles"], s_value), "heading_deg": 0.0}
    geometry = geometries[-1]
    for item in geometries:
        if float(item["s_m"]) <= s_value <= float(item["s_m"]) + float(item["length_m"]) + 1e-6:
            geometry = item
            break
        if float(item["s_m"]) <= s_value:
            geometry = item
    ds = max(0.0, min(float(geometry["length_m"]), s_value - float(geometry["s_m"])))
    heading = float(geometry["heading_rad"])
    if geometry["geometry_type"] == "arc":
        curvature = float(geometry["curvature"])
        if abs(curvature) < 1e-12:
            x = float(geometry["x_m"]) + ds * math.cos(heading)
            y = float(geometry["y_m"]) + ds * math.sin(heading)
        else:
            radius = 1.0 / curvature
            theta = heading + ds * curvature
            x = float(geometry["x_m"]) + radius * (math.sin(theta) - math.sin(heading))
            y = float(geometry["y_m"]) - radius * (math.cos(theta) - math.cos(heading))
            heading = theta
    else:
        x = float(geometry["x_m"]) + ds * math.cos(heading)
        y = float(geometry["y_m"]) + ds * math.sin(heading)
    return {
        "x": round(x, 4),
        "y": round(y, 4),
        "z": round(_elevation_at(road["elevation_profiles"], s_value), 4),
        "heading_deg": round(math.degrees(heading) % 360.0, 4),
    }


def lane_section_at_s(road: dict[str, Any], s_m: float) -> dict[str, Any] | None:
    sections = road["lane_sections"]
    for section in sections:
        if float(section["s_start_m"]) - 1e-6 <= s_m <= float(section["s_end_m"]) + 1e-6:
            return section
    return sections[-1] if sections else None


def road_contact_for_junction(road: dict[str, Any], junction_id: str) -> str | None:
    for relation in ("predecessor", "successor"):
        link = road["links"].get(relation)
        if link and link.get("element_type") == "junction" and str(link.get("element_id")) == str(junction_id):
            return "start" if relation == "predecessor" else "end"
    return None


def section_curvature(road: dict[str, Any], s_start: float, s_end: float) -> dict[str, float]:
    signed = 0.0
    absolute = 0.0
    max_abs_curvature = 0.0
    for geometry in road["plan_geometries"]:
        left = max(float(geometry["s_m"]), s_start)
        right = min(float(geometry["s_m"]) + float(geometry["length_m"]), s_end)
        overlap = max(0.0, right - left)
        if overlap <= 1e-6:
            continue
        curvature = float(geometry.get("curvature", 0.0))
        signed += curvature * overlap
        absolute += abs(curvature) * overlap
        max_abs_curvature = max(max_abs_curvature, abs(curvature))
    return {
        "net_heading_change_deg": round(math.degrees(signed), 4),
        "absolute_heading_change_deg": round(math.degrees(absolute), 4),
        "max_abs_curvature_per_m": round(max_abs_curvature, 8),
    }


def _parse_road(road: ET.Element, bbox_spacing_m: float) -> dict[str, Any]:
    road_id = str(road.get("id", ""))
    length_m = float(road.get("length", "0"))
    geometries = [_parse_geometry(item) for item in road.findall("./planView/geometry")]
    elevation_profiles = [
        {name: float(item.get(name, "0")) for name in ("s", "a", "b", "c", "d")}
        for item in road.findall("./elevationProfile/elevation")
    ]
    lane_sections = _parse_lane_sections(road, length_m)
    links = _parse_road_links(road.find("link"))
    public = {
        "road_id": road_id,
        "name": str(road.get("name", "")),
        "length_m": round(length_m, 4),
        "junction_id": str(road.get("junction", "-1")),
        "is_junction_internal": road.get("junction", "-1") != "-1",
        "links": links,
        "road_types": [dict(sorted(item.attrib.items())) for item in road.findall("type")],
        "plan_geometries": geometries,
        "geometry_types": sorted({item["geometry_type"] for item in geometries}),
        "unsupported_geometry_types": sorted({
            item["geometry_type"] for item in geometries if item["geometry_type"] not in {"line", "arc"}
        }),
        "elevation_profiles": sorted(elevation_profiles, key=lambda item: item["s"]),
        "lane_sections": lane_sections,
    }
    public["start_pose"] = pose_at_s(public, 0.0)
    public["end_pose"] = pose_at_s(public, length_m)
    public["bbox_xy"] = _road_bbox(public, bbox_spacing_m)
    return public


def _parse_geometry(geometry: ET.Element) -> dict[str, Any]:
    children = list(geometry)
    geometry_type = children[0].tag if children else "unknown"
    curvature = float(children[0].get("curvature", "0")) if geometry_type == "arc" else 0.0
    return {
        "s_m": round(float(geometry.get("s", "0")), 6),
        "x_m": round(float(geometry.get("x", "0")), 6),
        "y_m": round(float(geometry.get("y", "0")), 6),
        "heading_rad": round(float(geometry.get("hdg", "0")), 10),
        "length_m": round(float(geometry.get("length", "0")), 6),
        "geometry_type": geometry_type,
        "curvature": round(curvature, 12),
    }


def _parse_lane_sections(road: ET.Element, road_length: float) -> list[dict[str, Any]]:
    sections = road.findall("./lanes/laneSection")
    starts = [float(item.get("s", "0")) for item in sections]
    result = []
    for index, section in enumerate(sections):
        start = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else road_length
        lane_ids: list[int] = []
        markings: list[str] = []
        for side_name in ("left", "right"):
            for lane in section.findall(f"./{side_name}/lane"):
                if lane.get("type") != "driving":
                    continue
                lane_ids.append(int(lane.get("id", "0")))
                for marking in lane.findall("roadMark"):
                    marking_type = str(marking.get("type", "none")).lower()
                    if marking_type not in {"", "none", "other"}:
                        markings.append(marking_type)
        result.append({
            "section_id": index,
            "s_start_m": round(start, 4),
            "s_end_m": round(end, 4),
            "length_m": round(max(0.0, end - start), 4),
            "driving_lane_ids": sorted(lane_ids),
            "total_driving_lanes": len(lane_ids),
            "forward_driving_lanes": sum(item < 0 for item in lane_ids),
            "reverse_driving_lanes": sum(item > 0 for item in lane_ids),
            "road_marking_candidate_count": len(markings),
            "road_marking_types": sorted(set(markings)),
        })
    return result


def _parse_road_links(link: ET.Element | None) -> dict[str, Any]:
    result: dict[str, Any] = {"predecessor": None, "successor": None}
    if link is None:
        return result
    for relation in result:
        item = link.find(relation)
        if item is not None:
            result[relation] = {
                "element_type": str(item.get("elementType", "")),
                "element_id": str(item.get("elementId", "")),
                "contact_point": item.get("contactPoint"),
            }
    return result


def _parse_junction(junction: ET.Element) -> dict[str, Any]:
    connections = []
    for item in junction.findall("connection"):
        connections.append({
            "connection_id": str(item.get("id", "")),
            "incoming_road_id": str(item.get("incomingRoad", "")),
            "connecting_road_id": str(item.get("connectingRoad", "")),
            "contact_point": str(item.get("contactPoint", "")),
            "lane_links": [
                {"from_lane_id": int(link.get("from", "0")), "to_lane_id": int(link.get("to", "0"))}
                for link in item.findall("laneLink")
            ],
        })
    return {
        "junction_id": str(junction.get("id", "")),
        "name": str(junction.get("name", "")),
        "connections": sorted(connections, key=lambda item: _numeric_key(item["connection_id"])),
        "incoming_road_ids": sorted({item["incoming_road_id"] for item in connections}, key=_numeric_key),
        "connecting_road_ids": sorted({item["connecting_road_id"] for item in connections}, key=_numeric_key),
    }


def _elevation_at(profiles: list[dict[str, float]], s_m: float) -> float:
    if not profiles:
        return 0.0
    active = profiles[0]
    for profile in profiles:
        if profile["s"] <= s_m:
            active = profile
        else:
            break
    ds = s_m - active["s"]
    return active["a"] + active["b"] * ds + active["c"] * ds * ds + active["d"] * ds * ds * ds


def _road_bbox(road: dict[str, Any], spacing_m: float) -> dict[str, float]:
    length = float(road["length_m"])
    samples = max(2, int(math.ceil(length / max(1.0, spacing_m))) + 1)
    poses = [pose_at_s(road, length * index / (samples - 1)) for index in range(samples)]
    xs = [item["x"] for item in poses]
    ys = [item["y"] for item in poses]
    return {"min_x": min(xs), "min_y": min(ys), "max_x": max(xs), "max_y": max(ys)}


def _numeric_key(value: Any) -> tuple[int, Any]:
    text = str(value)
    return (0, int(text)) if text.lstrip("-").isdigit() else (1, text)
