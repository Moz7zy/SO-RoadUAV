"""Read-only Full.2 audit for over-split projected lane-marking instances.

This module deliberately does not change the historical V2/R4 geometry or
projection helpers.  CARLA is loaded only through the named ``carla_client``
adapter used by :func:`build_world_evidence`, where a frozen OpenDRIVE document
constructs an offline ``carla.Map``.  No client or server API is used.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import csv
import hashlib
import importlib
import json
import math
from pathlib import Path
import shutil
import sys
from typing import Any, Iterable

from benchmark_generator.config import ConfigError, ensure_path_within


AUDIT_SCHEMA_VERSION = "benchmark_v3_full_2_lane_instance_audit_0.1"
AUDIT_RULE_VERSION = "benchmark_v3_full_2_lane_instance_audit_rule_0.1"
CARLA_PYTHON = Path(sys.executable)
IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 720
WAYPOINT_SPACING_M = 2.0
ENDPOINT_DISTANCE_M = max(3.0, 1.5 * WAYPOINT_SPACING_M)
HEADING_DIFFERENCE_DEG = 15.0
HIGH_PRIORITY_RATIO = 1.8

SITE_FIELDS = [
    "site_id", "physical_subset", "source_full_1_role", "view_memberships",
    "map_id", "scene", "structural_cell", "leakage_group_id",
    "source_span_count", "road_ids", "section_ids", "stitch_edge_count",
    "lane_count", "lane_group_count", "expected_physical_marking_count",
    "observed_instance_count_min", "observed_instance_count_max",
    "observed_instance_count_mode", "excess_ratio", "affected_anchor_count",
    "affected_sample_count", "candidate_reason", "auto_merge_eligibility",
    "needs_human_confirmation",
]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ConfigError(f"JSONL row must be an object: {path}:{line_number}")
            rows.append(value)
    return rows


def stable_mode(values: Iterable[int]) -> int:
    counts = Counter(int(value) for value in values)
    if not counts:
        raise ConfigError("cannot compute a mode for an empty sequence")
    return min(counts, key=lambda value: (-counts[value], value))


def lane_groups(lane_ids: Iterable[int]) -> list[list[int]]:
    ids = sorted(set(int(value) for value in lane_ids))
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


def expected_marking_count(lane_count: int, lane_group_count: int) -> int:
    if lane_count <= 0 or lane_group_count <= 0:
        raise ConfigError(
            f"expected physical marking count needs positive lanes/groups: "
            f"lanes={lane_count}, groups={lane_group_count}"
        )
    return int(lane_count) + int(lane_group_count)


def _xyz(value: Any) -> tuple[float, float, float]:
    if isinstance(value, dict):
        return float(value["x"]), float(value["y"]), float(value.get("z", 0.0))
    return float(value.x), float(value.y), float(value.z)


def _distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return math.dist(left, right)


def _undirected_heading_difference(first: tuple[float, float], second: tuple[float, float]) -> float:
    first_angle = math.degrees(math.atan2(first[1], first[0]))
    second_angle = math.degrees(math.atan2(second[1], second[0]))
    difference = abs((first_angle - second_angle + 180.0) % 360.0 - 180.0)
    return min(difference, 180.0 - difference)


def _endpoint(fragment: dict[str, Any], endpoint: str) -> tuple[float, float, float]:
    return tuple(fragment[f"{endpoint}_xyz"])  # type: ignore[return-value]


def _tangent(fragment: dict[str, Any], endpoint: str) -> tuple[float, float]:
    return tuple(fragment[f"{endpoint}_tangent_xy"])  # type: ignore[return-value]


def _markings_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["marking_type"] == right["marking_type"]
        and left["marking_color"] == right["marking_color"]
        and abs(float(left["marking_width_m"]) - float(right["marking_width_m"])) <= 0.01
    )


def match_boundary_endpoints(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    *,
    left_endpoint: str,
    right_endpoint: str,
    distance_threshold_m: float = ENDPOINT_DISTANCE_M,
    heading_threshold_deg: float = HEADING_DIFFERENCE_DEG,
) -> dict[str, Any]:
    """Match two endpoint cross-sections one-to-one while failing on ambiguity."""

    if left_endpoint not in {"start", "end"} or right_endpoint not in {"start", "end"}:
        raise ConfigError("boundary endpoint must be 'start' or 'end'")
    feasible: list[dict[str, Any]] = []
    for left_index, left_item in enumerate(left):
        for right_index, right_item in enumerate(right):
            distance = _distance(_endpoint(left_item, left_endpoint), _endpoint(right_item, right_endpoint))
            heading = _undirected_heading_difference(
                _tangent(left_item, left_endpoint), _tangent(right_item, right_endpoint)
            )
            if distance <= distance_threshold_m and heading <= heading_threshold_deg:
                feasible.append({
                    "left_index": left_index,
                    "right_index": right_index,
                    "distance_m": round(distance, 6),
                    "heading_difference_deg": round(heading, 6),
                    "marking_compatible": _markings_compatible(left_item, right_item),
                })

    ambiguous = []
    for side, key, other in (
        ("left", "left_index", "right_index"),
        ("right", "right_index", "left_index"),
    ):
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in feasible:
            grouped[int(item[key])].append(item)
        for index, options in grouped.items():
            ordered = sorted(options, key=lambda item: (item["distance_m"], item[other]))
            if len(ordered) > 1 and ordered[1]["distance_m"] - ordered[0]["distance_m"] <= 0.25:
                ambiguous.append({"side": side, "index": index, "options": ordered})

    selected = []
    used_left: set[int] = set()
    used_right: set[int] = set()
    if not ambiguous:
        for item in sorted(feasible, key=lambda value: (value["distance_m"], value["left_index"], value["right_index"])):
            if item["left_index"] in used_left or item["right_index"] in used_right:
                continue
            selected.append(item)
            used_left.add(item["left_index"])
            used_right.add(item["right_index"])

    reference_tangent = _tangent(left[0], left_endpoint) if left else (1.0, 0.0)
    tangent_norm = math.hypot(*reference_tangent) or 1.0
    common_normal = (-reference_tangent[1] / tangent_norm, reference_tangent[0] / tangent_norm)

    def ranks(items: list[dict[str, Any]], endpoint: str) -> dict[int, int]:
        ordered = sorted(
            range(len(items)),
            key=lambda index: (
                _endpoint(items[index], endpoint)[0] * common_normal[0]
                + _endpoint(items[index], endpoint)[1] * common_normal[1],
                index,
            ),
        )
        return {item_index: rank for rank, item_index in enumerate(ordered)}

    left_ranks = ranks(left, left_endpoint)
    right_ranks = ranks(right, right_endpoint)
    order_preserved = all(
        left_ranks[item["left_index"]] == right_ranks[item["right_index"]]
        for item in selected
    )
    complete = len(selected) == len(left) == len(right)
    compatible = bool(selected) and all(item["marking_compatible"] for item in selected)
    return {
        "status": "pass" if complete and compatible and order_preserved and not ambiguous else "fail_closed",
        "left_boundary_count": len(left),
        "right_boundary_count": len(right),
        "feasible_pair_count": len(feasible),
        "selected_pairs": selected,
        "ambiguous_matches": ambiguous,
        "complete_one_to_one": complete,
        "lateral_order_preserved": order_preserved,
        "all_markings_compatible": compatible,
        "thresholds": {
            "endpoint_distance_m": distance_threshold_m,
            "heading_difference_deg": heading_threshold_deg,
            "ambiguity_distance_delta_m": 0.25,
        },
    }


def _resample_xyz(points: list[tuple[float, float, float]], count: int = 16) -> list[tuple[float, float, float]]:
    if len(points) < 2:
        return points
    cumulative = [0.0]
    for first, second in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + _distance(first, second))
    total = cumulative[-1]
    if total <= 1e-9:
        return [points[0]] * count
    result = []
    for index in range(count):
        target = total * index / (count - 1)
        segment = 0
        while segment < len(cumulative) - 2 and cumulative[segment + 1] < target:
            segment += 1
        length = max(1e-9, cumulative[segment + 1] - cumulative[segment])
        alpha = (target - cumulative[segment]) / length
        first, second = points[segment], points[segment + 1]
        result.append(tuple(first[axis] + alpha * (second[axis] - first[axis]) for axis in range(3)))
    return result


def _mean_polyline_distance(left: list[tuple[float, float, float]], right: list[tuple[float, float, float]]) -> float:
    first = _resample_xyz(left)
    second = _resample_xyz(right)
    forward = sum(_distance(a, b) for a, b in zip(first, second)) / len(first)
    reverse = sum(_distance(a, b) for a, b in zip(first, reversed(second))) / len(first)
    return min(forward, reverse)


def _lane_edge_xyz(waypoint: Any, side: str) -> tuple[float, float, float]:
    right = waypoint.transform.get_right_vector()
    sign = -1.0 if side == "left" else 1.0
    center = waypoint.transform.location
    half_width = float(waypoint.lane_width) * 0.5
    return (
        float(center.x) + sign * float(right.x) * half_width,
        float(center.y) + sign * float(right.y) * half_width,
        float(center.z) + 0.08,
    )


def _fragment_from_samples(
    key: tuple[Any, ...],
    samples: list[tuple[float, tuple[float, float, float]]],
    span: dict[str, Any],
    span_index: int,
) -> dict[str, Any] | None:
    if len(samples) < 2:
        return None
    road_id, section_id, lane_id, side, marking_type, marking_color, marking_width = key
    points = [point for _, point in samples]
    start_vector = (points[1][0] - points[0][0], points[1][1] - points[0][1])
    end_vector = (points[-1][0] - points[-2][0], points[-1][1] - points[-2][1])
    return {
        "fragment_id": f"span{span_index:02d}_{road_id}_{section_id}_{lane_id}_{side}_{len(samples)}_{samples[0][0]:.3f}",
        "span_index": span_index,
        "source_ref": span["source_ref"],
        "road_id": int(road_id),
        "section_id": int(section_id),
        "lane_id": int(lane_id),
        "side": side,
        "marking_type": marking_type,
        "marking_color": marking_color,
        "marking_width_m": float(marking_width),
        "source_s_range": [round(samples[0][0], 6), round(samples[-1][0], 6)],
        "points_s": [round(value, 6) for value, _ in samples],
        "points_xyz": points,
        "start_xyz": points[0],
        "end_xyz": points[-1],
        "start_tangent_xy": start_vector,
        "end_tangent_xy": end_vector,
        "source_lane_side_refs": [{
            "road_id": int(road_id), "section_id": int(section_id), "lane_id": int(lane_id),
            "side": side, "marking_type": marking_type,
        }],
    }


def _deduplicate_span_fragments(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for fragment in sorted(fragments, key=lambda item: item["fragment_id"]):
        match = None
        for existing in result:
            if (
                existing["marking_type"] == fragment["marking_type"]
                and existing["marking_color"] == fragment["marking_color"]
                and abs(existing["marking_width_m"] - fragment["marking_width_m"]) <= 0.01
                and _mean_polyline_distance(existing["points_xyz"], fragment["points_xyz"]) <= 0.25
            ):
                match = existing
                break
        if match is None:
            result.append(fragment)
        else:
            match["source_lane_side_refs"].extend(fragment["source_lane_side_refs"])
            match["source_lane_side_refs"] = sorted(
                match["source_lane_side_refs"],
                key=lambda item: (item["road_id"], item["section_id"], item["lane_id"], item["side"], item["marking_type"]),
            )
    return result


def _collect_span_fragments(carla: Any, waypoints: list[Any], span: dict[str, Any], span_index: int) -> dict[str, Any]:
    low = min(float(span["physical_start_s_m"]), float(span["physical_end_s_m"]))
    high = max(float(span["physical_start_s_m"]), float(span["physical_end_s_m"]))
    matching = [
        waypoint for waypoint in waypoints
        if int(waypoint.road_id) == int(span["road_id"])
        and int(waypoint.section_id) == int(span["section_id"])
        and low - 0.1 <= float(waypoint.s) <= high + 0.1
        and waypoint.lane_type == carla.LaneType.Driving
    ]
    lane_ids = sorted({int(waypoint.lane_id) for waypoint in matching})
    buckets: dict[tuple[Any, ...], list[tuple[float, tuple[float, float, float]]]] = defaultdict(list)
    for waypoint in matching:
        for side, marking in (("left", waypoint.left_lane_marking), ("right", waypoint.right_lane_marking)):
            if marking is None or getattr(marking, "type", None) in (
                None, carla.LaneMarkingType.NONE, carla.LaneMarkingType.Other,
            ):
                continue
            key = (
                int(waypoint.road_id), int(waypoint.section_id), int(waypoint.lane_id), side,
                str(marking.type), str(getattr(marking, "color", "unknown")),
                round(float(getattr(marking, "width", 0.0)), 3),
            )
            buckets[key].append((float(waypoint.s), _lane_edge_xyz(waypoint, side)))
    raw = []
    for key, samples in sorted(buckets.items()):
        current = []
        for sample in sorted(samples, key=lambda item: item[0]):
            if current and sample[0] - current[-1][0] > 5.0:
                fragment = _fragment_from_samples(key, current, span, span_index)
                if fragment is not None:
                    raw.append(fragment)
                current = []
            current.append(sample)
        fragment = _fragment_from_samples(key, current, span, span_index)
        if fragment is not None:
            raw.append(fragment)
    return {
        "span_index": span_index,
        "source_ref": span["source_ref"],
        "road_id": int(span["road_id"]),
        "section_id": int(span["section_id"]),
        "lane_ids": lane_ids,
        "lane_groups": lane_groups(lane_ids),
        "fragments": _deduplicate_span_fragments(raw),
    }


def _endpoint_fragments(span: dict[str, Any], endpoint: str) -> list[dict[str, Any]]:
    target_s = min(float(span["physical_start_s_m"]), float(span["physical_end_s_m"])) if endpoint == "start" else max(float(span["physical_start_s_m"]), float(span["physical_end_s_m"]))
    key = "start" if endpoint == "start" else "end"
    values = [
        fragment for fragment in span["fragments"]
        if abs(float(fragment["source_s_range"][0 if key == "start" else 1]) - target_s) <= ENDPOINT_DISTANCE_M
    ]
    if not values:
        return []
    tangent = _tangent(values[0], key)
    norm = math.hypot(*tangent) or 1.0
    normal = (-tangent[1] / norm, tangent[0] / norm)
    center = tuple(sum(_endpoint(item, key)[axis] for item in values) / len(values) for axis in range(3))
    return sorted(
        values,
        key=lambda item: (
            (_endpoint(item, key)[0] - center[0]) * normal[0]
            + (_endpoint(item, key)[1] - center[1]) * normal[1],
            item["fragment_id"],
        ),
    )


def _unit_road_map(road_library: dict[str, Any]) -> dict[str, int]:
    return {
        str(item["unit_id"]): int(item["road_id"])
        for item in road_library.get("local_homogeneous_units", [])
    }


def _connection_specs(site: dict[str, Any], unit_roads: dict[str, int]) -> list[dict[str, Any]]:
    specs = []
    site_roads = {int(span["road_id"]) for span in site["source_spans"]}
    for index, record in enumerate(site.get("merge_evidence", {}).get("records", [])):
        if "left_road_id" in record:
            left_road = int(record["left_road_id"])
            right_road = int(record["right_road_id"])
            left_endpoint = str(record["left_endpoint"])
            right_endpoint = str(record["right_endpoint"])
        else:
            left = record.get("left", {})
            right = record.get("right", {})
            left_road = unit_roads.get(str(left.get("unit_id")), -1)
            right_road = unit_roads.get(str(right.get("unit_id")), -1)
            left_endpoint = str(left.get("endpoint"))
            right_endpoint = str(right.get("endpoint"))
        if left_road not in site_roads or right_road not in site_roads:
            continue
        specs.append({
            "record_index": index,
            "left_road_id": left_road,
            "right_road_id": right_road,
            "left_endpoint": left_endpoint,
            "right_endpoint": right_endpoint,
            "frozen_record": deepcopy(record),
        })
    return specs


def _ref_key(value: dict[str, Any], include_type: bool = True) -> tuple[Any, ...]:
    result: tuple[Any, ...] = (
        int(value["road_id"]), int(value["section_id"]), int(value["lane_id"]), str(value["side"]),
    )
    return result + (str(value.get("marking_type")),) if include_type else result


def _pair_ref_payload(left: dict[str, Any], right: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "left_refs": [list(_ref_key(item)) for item in left["source_lane_side_refs"]],
        "right_refs": [list(_ref_key(item)) for item in right["source_lane_side_refs"]],
        "left_fragment_id": left["fragment_id"],
        "right_fragment_id": right["fragment_id"],
    }


def build_world_evidence(
    sites: dict[str, dict[str, Any]],
    road_library: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Build offline OpenDRIVE endpoint evidence.  Never constructs a Client."""

    if Path(sys.executable).resolve() != CARLA_PYTHON.resolve():
        raise ConfigError(f"offline CARLA geometry must run with {CARLA_PYTHON}, got {sys.executable}")
    carla_adapter = importlib.import_module(
        "benchmark_generator.carla_client.lane_instance_audit_geometry"
    )
    carla = carla_adapter.load_offline_carla_module()

    map_records = {item["map_id"]: item for item in road_library["maps"]}
    unit_roads = _unit_road_map(road_library)
    by_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for site in sites.values():
        by_map[site["map_id"]].append(site)
    output: dict[str, dict[str, Any]] = {}
    map_provenance = {}
    for map_id, map_sites in sorted(by_map.items()):
        record = map_records[map_id]
        xodr_path = Path(record["opendrive_path"])
        actual_sha = file_sha256(xodr_path)
        if actual_sha != record["opendrive_sha256"]:
            raise ConfigError(f"frozen OpenDRIVE checksum drifted: {map_id}")
        carla_map = carla.Map(map_id, xodr_path.read_text(encoding="utf-8"))
        waypoints = carla_map.generate_waypoints(WAYPOINT_SPACING_M)
        map_provenance[map_id] = {
            "opendrive_path": str(xodr_path), "opendrive_sha256": actual_sha,
            "offline_carla_map": True, "carla_client": False,
            "waypoint_sampling_spacing_m": WAYPOINT_SPACING_M,
            "waypoint_count": len(waypoints),
        }
        for site in sorted(map_sites, key=lambda item: item["site_id"]):
            span_records = [
                _collect_span_fragments(carla, waypoints, span, index)
                for index, span in enumerate(site["source_spans"])
            ]
            spans_by_road: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
            for frozen, reconstructed in zip(site["source_spans"], span_records):
                spans_by_road[int(frozen["road_id"])].append((frozen, reconstructed))
            connections = []
            merge_pairs = []
            for spec in _connection_specs(site, unit_roads):
                left_options = spans_by_road[spec["left_road_id"]]
                right_options = spans_by_road[spec["right_road_id"]]
                if len(left_options) != 1 or len(right_options) != 1:
                    connections.append({**spec, "status": "fail_closed", "reason": "road_to_span_not_unique"})
                    continue
                left_frozen, left_span = left_options[0]
                right_frozen, right_span = right_options[0]
                left_fragments = _endpoint_fragments({**left_frozen, **left_span}, spec["left_endpoint"])
                right_fragments = _endpoint_fragments({**right_frozen, **right_span}, spec["right_endpoint"])
                match = match_boundary_endpoints(
                    left_fragments, right_fragments,
                    left_endpoint=spec["left_endpoint"], right_endpoint=spec["right_endpoint"],
                )
                for pair in match["selected_pairs"]:
                    if pair["marking_compatible"]:
                        merge_pairs.append(_pair_ref_payload(
                            left_fragments[pair["left_index"]], right_fragments[pair["right_index"]], "cross_span",
                        ))
                connections.append({**spec, "matching": match, "status": match["status"]})

            same_span_pairs = []
            same_span_ambiguous = []
            for span in span_records:
                grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
                for fragment in span["fragments"]:
                    for ref in fragment["source_lane_side_refs"]:
                        grouped[_ref_key(ref, include_type=False)].append(fragment)
                seen = set()
                for base_ref, fragments in sorted(grouped.items()):
                    ordered = sorted(fragments, key=lambda item: item["source_s_range"])
                    for left, right in zip(ordered, ordered[1:]):
                        key = (left["fragment_id"], right["fragment_id"])
                        if key in seen:
                            continue
                        seen.add(key)
                        distance = _distance(tuple(left["end_xyz"]), tuple(right["start_xyz"]))
                        heading = _undirected_heading_difference(tuple(left["end_tangent_xy"]), tuple(right["start_tangent_xy"]))
                        compatible = _markings_compatible(left, right)
                        payload = {
                            **_pair_ref_payload(left, right, "same_span_gap_or_type_split"),
                            "base_ref": list(base_ref), "endpoint_distance_m": round(distance, 6),
                            "heading_difference_deg": round(heading, 6), "marking_compatible": compatible,
                        }
                        if distance <= ENDPOINT_DISTANCE_M and heading <= HEADING_DIFFERENCE_DEG and compatible:
                            same_span_pairs.append(payload)
                        elif distance <= ENDPOINT_DISTANCE_M and heading <= HEADING_DIFFERENCE_DEG:
                            same_span_ambiguous.append({**payload, "reason": "marking_transition_not_explicitly_compatible"})
            merge_pairs.extend(same_span_pairs)
            groups = [len(item["lane_groups"]) for item in span_records if item["lane_groups"]]
            lane_group_count = stable_mode(groups) if groups else 1
            connection_pass = bool(connections) and all(item["status"] == "pass" for item in connections)
            span_summaries = []
            for span in span_records:
                summary = {key: value for key, value in span.items() if key != "fragments"}
                summary["fragment_count"] = len(span["fragments"])
                span_summaries.append(summary)
            output[site["site_id"]] = {
                "lane_group_count": lane_group_count,
                "span_lane_group_counts": groups,
                "spans": span_summaries,
                "connections": connections,
                "mergeable_ref_pairs": merge_pairs,
                "same_span_ambiguous_pairs": same_span_ambiguous,
                "connection_evidence_complete": connection_pass,
                "ambiguous": bool(same_span_ambiguous) or any(
                    item["status"] != "pass" for item in connections
                ),
            }
    return output, map_provenance


