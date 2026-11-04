"""Versioned V3 physical lane-marking merge and reprojection.

The rule is opt-in and used only by the Full.2 offline label-fix workflow.  It
does not alter the historical V2/R4 collector or projection defaults.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Iterable

from benchmark_generator.config import ConfigError
from benchmark_generator.v3 import lane_instance_audit as audit


RULE_VERSION = "lane_marking_physical_merge_1.0"
PROTOCOL_VERSION = "V3-PLAN-20260809-FULL.2-LABELFIX.1"
VISIBLE_MARGIN_PX = 80.0
MIN_LANE_POINTS = 3
MIN_LANE_ARC_LENGTH_PX = 20.0
SIMPLIFY_MIN_STEP_PX = 2.0


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ref_key(value: dict[str, Any], include_type: bool = True) -> tuple[Any, ...]:
    result: tuple[Any, ...] = (
        int(value["road_id"]), int(value["section_id"]), int(value["lane_id"]), str(value["side"]),
    )
    return result + (str(value.get("marking_type")),) if include_type else result


def _distance(left: Iterable[float], right: Iterable[float]) -> float:
    return math.dist(tuple(float(value) for value in left), tuple(float(value) for value in right))


def _undirected_heading_difference(first: Iterable[float], second: Iterable[float]) -> float:
    a = tuple(float(value) for value in first)
    b = tuple(float(value) for value in second)
    first_angle = math.degrees(math.atan2(a[1], a[0]))
    second_angle = math.degrees(math.atan2(b[1], b[0]))
    difference = abs((first_angle - second_angle + 180.0) % 360.0 - 180.0)
    return min(difference, 180.0 - difference)


def _frozen_connection_is_safe(site: dict[str, Any], record: dict[str, Any]) -> bool:
    merge = site.get("merge_evidence", {})
    if merge.get("junction_internal_crossing") or merge.get("merge_split_conflict_crossing"):
        return False
    if record.get("junction_internal_crossing") or record.get("merge_split_conflict_crossing"):
        return False
    checks = record.get("checks", {})
    required = (
        "endpoint_distance_ok", "heading_difference_ok", "elevation_difference_ok",
        "same_lane_count", "marking_compatible",
    )
    return all(checks.get(key) is True for key in required)


def _edge_key(edge: dict[str, Any]) -> tuple[str, str]:
    return tuple(sorted((str(edge["left_fragment_id"]), str(edge["right_fragment_id"]))))  # type: ignore[return-value]


def _add_unique_edge(edges: list[dict[str, Any]], edge: dict[str, Any]) -> None:
    key = _edge_key(edge)
    existing = next((item for item in edges if _edge_key(item) == key), None)
    if existing is None:
        edges.append(edge)
        return
    if existing["left_endpoint"] != edge["left_endpoint"] or existing["right_endpoint"] != edge["right_endpoint"]:
        raise ConfigError(f"physical boundary pair has conflicting endpoint matches: {key}")
    existing["source_lane_side_match_refs"] = sorted(
        existing.get("source_lane_side_match_refs", []) + edge.get("source_lane_side_match_refs", []),
        key=repr,
    )


def _cross_span_edges(
    *,
    site: dict[str, Any],
    spans: list[dict[str, Any]],
    unit_roads: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    spans_by_road: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for frozen, reconstructed in zip(site["source_spans"], spans):
        spans_by_road[int(frozen["road_id"])].append((frozen, reconstructed))
    edges = []
    connection_audits = []
    for spec in audit._connection_specs(site, unit_roads):
        record = spec["frozen_record"]
        if not _frozen_connection_is_safe(site, record):
            raise ConfigError(f"forbidden junction/branch connection in confirmed site {site['site_id']}")
        left_options = spans_by_road[spec["left_road_id"]]
        right_options = spans_by_road[spec["right_road_id"]]
        if len(left_options) != 1 or len(right_options) != 1:
            raise ConfigError(f"road-to-span connection is not unique: {site['site_id']} {spec}")
        left_frozen, left_span = left_options[0]
        right_frozen, right_span = right_options[0]
        left_fragments = audit._endpoint_fragments({**left_frozen, **left_span}, spec["left_endpoint"])
        right_fragments = audit._endpoint_fragments({**right_frozen, **right_span}, spec["right_endpoint"])
        matching = audit.match_boundary_endpoints(
            left_fragments, right_fragments,
            left_endpoint=spec["left_endpoint"], right_endpoint=spec["right_endpoint"],
        )
        if matching["ambiguous_matches"]:
            raise ConfigError(f"ambiguous cross-span endpoint matching: {site['site_id']} {spec}")
        selected = matching["selected_pairs"]
        if not selected or not all(item["marking_compatible"] for item in selected):
            raise ConfigError(f"no compatible cross-span endpoint matching: {site['site_id']} {spec}")
        left_indexes = [int(item["left_index"]) for item in selected]
        right_indexes = [int(item["right_index"]) for item in selected]
        left_order = sorted(range(len(left_indexes)), key=lambda index: left_indexes[index])
        ordered_right = [right_indexes[index] for index in left_order]
        monotonic = (
            all(first < second for first, second in zip(ordered_right, ordered_right[1:]))
            or all(first > second for first, second in zip(ordered_right, ordered_right[1:]))
            or len(ordered_right) <= 1
        )
        if not monotonic:
            raise ConfigError(f"cross-span lateral order is not monotonic: {site['site_id']} {spec}")
        for pair in selected:
            left = left_fragments[int(pair["left_index"])]
            right = right_fragments[int(pair["right_index"])]
            edge = {
                "edge_kind": "frozen_cross_segment_connection",
                "left_fragment_id": left["fragment_id"],
                "right_fragment_id": right["fragment_id"],
                "left_endpoint": spec["left_endpoint"],
                "right_endpoint": spec["right_endpoint"],
                "endpoint_distance_m": pair["distance_m"],
                "heading_difference_deg": pair["heading_difference_deg"],
                "marking_compatible": True,
                "marking_type_transition": None,
                "frozen_stitch_record_index": spec["record_index"],
                "source_lane_side_match_refs": [
                    {
                        "left": deepcopy(left["source_lane_side_refs"]),
                        "right": deepcopy(right["source_lane_side_refs"]),
                    }
                ],
            }
            _add_unique_edge(edges, edge)
        connection_audits.append({
            "left_road_id": spec["left_road_id"], "right_road_id": spec["right_road_id"],
            "left_endpoint": spec["left_endpoint"], "right_endpoint": spec["right_endpoint"],
            "selected_pair_count": len(selected), "left_boundary_count": len(left_fragments),
            "right_boundary_count": len(right_fragments), "partial_unique_match": not matching["complete_one_to_one"],
            "lateral_order_monotonic": monotonic, "frozen_record": deepcopy(record),
        })
    return edges, connection_audits


def _same_span_edges(
    spans: list[dict[str, Any]],
    *,
    allow_marking_type_transitions: bool,
) -> list[dict[str, Any]]:
    edges = []
    for span in spans:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for fragment in span["fragments"]:
            for ref in fragment["source_lane_side_refs"]:
                grouped[_ref_key(ref, include_type=False)].append(fragment)
        for base_ref, values in sorted(grouped.items()):
            unique = {item["fragment_id"]: item for item in values}
            ordered = sorted(unique.values(), key=lambda item: item["source_s_range"])
            for left, right in zip(ordered, ordered[1:]):
                distance = _distance(left["end_xyz"], right["start_xyz"])
                heading = _undirected_heading_difference(left["end_tangent_xy"], right["start_tangent_xy"])
                type_changed = left["marking_type"] != right["marking_type"]
                color_width_match = (
                    left["marking_color"] == right["marking_color"]
                    and abs(float(left["marking_width_m"]) - float(right["marking_width_m"])) <= 0.01
                )
                if distance > audit.ENDPOINT_DISTANCE_M or heading > audit.HEADING_DIFFERENCE_DEG:
                    continue
                if not color_width_match or (type_changed and not allow_marking_type_transitions):
                    raise ConfigError(
                        f"same-span marking transition is not adjudicated compatible: {span['source_ref']} {base_ref}"
                    )
                edge = {
                    "edge_kind": "same_span_gap_or_marking_type_transition",
                    "left_fragment_id": left["fragment_id"],
                    "right_fragment_id": right["fragment_id"],
                    "left_endpoint": "end",
                    "right_endpoint": "start",
                    "endpoint_distance_m": round(distance, 6),
                    "heading_difference_deg": round(heading, 6),
                    "marking_compatible": True,
                    "marking_type_transition": (
                        {"from": left["marking_type"], "to": right["marking_type"],
                         "authority": "user_confirmed_affected_site_adjudication"}
                        if type_changed else None
                    ),
                    "frozen_stitch_record_index": None,
                    "source_lane_side_match_refs": [{"base_ref": list(base_ref)}],
                }
                _add_unique_edge(edges, edge)
    return edges


def _component_order(
    component: set[str],
    fragments: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for edge in edges:
        left = edge["left_fragment_id"]
        right = edge["right_fragment_id"]
        if left in component and right in component:
            adjacency[left].append((right, edge))
            adjacency[right].append((left, edge))
    if any(len(values) > 2 for values in adjacency.values()):
        raise ConfigError("a source boundary belongs to a branched physical marking chain")
    endpoints = sorted(node for node in component if len(adjacency[node]) <= 1)
    if len(component) > 1 and len(endpoints) != 2:
        raise ConfigError("physical marking component is cyclic or has ambiguous endpoints")
    start = endpoints[0] if endpoints else min(component)
    order = []
    ordered_edges = []
    previous = None
    current = start
    while current is not None:
        order.append(current)
        options = [(node, edge) for node, edge in adjacency[current] if node != previous]
        if not options:
            break
        if len(options) != 1:
            raise ConfigError("physical marking traversal is ambiguous")
        next_node, edge = options[0]
        ordered_edges.append(edge)
        previous, current = current, next_node
    if set(order) != component:
        raise ConfigError("physical marking chain traversal is incomplete")
    return order, ordered_edges


def build_fragment_chains(
    fragments: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    site_id: str,
) -> list[dict[str, Any]]:
    """Build unique non-branching physical chains from world-space fragments."""

    by_id = {item["fragment_id"]: item for item in fragments}
    if len(by_id) != len(fragments):
        raise ConfigError(f"duplicate source fragment identity: {site_id}")
    edge_keys = set()
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        left, right = edge["left_fragment_id"], edge["right_fragment_id"]
        if left not in by_id or right not in by_id or left == right:
            raise ConfigError(f"invalid physical marking edge: {site_id} {edge}")
        key = tuple(sorted((left, right)))
        if key in edge_keys:
            raise ConfigError(f"duplicate physical marking edge: {site_id} {key}")
        edge_keys.add(key)
        adjacency[left].add(right)
        adjacency[right].add(left)
    if any(len(values) > 2 for values in adjacency.values()):
        raise ConfigError(f"junction/merge/split branch in physical marking graph: {site_id}")
    unseen = set(by_id)
    chains = []
    while unseen:
        seed = min(unseen)
        component = set()
        stack = [seed]
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency[node] - component)
        unseen -= component
        order, ordered_edges = _component_order(component, by_id, edges)
        source_refs = sorted(
            {tuple(_ref_key(ref)) for node in order for ref in by_id[node]["source_lane_side_refs"]},
            key=repr,
        )
        source_segments = [
            {
                "fragment_id": node,
                "span_index": by_id[node]["span_index"],
                "source_ref": by_id[node]["source_ref"],
                "source_s_range": deepcopy(by_id[node]["source_s_range"]),
            }
            for node in order
        ]
        geometry = [
            [round(float(value), 2) for value in point]
            for node in order for point in by_id[node]["points_xyz"]
        ]
        identity = {
            "site_id": site_id,
            "source_lane_side_refs": [list(value) for value in source_refs],
            "source_segment_refs": source_segments,
            "world_geometry": geometry,
        }
        chain_id = f"physical_chain_{canonical_json_sha256(identity)[:20]}"
        chains.append({
            "chain_id": chain_id,
            "physical_marking_key": f"corridor_{site_id}_{canonical_json_sha256(identity)[:24]}",
            "fragment_ids": order,
            "merge_edges": deepcopy(ordered_edges),
            "source_lane_side_refs": [
                {"road_id": value[0], "section_id": value[1], "lane_id": value[2],
                 "side": value[3], "marking_type": value[4]}
                for value in source_refs
            ],
            "source_segment_refs": source_segments,
            "source_road_section_refs": sorted({
                f"road:{by_id[node]['road_id']}:section:{by_id[node]['section_id']}" for node in order
            }),
            "marking_type_transitions": [
                deepcopy(edge["marking_type_transition"])
                for edge in ordered_edges if edge.get("marking_type_transition") is not None
            ],
        })
    fragment_use = Counter(node for chain in chains for node in chain["fragment_ids"])
    if any(count != 1 for count in fragment_use.values()) or set(fragment_use) != set(by_id):
        raise ConfigError(f"source boundary chain assignment is not unique and complete: {site_id}")
    return sorted(chains, key=lambda item: item["physical_marking_key"])


def reconstruct_site_physical_chains(
    *,
    carla: Any,
    waypoints: list[Any],
    site: dict[str, Any],
    road_library: dict[str, Any],
    allow_marking_type_transitions: bool,
    drop_source_ref: tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    spans = [
        audit._collect_span_fragments(carla, waypoints, span, index)
        for index, span in enumerate(site["source_spans"])
    ]
    unit_roads = audit._unit_road_map(road_library)
    cross_edges, connection_audits = _cross_span_edges(site=site, spans=spans, unit_roads=unit_roads)
    same_span_edges = _same_span_edges(
        spans, allow_marking_type_transitions=allow_marking_type_transitions,
    )
    fragments = [fragment for span in spans for fragment in span["fragments"]]
    dropped = []
    if drop_source_ref is not None:
        retained = []
        for fragment in fragments:
            refs = {_ref_key(ref) for ref in fragment["source_lane_side_refs"]}
            if tuple(drop_source_ref) in refs:
                dropped.append(fragment["fragment_id"])
            else:
                retained.append(fragment)
        fragments = retained
    fragment_ids = {item["fragment_id"] for item in fragments}
    edges = [
        edge for edge in cross_edges + same_span_edges
        if edge["left_fragment_id"] in fragment_ids and edge["right_fragment_id"] in fragment_ids
    ]
    chains = build_fragment_chains(fragments, edges, site_id=site["site_id"])
    lane_group_values = [span["lane_groups"] for span in spans if span["lane_groups"]]
    lane_group_count = audit.stable_mode(len(value) for value in lane_group_values)
    expected = audit.expected_marking_count(int(site["total_driving_lanes"]), lane_group_count)
    if len(chains) != expected:
        raise ConfigError(
            f"physical marking chain count mismatch after adjudicated merge: {site['site_id']} "
            f"expected={expected} actual={len(chains)} dropped={dropped}"
        )
    return {
        "rule_version": RULE_VERSION,
        "site_id": site["site_id"],
        "lane_count": int(site["total_driving_lanes"]),
        "lane_group_count": lane_group_count,
        "lane_groups": lane_group_values,
        "expected_physical_marking_count": expected,
        "fragments": {item["fragment_id"]: item for item in fragments},
        "chains": chains,
        "edges": edges,
        "connection_audits": connection_audits,
        "dropped_fragment_ids": dropped,
        "junction_internal_crossing": bool(site.get("merge_evidence", {}).get("junction_internal_crossing")),
        "merge_split_conflict_crossing": bool(site.get("merge_evidence", {}).get("merge_split_conflict_crossing")),
    }


def _camera_intrinsic(width: int, height: int, fov: float) -> Any:
    import numpy as np  # type: ignore

    focal = width / (2.0 * math.tan(math.radians(fov) / 2.0))
    return np.asarray([[focal, 0.0, width / 2.0], [0.0, focal, height / 2.0], [0.0, 0.0, 1.0]])


def _project_xyz(point: Iterable[float], world_to_camera: Any, intrinsic: Any) -> tuple[float, float, float] | None:
    import numpy as np  # type: ignore

    values = tuple(float(value) for value in point)
    world = np.asarray([values[0], values[1], values[2], 1.0])
    camera = world_to_camera @ world
    cv_point = np.asarray([camera[1], -camera[2], camera[0]])
    if cv_point[2] <= 0.05:
        return None
    image = intrinsic @ cv_point
    return float(image[0] / image[2]), float(image[1] / image[2]), float(cv_point[2])


def _arc_length(points: list[list[float]]) -> float:
    return sum(math.dist(first, second) for first, second in zip(points, points[1:]))


def _simplify(points: list[list[float]]) -> list[list[float]]:
    result = []
    last = None
    for point in points:
        if last is None or math.dist(last, point) >= SIMPLIFY_MIN_STEP_PX:
            result.append(point)
            last = point
    return result


def _edge_for_nodes(edges: list[dict[str, Any]], left: str, right: str) -> dict[str, Any]:
    key = tuple(sorted((left, right)))
    return next(edge for edge in edges if _edge_key(edge) == key)


def _oriented_fragment_points(
    *,
    chain: dict[str, Any],
    node_index: int,
    fragment: dict[str, Any],
    edges: list[dict[str, Any]],
    local_s_range: list[float],
) -> list[tuple[float, float, float]]:
    low, high = min(local_s_range), max(local_s_range)
    samples = [
        (float(s_value), tuple(float(value) for value in point))
        for s_value, point in zip(fragment["points_s"], fragment["points_xyz"])
        if low - 0.1 <= float(s_value) <= high + 0.1
    ]
    if len(samples) < 2:
        return []
    previous = chain["fragment_ids"][node_index - 1] if node_index > 0 else None
    following = chain["fragment_ids"][node_index + 1] if node_index + 1 < len(chain["fragment_ids"]) else None
    reverse = False
    if previous is not None:
        edge = _edge_for_nodes(edges, previous, fragment["fragment_id"])
        endpoint = edge["left_endpoint"] if edge["left_fragment_id"] == fragment["fragment_id"] else edge["right_endpoint"]
        reverse = endpoint == "end"
    elif following is not None:
        edge = _edge_for_nodes(edges, fragment["fragment_id"], following)
        endpoint = edge["left_endpoint"] if edge["left_fragment_id"] == fragment["fragment_id"] else edge["right_endpoint"]
        reverse = endpoint == "start"
    points = [point for _, point in samples]
    return list(reversed(points)) if reverse else points


def project_physical_chains(
    *,
    carla: Any,
    site_geometry: dict[str, Any],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    camera = metadata["actual_conditions"]["camera"]
    transform_value = camera["transform_world"]
    location = transform_value["location"]
    rotation = transform_value["rotation"]
    transform = carla.Transform(
        carla.Location(x=float(location["x"]), y=float(location["y"]), z=float(location["z"])),
        carla.Rotation(pitch=float(rotation["pitch"]), yaw=float(rotation["yaw"]), roll=float(rotation["roll"])),
    )
    import numpy as np  # type: ignore

    world_to_camera = np.asarray(transform.get_inverse_matrix(), dtype=float)
    width = int(camera["image_width"])
    height = int(camera["image_height"])
    fov = float(camera["fov_degrees"])
    intrinsic = _camera_intrinsic(width, height, fov)
    constructions = metadata["labels"]["lane_polyline_projected"].get("corridor_construction", [])
    if len(constructions) != len(site_geometry["lane_groups"]):
        raise ConfigError(
            f"corridor-construction/source-span count mismatch: {metadata['sample_id']} "
            f"{len(constructions)} != {len(site_geometry['lane_groups'])}"
        )
    local_ranges = {
        index: [float(value) for value in construction["local_s_range"]]
        for index, construction in enumerate(constructions)
    }
    instances = []
    for chain in site_geometry["chains"]:
        world_points = []
        for index, fragment_id in enumerate(chain["fragment_ids"]):
            fragment = site_geometry["fragments"][fragment_id]
            points = _oriented_fragment_points(
                chain=chain, node_index=index, fragment=fragment, edges=site_geometry["edges"],
                local_s_range=local_ranges[int(fragment["span_index"])],
            )
            for point in points:
                if world_points and _distance(world_points[-1], point) <= 0.05:
                    continue
                world_points.append(point)
        projected = []
        for point in world_points:
            value = _project_xyz(point, world_to_camera, intrinsic)
            if value is None:
                continue
            x, y, _depth = value
            if -VISIBLE_MARGIN_PX <= x < width + VISIBLE_MARGIN_PX and -VISIBLE_MARGIN_PX <= y < height + VISIBLE_MARGIN_PX:
                projected.append([round(x, 3), round(y, 3)])
        projected = _simplify(projected)
        if len(projected) < MIN_LANE_POINTS or _arc_length(projected) < MIN_LANE_ARC_LENGTH_PX:
            continue
        source = chain["source_lane_side_refs"][0]
        marking_types = sorted({item["marking_type"] for item in chain["source_lane_side_refs"]})
        instances.append({
            "instance_id": None,
            "instance_semantics": "unique_corridor_level_physical_marking",
            "physical_marking_key": chain["physical_marking_key"],
            "physical_chain_id": chain["chain_id"],
            "road_id": source["road_id"],
            "section_id": source["section_id"],
            "lane_id": source["lane_id"],
            "side": source["side"],
            "marking_type": marking_types[0] if len(marking_types) == 1 else "Mixed",
            "points_image": projected,
            "source_type": "program_derived",
            "source_detail": "frozen_opendrive_corridor_level_physical_marking_reprojection",
            "source_lane_side_refs": deepcopy(chain["source_lane_side_refs"]),
            "source_segment_refs": deepcopy(chain["source_segment_refs"]),
            "source_road_section_refs": deepcopy(chain["source_road_section_refs"]),
            "merge_edges": deepcopy(chain["merge_edges"]),
            "marking_type_transitions": deepcopy(chain["marking_type_transitions"]),
            "visibility_semantics": "projected_in_image_no_occlusion_test",
            "projection_rule_version": RULE_VERSION,
        })
    instances.sort(key=lambda item: item["physical_marking_key"])
    for index, instance in enumerate(instances):
        instance["instance_id"] = f"lane_marking_{index:04d}"
    return instances


def label_checksum(instances: list[dict[str, Any]]) -> str:
    return canonical_json_sha256(instances)
