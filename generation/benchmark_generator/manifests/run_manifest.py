"""Run manifest builders for Phase D preflight."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def build_preflight_run_manifest(config: dict[str, Any], run_id: str, argv: list[str]) -> dict[str, Any]:
    map_cfg = config["maps"][0]
    return {
        "schema_version": "benchmark_v1_run_manifest_0.1",
        "run_id": run_id,
        "created_at": "not_created_static_preflight",
        "command_boundary": {
            "carla_connection": "requires_user_approval_and_confirm_flag",
            "frame_generation": "requires_user_approval_and_confirm_flag",
            "label_conversion_new_raw_data": "not_run",
            "training": "not_run",
            "formal_split": "not_run",
        },
        "maps": [
            {
                "map_name": map_cfg["map_name"],
                "opendrive_path": map_cfg["opendrive_path"],
            }
        ],
        "environment": {
            "weather_preset": config["environment"]["weather_preset"],
            "weather_presets_allowed": config["environment"].get("weather_presets", []),
            "traffic_profile": config["traffic"]["profile"],
            "traffic_source": config["traffic"]["source"],
        },
        "camera": {
            "mount_mode": config["camera"]["mount_mode"],
            "grid": config["camera"]["grid"],
        },
        "output": {
            "root": str(Path(config["output"]["root"]).resolve()),
            "path_policy": {
                "allowed_roots": config["output"]["allowed_roots"],
                "forbidden_legacy_roots": ["CarlaProject/CarlaDataset/UAVLanesSimple_v0"],
            },
        },
        "implemented_fields": [
            "road_mask_full",
            "road_boundary_full",
            "road_heading",
            "lane_polyline_projected",
            "semantic_road_mask_proxy",
            "weather_preset",
            "camera_mount_mode",
            "sample_metadata",
            "qa_summary",
        ],
        "missing_fields": [
            "lane_polyline_visible_true",
            "road_mask_visible_true",
            "actor_occlusion_ratio",
            "sumo_lane_occupancy",
            "asset_level_appearance",
            "sim2real_split",
            "formal_train_val_test_split",
        ],
        "future_extension_fields": config.get("future_extension_fields", []),
        "script": {
            "entrypoint": "BenchmarkProject/tools/run_carla_devtest.py",
            "argv": argv,
            "required_python": sys.executable,
        },
    }