def _instance_refs(instance: dict[str, Any]) -> set[tuple[Any, ...]]:
    return {_ref_key(item) for item in instance.get("source_lane_side_refs", [])}


def sample_split_evidence(metadata: dict[str, Any], merge_pairs: list[dict[str, Any]]) -> dict[str, Any]:
    instances = metadata["labels"]["lane_polyline_projected"]["instances"]
    refs_by_instance = [_instance_refs(instance) for instance in instances]
    same_source_splits = []
    by_base: dict[tuple[Any, ...], set[int]] = defaultdict(set)
    by_exact: dict[tuple[Any, ...], set[int]] = defaultdict(set)
    for index, refs in enumerate(refs_by_instance):
        for ref in refs:
            by_exact[ref].add(index)
            by_base[ref[:-1]].add(index)
    for ref, indexes in sorted(by_exact.items()):
        if len(indexes) > 1:
            same_source_splits.append({"kind": "same_source_gap_split", "source_ref": list(ref), "instance_indexes": sorted(indexes)})
    for ref, indexes in sorted(by_base.items()):
        types = {exact[-1] for exact in by_exact if exact[:-1] == ref and by_exact[exact] & indexes}
        if len(indexes) > 1 and len(types) > 1:
            same_source_splits.append({
                "kind": "same_source_marking_type_split", "source_ref_without_type": list(ref),
                "marking_types": sorted(types), "instance_indexes": sorted(indexes),
            })
    pair_hits = []
    for pair in merge_pairs:
        left_refs = {tuple(value) for value in pair["left_refs"]}
        right_refs = {tuple(value) for value in pair["right_refs"]}
        left_indexes = [index for index, refs in enumerate(refs_by_instance) if refs & left_refs]
        right_indexes = [index for index, refs in enumerate(refs_by_instance) if refs & right_refs]
        distinct = sorted({(left, right) for left in left_indexes for right in right_indexes if left != right})
        if distinct:
            pair_hits.append({
                "kind": pair["kind"], "left_refs": pair["left_refs"], "right_refs": pair["right_refs"],
                "distinct_instance_pairs": [list(value) for value in distinct],
            })
    return {"same_source_splits": same_source_splits, "physical_pair_hits": pair_hits}


