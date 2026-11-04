"""QA summary skeletons for Phase D devtest preparation."""

from __future__ import annotations

from typing import Any


def expected_contact_sheet_layers() -> list[str]:
    return [
        "rgb",
        "road_mask_full",
        "semantic_road_mask_proxy",
        "lane_polyline_projected_overlay",
        "road_heading_overlay",
    ]


def build_preflight_qa_summary(run_id: str, weather_preset: str, camera_mount_mode: str) -> dict[str, Any]:
    checks = {
        "road_mask_full": "planned_not_run",
        "road_boundary_full": "planned_not_run",
        "road_heading": "planned_not_run",
        "lane_polyline_projected": "planned_not_run",
        "semantic_road_mask_proxy": "planned_not_run",
        "sample_metadata": "planned_not_run",
        "qa_summary": "planned_not_run",
    }
    return {
        "schema_version": "benchmark_v1_qa_summary_0.1",
        "run_id": run_id,
        "environment": {
            "weather_preset": weather_preset,
            "traffic_profile": "none",
        },
        "camera": {
            "mount_mode": camera_mount_mode,
            "camera_grid_name": "town04_opt_phase_d_devtest",
        },
        "qa_scope": {
            "mode": "phase_d_preflight",
            "carla_connection": "not_used_in_prep",
            "frame_generation": "not_run_in_prep",
        },
        "frame_counts": {"total": 0, "pass": 0, "warn": 0, "reject": 0, "not_run": 0},
        "required_checks": checks,
        "contact_sheet_layers": expected_contact_sheet_layers(),
        "not_available_fields": [
            "road_mask_full",
            "road_boundary_full",
            "road_heading",
            "lane_polyline_projected",
            "semantic_road_mask_proxy",
        ],
        "future_extension_fields": [
            "lane_polyline_visible_true",
            "road_mask_visible_true",
            "SUMO",
            "asset_level_appearance",
            "Sim2Real",
        ],
        "status": "not_run",
        "rejection_reasons": [],
    }
