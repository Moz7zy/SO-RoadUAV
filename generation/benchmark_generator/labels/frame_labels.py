"""Frame-label builders aligned with the Phase A-C schema.

These builders create schema-aligned placeholder records for Phase D planning.
They do not generate images or labels; CARLA execution must fill available
payloads later under explicit user approval.
"""

from __future__ import annotations

from typing import Any


def required_label_names() -> list[str]:
    return [
        "road_mask_full",
        "road_boundary_full",
        "road_heading",
        "lane_polyline_projected",
        "semantic_road_mask_proxy",
    ]


def build_not_available_frame_label(
    sample_id: str,
    map_name: str,
    corridor_id: str,
    weather_preset: str,
    camera_mount_mode: str,
    reason: str,
    source_policy_phase: str = "phase_d_preparation_only",
    qa_rejection_reason: str = "phase_d_preparation_only",
) -> dict[str, Any]:
    return {
        "schema_version": "benchmark_v1_frame_label_0.1",
        "sample_id": sample_id,
        "image": {"rgb_path": "", "width": 1280, "height": 720},
        "camera": {
            "mount_mode": camera_mount_mode,
            "sampling_mode": "corridor_centerline_static",
            "intrinsics": {"fx": 640.0, "fy": 640.0, "cx": 640.0, "cy": 360.0, "fov_degrees": 90.0},
            "transform_world": {},
        },
        "environment": {
            "weather_preset": weather_preset,
            "lighting_preset": "noon",
            "traffic_profile": "none",
            "traffic_source": "none",
        },
        "map": {
            "map_name": map_name,
            "opendrive_source": f"{map_name}.xodr",
            "corridor_id": corridor_id,
            "road_ids": [],
            "section_ids": [],
            "lane_ids": [],
        },
        "labels": {
            "road_mask_full": _path_label("road_mask_full", "official_prediction_target", "road_mask_full_path", reason),
            "road_boundary_full": {
                "protocol_name": "road_boundary_full",
                "label_role": "official_prediction_target",
                "source_type": "not_available",
                "availability": "not_available",
                "storage_data_field": "road_boundary_full",
                "left": [],
                "right": [],
                "not_available_reason": reason,
            },
            "road_heading": {
                "protocol_name": "road_heading",
                "label_role": "official_prediction_target",
                "source_type": "not_available",
                "availability": "not_available",
                "storage_data_field": "road_heading",
                "world_yaw_deg": None,
                "image_angle_deg": None,
                "image_vector": None,
                "not_available_reason": reason,
            },
            "lane_polyline_projected": {
                "protocol_name": "lane_polyline_projected",
                "label_role": "official_prediction_target",
                "source_type": "not_available",
                "availability": "not_available",
                "storage_data_field": "lane_polylines_projected",
                "instances": [],
                "not_available_reason": reason,
            },
            "semantic_road_mask_proxy": _path_label(
                "semantic_road_mask_proxy",
                "proxy_label",
                "semantic_road_mask_proxy_path",
                reason,
            ),
        },
        "sample_metadata": {
            "label_sources": {
                "road_mask_full": "not_available",
                "road_boundary_full": "not_available",
                "road_heading": "not_available",
                "lane_polyline_projected": "not_available",
                "semantic_road_mask_proxy": "not_available",
            },
            "label_field_mapping": {
                "road_mask_full": "labels.road_mask_full.path",
                "road_boundary_full": "labels.road_boundary_full",
                "road_heading": "labels.road_heading",
                "lane_polyline_projected": "labels.lane_polyline_projected.instances",
                "semantic_road_mask_proxy": "labels.semantic_road_mask_proxy.path",
            },
            "source_policy": {
                "phase": source_policy_phase,
                "true_visible": "not_available",
                "lane_polyline_instance_semantics": "unique_physical_marking",
                "road_edge_marking_policy": "included_once_as_unique_physical_marking",
                "coordinate_domain": "planned_image_projection_with_bounded_margin_for_lane_polylines_unclipped_corridor_boundaries",
            },
            "not_available_fields": required_label_names(),
            "future_extension_fields": [
                "lane_polyline_visible_true",
                "road_mask_visible_true",
                "SUMO",
                "asset_level_appearance",
                "Sim2Real",
            ],
        },
        "qa": {"status": "not_run", "rejection_reasons": [qa_rejection_reason]},
    }


def _path_label(protocol_name: str, role: str, storage_path_field: str, reason: str) -> dict[str, Any]:
    return {
        "protocol_name": protocol_name,
        "label_role": role,
        "source_type": "not_available",
        "availability": "not_available",
        "storage_path_field": storage_path_field,
        "path": None,
        "not_available_reason": reason,
    }