def _stable_color(key: str) -> tuple[int, int, int]:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return tuple(64 + int(value) % 192 for value in digest[:3])


def draw_overlay(rgb_path: Path, metadata: dict[str, Any], output_path: Path, expected: int) -> None:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    image = cv2.imread(str(rgb_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ConfigError(f"cannot read RGB for audit overlay: {rgb_path}")
    instances = metadata["labels"]["lane_polyline_projected"]["instances"]
    panel_height = min(24 + 17 * (len(instances) + 1), image.shape[0] - 1)
    panel = image[:panel_height].copy()
    image[:panel_height] = (0.25 * panel).astype(np.uint8)
    cv2.putText(
        image, f"expected={expected} observed={len(instances)} sample={metadata['sample_id']}",
        (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1, cv2.LINE_AA,
    )
    for index, instance in enumerate(instances):
        key = str(instance.get("physical_marking_key", instance.get("instance_id", index)))
        color = _stable_color(key)
        points = np.asarray([
            [int(round(float(point[0]))), int(round(float(point[1])))]
            for point in instance.get("points_image", [])
        ], dtype=np.int32)
        if len(points) >= 2:
            cv2.polylines(image, [points], False, color, 3, cv2.LINE_AA)
        label = (
            f"{instance.get('instance_id', index)} {color} "
            f"r{instance.get('road_id')}/s{instance.get('section_id')}/"
            f"l{instance.get('lane_id')}/{instance.get('side')}/{instance.get('marking_type')}"
        )
        cv2.putText(image, label, (8, 38 + index * 17), cv2.FONT_HERSHEY_SIMPLEX, 0.36, color, 1, cv2.LINE_AA)
        if len(points):
            x = max(0, min(image.shape[1] - 100, int(points[len(points) // 2][0])))
            y = max(panel_height + 12, min(image.shape[0] - 4, int(points[len(points) // 2][1])))
            cv2.putText(image, str(instance.get("instance_id", index)), (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.36, color, 1, cv2.LINE_AA)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), image, [int(cv2.IMWRITE_PNG_COMPRESSION), 9]):
        raise ConfigError(f"cannot write audit overlay: {output_path}")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_sites_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extras = sorted(set().union(*(row.keys() for row in rows)) - set(SITE_FIELDS)) if rows else []
    fields = SITE_FIELDS + extras
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            rendered = {
                key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            }
            writer.writerow(rendered)


def _input_file_checksums(run_dir: Path, finalization_dir: Path, scene_dir: Path, static_dirs: list[Path]) -> dict[str, str]:
    paths = [
        run_dir / "run_manifest.json", run_dir / "run_status.json", run_dir / "indexes/sample_index.csv",
        run_dir / "manifests/subsets/train.jsonl", run_dir / "manifests/subsets/val.jsonl",
        run_dir / "manifests/subsets/test_shared.jsonl",
        run_dir / "manifests/test_core/core_scene.jsonl",
        run_dir / "manifests/test_core/core_observation.jsonl",
        run_dir / "manifests/test_core/core_axis.jsonl",
        run_dir / "manifests/test_challenge/challenge_l18.jsonl",
        finalization_dir / "site_registry.json", finalization_dir / "anchor_registry.json",
        finalization_dir / "road_library.json", finalization_dir / "run_manifest.json",
        scene_dir / "decision.json",
    ]
    paths.extend(path / "static_manifest.json" for path in static_dirs)
    return {str(path): file_sha256(path) for path in paths}


def _inventory_digest(rows: list[dict[str, Any]], keys: list[str]) -> str:
    selected = [{key: row[key] for key in keys} for row in sorted(rows, key=lambda item: item["sample_id"])]
    return canonical_json_sha256(selected)


def run_audit(
    *,
    run_dir: Path,
    declared_static_dir: Path,
    finalization_dir: Path,
    scene_dir: Path,
    output_root: Path,
    audit_id: str,
    created_at: str,
) -> Path:
    run_dir = Path(run_dir).resolve()
    declared_static_dir = Path(declared_static_dir).resolve()
    finalization_dir = Path(finalization_dir).resolve()
    scene_dir = Path(scene_dir).resolve()
    output_root = Path(output_root).resolve()
    output_dir = ensure_path_within(output_root / audit_id, [output_root], "Full.2 lane-instance audit output")
    if output_dir.exists():
        raise ConfigError(f"append-only audit output already exists: {output_dir}")

    run_manifest = load_json(run_dir / "run_manifest.json")
    bound_static_dir = Path(run_manifest["static_run"]).resolve()
    if not bound_static_dir.is_dir() or not declared_static_dir.is_dir():
        raise ConfigError("declared or canonical-bound Full.2 static artifact is missing")
    static_manifest = load_json(bound_static_dir / "static_manifest.json")
    if run_manifest["static_manifest_sha256"] != file_sha256(bound_static_dir / "static_manifest.json"):
        raise ConfigError("canonical-bound Full.2 static manifest checksum drifted")
    if run_manifest["expected_ledger_sha256"] != file_sha256(bound_static_dir / "expected_sample_ledger.jsonl"):
        raise ConfigError("canonical-bound Full.2 expected ledger checksum drifted")

    with (run_dir / "indexes/sample_index.csv").open("r", encoding="utf-8", newline="") as handle:
        index_rows = list(csv.DictReader(handle))
    if len(index_rows) != 18624 or len({row["sample_id"] for row in index_rows}) != 18624:
        raise ConfigError("Full.2 sample index is not the frozen 18,624 unique sample inventory")
    subset_rows = {}
    manifest_by_sample = {}
    for subset in ("train", "val", "test_shared"):
        values = load_jsonl(run_dir / "manifests/subsets" / f"{subset}.jsonl")
        subset_rows[subset] = values
        for row in values:
            if row["sample_id"] in manifest_by_sample:
                raise ConfigError(f"sample occurs in more than one physical subset: {row['sample_id']}")
            manifest_by_sample[row["sample_id"]] = row
    expected_subset_counts = {"train": 12000, "val": 1584, "test_shared": 5040}
    if {key: len(value) for key, value in subset_rows.items()} != expected_subset_counts:
        raise ConfigError("Full.2 physical subset counts drifted")
    if set(manifest_by_sample) != {row["sample_id"] for row in index_rows}:
        raise ConfigError("sample index and physical subset manifests disagree")

    view_paths = {
        "core_scene": run_dir / "manifests/test_core/core_scene.jsonl",
        "core_observation": run_dir / "manifests/test_core/core_observation.jsonl",
        "core_axis": run_dir / "manifests/test_core/core_axis.jsonl",
        "challenge_l18": run_dir / "manifests/test_challenge/challenge_l18.jsonl",
    }
    view_memberships: dict[str, set[str]] = defaultdict(set)
    view_counts = {}
    for view, path in view_paths.items():
        values = load_jsonl(path)
        view_counts[view] = len(values)
        for row in values:
            view_memberships[row["sample_id"]].add(view)
    if view_counts != {"core_scene": 5040, "core_observation": 1890, "core_axis": 882, "challenge_l18": 2268}:
        raise ConfigError("Full.2 logical test view counts drifted")

    finalization_manifest = load_json(finalization_dir / "run_manifest.json")
    finalization_declared_checksums = finalization_manifest.get("source_checksums", {})
    finalization_actual_registry_checksums = {
        name: file_sha256(finalization_dir / name)
        for name in ("site_registry.json", "anchor_registry.json", "road_library.json")
    }
    finalization_registry_checksum_matches = {
        name: finalization_declared_checksums.get(name) == checksum
        for name, checksum in finalization_actual_registry_checksums.items()
    }
    final_sites = {item["site_id"]: item for item in load_json(finalization_dir / "site_registry.json")["sites"]}
    selected_site_ids = {row["site_id"] for row in index_rows}
    if not selected_site_ids <= set(final_sites):
        raise ConfigError("Full.2 references sites missing from finalization-v2 registry")
    sites = {site_id: final_sites[site_id] for site_id in selected_site_ids}
    scene_decision = load_json(scene_dir / "decision.json")
    scene_mapping = {item["site_id"]: item for item in scene_decision["per_site_mapping"]}
    for row in manifest_by_sample.values():
        mapping = scene_mapping.get(row["site_id"])
        if mapping is not None and row["scene"] != mapping["new_scene"]:
            raise ConfigError(f"Full.2 manifest scene disagrees with scene reclassification: {row['site_id']}")

    road_library = load_json(finalization_dir / "road_library.json")
    world_evidence, map_provenance = build_world_evidence(sites, road_library)

    input_checksums_before = _input_file_checksums(
        run_dir, finalization_dir, scene_dir, list(dict.fromkeys([declared_static_dir, bound_static_dir])),
    )
    site_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    asset_inventory = []
    for index, index_row in enumerate(sorted(index_rows, key=lambda item: item["sample_id"]), start=1):
        sample_id = index_row["sample_id"]
        manifest = manifest_by_sample[sample_id]
        sample_dir = run_dir / "canonical" / sample_id
        metadata_path = sample_dir / "metadata.json"
        rgb_path = sample_dir / "rgb.jpg"
        mask_path = sample_dir / "road_mask_visible.png"
        metadata = load_json(metadata_path)
        if metadata["sample_id"] != sample_id or metadata["physical_subset"] != manifest["physical_subset"]:
            raise ConfigError(f"Full.2 sample identity/subset drifted: {sample_id}")
        if metadata["site_id"] != manifest["site_id"] or metadata["anchor_id"] != manifest["anchor_id"]:
            raise ConfigError(f"Full.2 site/anchor identity drifted: {sample_id}")
        actual_rgb_sha = file_sha256(rgb_path)
        actual_mask_sha = file_sha256(mask_path)
        if actual_rgb_sha != metadata["assets"]["rgb.jpg"]["sha256"]:
            raise ConfigError(f"RGB checksum disagrees with metadata: {sample_id}")
        if actual_mask_sha != metadata["assets"]["road_mask_visible.png"]["sha256"]:
            raise ConfigError(f"road mask checksum disagrees with metadata: {sample_id}")
        instances = metadata["labels"]["lane_polyline_projected"]["instances"]
        evidence = sample_split_evidence(metadata, world_evidence[metadata["site_id"]]["mergeable_ref_pairs"])
        frozen_site = sites[metadata["site_id"]]
        site_samples[metadata["site_id"]].append({
            "sample_id": sample_id,
            "site_id": metadata["site_id"],
            "anchor_id": metadata["anchor_id"],
            "physical_subset": manifest["physical_subset"],
            "observation_recipe_id": metadata["observation_recipe_id"],
            "recipe_family": metadata.get("recipe_family", "unknown"),
            "source_full_1_role": metadata.get("source_full_1_role"),
            "scene": manifest["scene"],
            "structural_cell": frozen_site["structural_cell"],
            "current_manifest_structural_cell": manifest["structural_cell"],
            "view_memberships": sorted(view_memberships.get(sample_id, set())),
            "is_reference": bool(metadata.get("is_reference")),
            "observed_instance_count": len(instances),
            "metadata_sha256": file_sha256(metadata_path),
            "rgb_sha256": actual_rgb_sha,
            "road_mask_visible_sha256": actual_mask_sha,
            "split_evidence": evidence,
            "metadata_path": str(metadata_path),
            "rgb_path": str(rgb_path),
        })
        asset_inventory.append({
            "sample_id": sample_id, "metadata_sha256": site_samples[metadata["site_id"]][-1]["metadata_sha256"],
            "rgb_sha256": actual_rgb_sha, "road_mask_visible_sha256": actual_mask_sha,
        })

    affected_sites = []
    affected_samples = []
    test_shared_details = []
    for site_id in sorted(site_samples):
        values = site_samples[site_id]
        site = sites[site_id]
        world = world_evidence[site_id]
        lane_count = int(site["total_driving_lanes"])
        group_count = int(world["lane_group_count"])
        expected = expected_marking_count(lane_count, group_count)
        by_context: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for value in values:
            by_context[(value["site_id"], value["anchor_id"], value["recipe_family"])].append(value["observed_instance_count"])
        candidate_values = []
        site_reasons = set()
        for value in values:
            context_counts = by_context[(value["site_id"], value["anchor_id"], value["recipe_family"])]
            count_excess = value["observed_instance_count"] > expected
            high_priority = value["observed_instance_count"] >= HIGH_PRIORITY_RATIO * expected
            physical_hits = bool(value["split_evidence"]["physical_pair_hits"])
            same_source = bool(value["split_evidence"]["same_source_splits"])
            if not (count_excess or physical_hits or same_source):
                continue
            reasons = []
            if count_excess:
                reasons.append("observed_count_exceeds_expected")
                site_reasons.add("observed>expected")
            if high_priority:
                reasons.append("high_priority_ge_1.8x_expected")
                site_reasons.add("observed>=1.8x_expected")
            if physical_hits:
                reasons.append("world_opendrive_physical_pair_split_across_instances")
                site_reasons.add("world/OpenDRIVE continuity")
            if same_source:
                reasons.append("same_span_gap_or_marking_type_bucket_split")
                site_reasons.add("same-span gap/type split")
            record = {
                key: value[key] for key in (
                    "sample_id", "site_id", "anchor_id", "physical_subset", "observation_recipe_id",
                    "recipe_family", "source_full_1_role", "scene", "structural_cell", "view_memberships",
                    "observed_instance_count", "metadata_sha256", "rgb_sha256", "road_mask_visible_sha256",
                )
            }
            record.update({
                "expected_physical_marking_count": expected,
                "context_observed_instance_count_min": min(context_counts),
                "context_observed_instance_count_max": max(context_counts),
                "context_observed_instance_count_mode": stable_mode(context_counts),
                "candidate_reasons": reasons,
                "split_evidence": value["split_evidence"],
                "needs_human_confirmation": True,
            })
            candidate_values.append((value, record))
            affected_samples.append(record)

        if not candidate_values:
            continue
        counts = [value["observed_instance_count"] for value in values]
        physical_subset = values[0]["physical_subset"]
        structural_cell = str(site["structural_cell"])
        structural_intersection_or_ramp = (
            structural_cell.startswith(("Intersection-", "Ramp-"))
            or site.get("feature_kind") in {"intersection_approach", "ramp"}
        )
        complex_structure = (
            values[0]["scene"] in {"intersection", "ramp"}
            or structural_intersection_or_ramp
            or group_count > 1
            or bool(site.get("merge_evidence", {}).get("junction_internal_crossing"))
            or bool(site.get("merge_evidence", {}).get("merge_split_conflict_crossing"))
        )
        if structural_intersection_or_ramp:
            site_reasons.add("frozen structural topology is Intersection/Ramp")
        if group_count > 1:
            site_reasons.add("multiple lane groups/separated carriageway review")
        if world["ambiguous"]:
            eligibility = "ineligible_ambiguous_world_matching"
        elif complex_structure:
            eligibility = "ineligible_complex_structure_requires_human"
        elif world["connection_evidence_complete"] or any(
            value["split_evidence"]["same_source_splits"] for value, _ in candidate_values
        ):
            eligibility = "eligible_unambiguous"
        else:
            eligibility = "ineligible_count_only_no_complete_physical_pair"
        roles = sorted({str(value.get("source_full_1_role")) for value in values})
        views = sorted({view for value in values for view in value["view_memberships"]})
        affected_row = {
            "site_id": site_id,
            "physical_subset": physical_subset,
            "source_full_1_role": roles,
            "view_memberships": views,
            "map_id": site["map_id"],
            "scene": values[0]["scene"],
            "structural_cell": site["structural_cell"],
            "leakage_group_id": site["leakage_group_id"],
            "source_span_count": len(site["source_spans"]),
            "road_ids": sorted({str(span["road_id"]) for span in site["source_spans"]}, key=lambda item: int(item)),
            "section_ids": sorted({int(span["section_id"]) for span in site["source_spans"]}),
            "stitch_edge_count": len(site.get("merge_evidence", {}).get("records", [])),
            "lane_count": lane_count,
            "lane_group_count": group_count,
            "expected_physical_marking_count": expected,
            "observed_instance_count_min": min(counts),
            "observed_instance_count_max": max(counts),
            "observed_instance_count_mode": stable_mode(counts),
            "excess_ratio": round(max(counts) / expected, 6),
            "affected_anchor_count": len({value["anchor_id"] for value, _ in candidate_values}),
            "affected_sample_count": len(candidate_values),
            "candidate_reason": sorted(site_reasons),
            "auto_merge_eligibility": eligibility,
            "needs_human_confirmation": True,
            "world_evidence_status": "ambiguous" if world["ambiguous"] else "unambiguous",
            "junction_internal_crossing": bool(site.get("merge_evidence", {}).get("junction_internal_crossing")),
            "merge_split_conflict_crossing": bool(site.get("merge_evidence", {}).get("merge_split_conflict_crossing")),
            "world_mergeable_pair_count": len(world["mergeable_ref_pairs"]),
            "world_same_span_ambiguous_pair_count": len(world["same_span_ambiguous_pairs"]),
            "current_manifest_structural_cells": sorted({
                value["current_manifest_structural_cell"] for value in values
            }),
        }
        affected_sites.append(affected_row)
        if physical_subset == "test_shared":
            test_shared_details.append({
                "site_id": site_id,
                "anchors": sorted({value["anchor_id"] for value, _ in candidate_values}),
                "view_memberships": views,
                "affected_sample_count": len(candidate_values),
                "samples_by_view": {
                    view: sum(view in value["view_memberships"] for value, _ in candidate_values)
                    for view in view_paths
                },
            })

    affected_samples.sort(key=lambda item: item["sample_id"])
    affected_sites.sort(key=lambda item: item["site_id"])
    output_dir.mkdir(parents=True)
    examples_dir = output_dir / "examples"
    example_records = []
    affected_by_site: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in affected_samples:
        affected_by_site[row["site_id"]].append(row)
    for site_row in affected_sites:
        site_id = site_row["site_id"]
        candidates = affected_by_site[site_id]
        chosen = min(
            candidates,
            key=lambda item: (
                not any(value["sample_id"] == item["sample_id"] and value["is_reference"] for value in site_samples[site_id]),
                not bool(item["split_evidence"]["physical_pair_hits"]),
                -item["observed_instance_count"], item["sample_id"],
            ),
        )
        source = next(value for value in site_samples[site_id] if value["sample_id"] == chosen["sample_id"])
        target_dir = examples_dir / site_id
        rgb_name = f"{chosen['anchor_id']}_{chosen['sample_id']}_rgb.jpg"
        overlay_name = f"{chosen['anchor_id']}_{chosen['sample_id']}_lane_instances_before.png"
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source["rgb_path"], target_dir / rgb_name)
        metadata = load_json(Path(source["metadata_path"]))
        draw_overlay(Path(source["rgb_path"]), metadata, target_dir / overlay_name, int(site_row["expected_physical_marking_count"]))
        if file_sha256(target_dir / rgb_name) != source["rgb_sha256"]:
            raise ConfigError(f"audit example RGB copy checksum drifted: {chosen['sample_id']}")
        example_records.append({
            "site_id": site_id, "anchor_id": chosen["anchor_id"], "sample_id": chosen["sample_id"],
            "rgb": str((target_dir / rgb_name).relative_to(output_dir)),
            "overlay": str((target_dir / overlay_name).relative_to(output_dir)),
            "rgb_sha256": source["rgb_sha256"],
            "overlay_sha256": file_sha256(target_dir / overlay_name),
        })

    _write_sites_csv(output_dir / "affected_sites.csv", affected_sites)
    _write_jsonl(output_dir / "affected_samples.jsonl", affected_samples)
    threshold_record = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "rule_version": AUDIT_RULE_VERSION,
        "count_rules": {
            "ordinary_single_continuous_carriageway": "lane_count + 1",
            "multiple_lane_groups": "lane_count + lane_group_count",
            "multi_source_span": "continuous corridor boundary count, never sum per-span counts",
            "complex_structure": "count_is_candidate_trigger_only",
            "high_priority_excess_ratio": HIGH_PRIORITY_RATIO,
            "context_statistics": "same site_id + anchor_id + recipe_family min/max/mode",
        },
        "world_rules": {
            "waypoint_sampling_spacing_m": WAYPOINT_SPACING_M,
            "endpoint_distance_threshold_m": ENDPOINT_DISTANCE_M,
            "endpoint_heading_difference_threshold_deg": HEADING_DIFFERENCE_DEG,
            "one_to_one": True,
            "lateral_order_preserved": True,
            "ambiguous_best_match": "fail_closed",
            "marking_compatibility": "type_color_width_equal; no unrecorded transition allowed",
            "junction_or_branch_crossing": "forbidden",
        },
        "same_span_rules": {
            "historical_bucket_keys": ["road_id", "section_id", "lane_id", "side", "marking_type"],
            "historical_waypoint_gap_split_m": 5.0,
            "type_transition_without_explicit_compatibility": "human_candidate_no_auto_merge",
        },
        "candidate_site_world_evidence": {
            row["site_id"]: world_evidence[row["site_id"]]
            for row in affected_sites
        },
        "candidate_site_world_evidence_sha256": canonical_json_sha256({
            row["site_id"]: world_evidence[row["site_id"]]
            for row in affected_sites
        }),
        "scene_source": {
            "primary": "Full.2 physical subset/four-view manifests",
            "validated_against": str(scene_dir / "decision.json"),
            "finalization_structural_cell_used_as_paper_scene": False,
        },
        "static_provenance_resolution": {
            "user_declared_static": str(declared_static_dir),
            "canonical_run_bound_static": str(bound_static_dir),
            "same_path": declared_static_dir == bound_static_dir,
            "resolution": "canonical_run_manifest_binding_used_for_sample_ledger_and_manifests",
        },
        "human_gate": "all candidates require explicit confirmed_affected_site_ids before any fix",
        "carla": {"offline_map_only": True, "client_constructed": False, "server_connection": False},
    }
    _write_json(output_dir / "threshold_and_rule_record.json", threshold_record)

    input_checksums_after = _input_file_checksums(
        run_dir, finalization_dir, scene_dir, list(dict.fromkeys([declared_static_dir, bound_static_dir])),
    )
    if input_checksums_before != input_checksums_after:
        raise ConfigError("authoritative input checksums changed during read-only audit")
    output_files = sorted(path for path in output_dir.rglob("*") if path.is_file())
    output_checksums = {
        str(path.relative_to(output_dir)): file_sha256(path)
        for path in output_files
    }
    classifications = Counter(row["auto_merge_eligibility"] for row in affected_sites)
    subset_summary = {}
    for subset in ("train", "val", "test_shared"):
        site_ids = {row["site_id"] for row in affected_sites if row["physical_subset"] == subset}
        sample_rows = [row for row in affected_samples if row["physical_subset"] == subset]
        subset_summary[subset] = {
            "sites": len(site_ids),
            "anchors": len({row["anchor_id"] for row in sample_rows}),
            "samples": len(sample_rows),
        }
    view_affected_counts = {
        view: sum(view in row["view_memberships"] for row in affected_samples)
        for view in view_paths
    }
    summary = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "rule_version": AUDIT_RULE_VERSION,
        "audit_id": audit_id,
        "created_at": created_at,
        "status": "awaiting_user_confirmation_before_fix",
        "hard_stop": "phase_a_complete_no_labels_modified",
        "authoritative_inputs": {
            "full_2_canonical_run": str(run_dir),
            "user_declared_full_2_static": str(declared_static_dir),
            "canonical_bound_full_2_static": str(bound_static_dir),
            "finalization_v2": str(finalization_dir),
            "scene_reclassification": str(scene_dir),
        },
        "preexisting_input_provenance_observations": {
            "finalization_v2_manifest_declared_registry_checksums": {
                name: finalization_declared_checksums.get(name)
                for name in finalization_actual_registry_checksums
            },
            "finalization_v2_actual_registry_checksums": finalization_actual_registry_checksums,
            "finalization_v2_registry_checksum_matches_manifest": finalization_registry_checksum_matches,
            "disposition": (
                "all_match"
                if all(finalization_registry_checksum_matches.values())
                else "preexisting_internal_checksum_mismatch_recorded; current artifact paths used because user explicitly designated them authoritative"
            ),
        },
        "coverage": {
            "metadata_read": len(index_rows),
            "physical_subset_counts": expected_subset_counts,
            "selected_sites": len(selected_site_ids),
            "test_shared_sites": len({row["site_id"] for row in subset_rows["test_shared"]}),
            "test_shared_anchors": len({row["anchor_id"] for row in subset_rows["test_shared"]}),
            "logical_view_counts": view_counts,
        },
        "candidates": {
            "site_count": len(affected_sites),
            "sample_count": len(affected_samples),
            "by_physical_subset": subset_summary,
            "by_auto_merge_eligibility": dict(sorted(classifications.items())),
            "test_shared_hit": bool(test_shared_details),
            "test_shared_details": test_shared_details,
            "affected_test_view_sample_memberships": view_affected_counts,
            "recommended_confirm_or_reject_site_ids": [row["site_id"] for row in affected_sites],
        },
        "immutability": {
            "input_checksums_before": input_checksums_before,
            "input_checksums_after": input_checksums_after,
            "input_critical_files_unchanged": True,
            "rgb_asset_count": len(asset_inventory),
            "road_mask_visible_asset_count": len(asset_inventory),
            "rgb_inventory_sha256": _inventory_digest(asset_inventory, ["sample_id", "rgb_sha256"]),
            "road_mask_visible_inventory_sha256": _inventory_digest(asset_inventory, ["sample_id", "road_mask_visible_sha256"]),
            "metadata_inventory_sha256": _inventory_digest(asset_inventory, ["sample_id", "metadata_sha256"]),
            "labels_modified": False,
        },
        "world_geometry": {
            "map_provenance": map_provenance,
            "site_evidence_sha256": canonical_json_sha256(world_evidence),
            "carla_client_constructed": False,
            "carla_server_connected": False,
        },
        "examples": example_records,
        "artifact_checksums_excluding_summary": output_checksums,
        "artifact_payload_sha256_excluding_summary": canonical_json_sha256(output_checksums),
        "prohibited_actions": {
            "carla_server_started_or_connected": False,
            "image_capture_or_dataset_generation": False,
            "label_mutation": False,
            "baseline_training_or_evaluation": False,
            "files_deleted": False,
            "git_commit": False,
        },
        "required_next_input": "explicit final confirmed_affected_site_ids from user",
    }
    _write_json(output_dir / "audit_summary.json", summary)
    return output_dir
