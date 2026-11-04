"""Frozen constants for the Benchmark-V3 generation boundary.

The output root is configured by ``generate_dataset.py`` through
``BENCHMARK_V3_OUTPUT_ROOT`` before any generator submodule is imported; every
artifact path below is derived from it at import time.
"""

import os

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
V3_CONFIG_ROOT = PACKAGE_ROOT / "configs"
V3_SCHEMA_ROOT = PACKAGE_ROOT / "schemas" / "v3"
INPUTS_ROOT = PACKAGE_ROOT / "inputs"

OUTPUT_ROOT_ENV = "BENCHMARK_V3_OUTPUT_ROOT"
V3_ARTIFACT_ROOT = Path(
    os.environ.get(OUTPUT_ROOT_ENV, PACKAGE_ROOT / "_work")
).resolve()


def configure_output_root(path) -> Path:
    """Bind the output root. Must run before importing generator submodules."""

    resolved = Path(path).resolve()
    os.environ[OUTPUT_ROOT_ENV] = str(resolved)
    return resolved


def output_root() -> Path:
    return V3_ARTIFACT_ROOT

PROTOCOL_VERSION = "V3-PLAN-20260722"
CONFIG_SCHEMA_VERSION = "benchmark_v3_s0_config_0.1"
QA_POLICY_SCHEMA_VERSION = "benchmark_v3_qa_policy_config_0.1"
CHALLENGE_SCHEMA_VERSION = "benchmark_v3_challenge_l18_0.1"
ROAD_LIBRARY_SCHEMA_VERSION = "benchmark_v3_road_library_0.1"
SITE_REGISTRY_SCHEMA_VERSION = "benchmark_v3_site_registry_0.1"
ANCHOR_REGISTRY_SCHEMA_VERSION = "benchmark_v3_anchor_registry_0.1"
RECIPE_REGISTRY_SCHEMA_VERSION = "benchmark_v3_recipe_registry_0.1"
LEDGER_SCHEMA_VERSION = "benchmark_v3_expected_sample_ledger_0.1"
MANIFEST_ROW_SCHEMA_VERSION = "benchmark_v3_manifest_row_0.1"
FRAME_LABEL_SCHEMA_VERSION = "benchmark_v3_frame_label_0.1"
PLAN_SCHEMA_VERSION = "benchmark_v3_s0_plan_0.1"
RUN_STATUS_SCHEMA_VERSION = "benchmark_v3_s0_run_status_0.1"
S1_CONFIG_SCHEMA_VERSION = "benchmark_v3_s1_config_0.1"
ROAD_LIBRARY_INVENTORY_SCHEMA_VERSION = "benchmark_v3_road_library_inventory_0.1"
LEAKAGE_GROUP_REGISTRY_SCHEMA_VERSION = "benchmark_v3_leakage_group_registry_0.1"
CANDIDATE_SITE_REGISTRY_SCHEMA_VERSION = "benchmark_v3_candidate_site_registry_0.1"
CANDIDATE_ANCHOR_REGISTRY_SCHEMA_VERSION = "benchmark_v3_candidate_anchor_registry_0.1"
INVENTORY_COVERAGE_SCHEMA_VERSION = "benchmark_v3_inventory_coverage_0.1"
SITE_CARD_SCHEMA_VERSION = "benchmark_v3_site_card_0.1"
SITE_REVIEW_DECISION_SCHEMA_VERSION = "benchmark_v3_site_review_decision_0.1"
PREVIEW_LEDGER_SCHEMA_VERSION = "benchmark_v3_preview_ledger_0.1"
P0_DRY_RUN_PLAN_SCHEMA_VERSION = "benchmark_v3_p0_dry_run_plan_0.1"
S1_PLAN_SCHEMA_VERSION = "benchmark_v3_s1_plan_0.1"
S1_RUN_STATUS_SCHEMA_VERSION = "benchmark_v3_s1_run_status_0.1"
S1_1_PROTOCOL_VERSION = "V3-PLAN-20260722-R2"
S1_1_CONFIG_SCHEMA_VERSION = "benchmark_v3_s1_1_config_0.1"
S1_1_ROAD_LIBRARY_SCHEMA_VERSION = "benchmark_v3_s1_1_road_library_0.1"
S1_1_SITE_REGISTRY_SCHEMA_VERSION = "benchmark_v3_s1_1_candidate_site_registry_0.1"
S1_1_ANCHOR_REGISTRY_SCHEMA_VERSION = "benchmark_v3_s1_1_candidate_anchor_registry_0.1"
S1_1_LEAKAGE_REGISTRY_SCHEMA_VERSION = "benchmark_v3_s1_1_leakage_group_registry_0.1"
S1_1_COVERAGE_SCHEMA_VERSION = "benchmark_v3_s1_1_inventory_coverage_0.1"
S1_1_CAPACITY_SCHEMA_VERSION = "benchmark_v3_s1_1_capacity_simulation_0.1"
S1_1_COMPARISON_SCHEMA_VERSION = "benchmark_v3_s1_1_inventory_comparison_0.1"
S1_1_SITE_CARD_SCHEMA_VERSION = "benchmark_v3_s1_1_site_card_0.1"
S1_1_PREVIEW_LEDGER_SCHEMA_VERSION = "benchmark_v3_s1_1_preview_ledger_0.1"
S1_1_PLAN_SCHEMA_VERSION = "benchmark_v3_s1_1_plan_0.1"
S1_1_RUN_STATUS_SCHEMA_VERSION = "benchmark_v3_s1_1_run_status_0.1"
PREVIEW_PROTOCOL_VERSION = "V3-PLAN-20260723-R3"
PREVIEW_CONFIG_SCHEMA_VERSION = "benchmark_v3_level1_preview_config_0.1"
LEVEL1_PREVIEW_LEDGER_SCHEMA_VERSION = "benchmark_v3_level1_preview_ledger_0.1"
PREVIEW_SMOKE_PLAN_SCHEMA_VERSION = "benchmark_v3_preview_smoke_plan_0.1"
HARD_REJECT_AUDIT_SCHEMA_VERSION = "benchmark_v3_hard_reject_audit_0.1"
PREVIEW_METADATA_SCHEMA_VERSION = "benchmark_v3_preview_metadata_0.1"
PREVIEW_REVIEW_INDEX_SCHEMA_VERSION = "benchmark_v3_preview_review_index_0.1"
PREVIEW_RUN_MANIFEST_SCHEMA_VERSION = "benchmark_v3_preview_run_manifest_0.1"
PREVIEW_RUN_STATUS_SCHEMA_VERSION = "benchmark_v3_preview_run_status_0.1"
SITE_REVIEW_PACKAGE_SCHEMA_VERSION = "benchmark_v3_site_review_package_0.1"
SITE_REVIEW_PACKAGE_DECISION_SCHEMA_VERSION = "benchmark_v3_site_review_decision_0.2"
REVIEW_SHORT_ID_MAP_SCHEMA_VERSION = "benchmark_v3_review_short_id_map_0.1"
REVIEW_CARD_SCHEMA_VERSION = "benchmark_v3_review_card_0.1"
HARD_DIAGNOSTIC_PLAN_SCHEMA_VERSION = "benchmark_v3_hard_diagnostic_plan_0.1"
SHORT_ID_RULE_VERSION = "benchmark_v3_review_short_id_sort_map_site_0.1"
HARD_DIAGNOSTIC_RULE_VERSION = "benchmark_v3_hard_diagnostic_planner_0.1"
HARD_DIAGNOSTIC_CONFIG_SCHEMA_VERSION = "benchmark_v3_hard_diagnostic_config_0.1"
HARD_DIAGNOSTIC_METADATA_SCHEMA_VERSION = "benchmark_v3_hard_diagnostic_metadata_0.1"
HARD_DIAGNOSTIC_RUN_SCHEMA_VERSION = "benchmark_v3_hard_diagnostic_run_0.1"
HARD_DIAGNOSTIC_CAPTURE_RULE_VERSION = "benchmark_v3_hard_diagnostic_capture_0.1"

# Offline human-review resolution and hard-recovery planning.  These records
# are deliberately separate from the immutable S1.1/Preview/diagnostic
# inputs; they never imply that the authoritative registry has been frozen.
SITE_REVIEW_RESOLUTION_CONFIG_SCHEMA_VERSION = "benchmark_v3_site_review_resolution_config_0.1"
SITE_REVIEW_RESOLUTION_MANIFEST_SCHEMA_VERSION = "benchmark_v3_site_review_resolution_manifest_0.1"
SITE_REVIEW_RESOLUTION_RECORD_SCHEMA_VERSION = "benchmark_v3_site_review_resolution_record_0.1"
STRICT_RECHECK_SCHEMA_VERSION = "benchmark_v3_strict_recheck_0.1"
HARD_RECOVERY_PLAN_SCHEMA_VERSION = "benchmark_v3_hard_recovery_plan_0.1"
HARD_RECOVERY_PREVIEW_LEDGER_SCHEMA_VERSION = "benchmark_v3_hard_recovery_preview_ledger_0.1"
SITE_REVIEW_RESOLUTION_RULE_VERSION = "benchmark_v3_site_review_resolution_0.1"
STRICT_RECHECK_RULE_VERSION = "benchmark_v3_strict_recheck_0.1"
HARD_RECOVERY_RULE_VERSION = "benchmark_v3_hard_recovery_planning_0.1"

# The recovery Preview selector is deliberately a closed set.  Keep these
# identifiers in code as well as in the immutable resolution artifact so a
# future config cannot accidentally widen the capture scope to all hard or
# all non-hard records.
HARD_RECOVERY_PREVIEW_CONFIG_SCHEMA_VERSION = "benchmark_v3_hard_recovery_preview_config_0.1"
HARD_RECOVERY_PREVIEW_PLAN_SCHEMA_VERSION = "benchmark_v3_hard_recovery_preview_plan_0.1"
HARD_RECOVERY_PREVIEW_METADATA_SCHEMA_VERSION = "benchmark_v3_hard_recovery_preview_metadata_0.1"
HARD_RECOVERY_PREVIEW_CONFLICT_SCHEMA_VERSION = "benchmark_v3_hard_recovery_preview_conflict_0.1"
HARD_RECOVERY_PREVIEW_RULE_VERSION = "benchmark_v3_hard_recovery_preview_0.1"
HARD_RECOVERY_PREVIEW_SELECTION_RULE_VERSION = "benchmark_v3_hard_recovery_selection_0.1"
HARD_RECOVERY_PREVIEW_SHORT_IDS = (
    "H001", "H003", "H007", "H008", "H010", "H016", "H017", "H019",
    "H024", "H025", "H028", "H030", "H037", "H043", "H048", "H063", "H084",
)
HARD_RECOVERY_PREVIEW_EXCLUDED_SHORT_IDS = ("H050", "H064", "H073")

ID_RULE_VERSION = "benchmark_v3_identity_0.1"
DEVELOPMENT_RECIPE_RULE_VERSION = "benchmark_v3_development_stratified_random_0.1"
CORE_RECIPE_RULE_VERSION = "benchmark_v3_core_13_condition_0.1"
CHALLENGE_RECIPE_RULE_VERSION = "benchmark_v3_challenge_l18_0.1"
SITE_INTERFACE_RULE_VERSION = "benchmark_v3_site_anchor_interface_0.1"
OPENDRIVE_INVENTORY_RULE_VERSION = "benchmark_v3_opendrive_inventory_0.1"
INVENTORY_ID_RULE_VERSION = "benchmark_v3_inventory_identity_0.1"
SITE_CLASSIFICATION_RULE_VERSION = "benchmark_v3_site_classification_0.1"
LEAKAGE_GROUP_RULE_VERSION = "benchmark_v3_leakage_group_0.1"
ANCHOR_PLACEMENT_RULE_VERSION = "benchmark_v3_anchor_placement_0.1"
PREVIEW_LEDGER_RULE_VERSION = "benchmark_v3_preview_ledger_0.1"
P0_PREFLIGHT_RULE_VERSION = "benchmark_v3_p0_preflight_0.1"
S1_1_SEGMENTATION_RULE_VERSION = "benchmark_v3_local_homogeneous_segmentation_0.1"
S1_1_STITCHING_RULE_VERSION = "benchmark_v3_corridor_stitching_0.1"
S1_1_ELIGIBILITY_RULE_VERSION = "benchmark_v3_site_eligibility_proposal_0.1"
S1_1_ANCHOR_RULE_VERSION = "benchmark_v3_spatial_anchor_placement_0.1"
S1_1_CAPACITY_RULE_VERSION = "benchmark_v3_capacity_simulation_0.1"
LEVEL1_PREVIEW_RULE_VERSION = "benchmark_v3_level1_site_preview_0.1"
PREVIEW_SMOKE_SELECTION_RULE_VERSION = "benchmark_v3_preview_smoke_selection_0.1"
PREVIEW_RECOVERY_RULE_VERSION = "benchmark_v3_preview_recovery_0.1"
EXPOSURE_CALIBRATION_CONFIG_SCHEMA_VERSION = "benchmark_v3_exposure_calibration_config_0.1"
EXPOSURE_CALIBRATION_RUN_SCHEMA_VERSION = "benchmark_v3_exposure_calibration_run_0.1"

# Final human-review closure, immutable registry/split freeze, and the next
# offline P0 preparation boundary.  These versions are intentionally separate
# from the proposal-only S1.1 and Preview contracts.
FINALIZATION_PROTOCOL_VERSION = "V3-PLAN-20260726-R4"
FINALIZATION_CONFIG_SCHEMA_VERSION = "benchmark_v3_finalization_config_0.1"
FINALIZATION_MANIFEST_SCHEMA_VERSION = "benchmark_v3_finalization_manifest_0.1"
FINALIZATION_RECORD_SCHEMA_VERSION = "benchmark_v3_finalization_record_0.1"
FROZEN_ROAD_LIBRARY_SCHEMA_VERSION = "benchmark_v3_frozen_road_library_0.1"
FROZEN_SITE_REGISTRY_SCHEMA_VERSION = "benchmark_v3_frozen_site_registry_0.1"
FROZEN_ANCHOR_REGISTRY_SCHEMA_VERSION = "benchmark_v3_frozen_anchor_registry_0.1"
FROZEN_LEAKAGE_GROUP_REGISTRY_SCHEMA_VERSION = "benchmark_v3_frozen_leakage_group_registry_0.1"
DETERMINISTIC_SPLIT_SCHEMA_VERSION = "benchmark_v3_deterministic_split_0.1"
P0_CONFIG_SCHEMA_VERSION = "benchmark_v3_p0_config_0.1"
P0_PLAN_SCHEMA_VERSION = "benchmark_v3_p0_plan_0.1"
P0_LEDGER_SCHEMA_VERSION = "benchmark_v3_p0_expected_pilot_ledger_0.1"
FINALIZATION_RULE_VERSION = "benchmark_v3_finalization_0.1"
FROZEN_REGISTRY_RULE_VERSION = "benchmark_v3_frozen_registry_0.1"
DETERMINISTIC_SPLIT_RULE_VERSION = "benchmark_v3_deterministic_split_0.1"
P0_RULE_VERSION = "benchmark_v3_p0_static_preflight_0.1"
P0_EXECUTION_CONFIG_SCHEMA_VERSION = "benchmark_v3_p0_execution_config_0.1"
P0_EXECUTION_MANIFEST_SCHEMA_VERSION = "benchmark_v3_p0_execution_manifest_0.1"
P0_EXECUTION_METADATA_SCHEMA_VERSION = "benchmark_v3_p0_execution_metadata_0.1"
P0_EXECUTION_STATUS_SCHEMA_VERSION = "benchmark_v3_p0_execution_status_0.1"
P0_EXECUTION_RULE_VERSION = "benchmark_v3_p0_pilot_execution_0.1"

# V3-P1 is an independent pilot execution boundary.  Keep its protocol,
# config, artifact, and execution identifiers separate from P0 so a future
# runner cannot accidentally promote P0 semantics or outputs.
P1_PROTOCOL_VERSION = "V3-PLAN-20260727-P1"
P1_CONFIG_SCHEMA_VERSION = "benchmark_v3_p1_execution_config_0.1"
P1_PROTOCOL_DECISION_SCHEMA_VERSION = "benchmark_v3_p1_protocol_decision_0.1"
P1_PLAN_SCHEMA_VERSION = "benchmark_v3_p1_static_plan_0.1"
P1_LEDGER_SCHEMA_VERSION = "benchmark_v3_p1_expected_ledger_0.1"
P1_TRAFFIC_LEDGER_SCHEMA_VERSION = "benchmark_v3_p1_expected_traffic_ledger_0.1"
P1_STATIC_MANIFEST_SCHEMA_VERSION = "benchmark_v3_p1_static_manifest_0.1"
P1_STATIC_STATUS_SCHEMA_VERSION = "benchmark_v3_p1_static_status_0.1"
P1_SOURCE_CHECKSUMS_SCHEMA_VERSION = "benchmark_v3_p1_source_checksums_0.1"
P1_RECIPE_TABLE_SCHEMA_VERSION = "benchmark_v3_p1_recipe_table_0.1"
P1_EXECUTION_MANIFEST_SCHEMA_VERSION = "benchmark_v3_p1_execution_manifest_0.1"
P1_EXECUTION_STATUS_SCHEMA_VERSION = "benchmark_v3_p1_execution_status_0.1"
P1_WORKER_RESULT_SCHEMA_VERSION = "benchmark_v3_p1_worker_result_0.1"
P1_METADATA_SCHEMA_VERSION = "benchmark_v3_p1_sample_metadata_0.1"
P1_REVIEW_CARD_SCHEMA_VERSION = "benchmark_v3_p1_review_card_0.1"
P1_REVIEW_INDEX_SCHEMA_VERSION = "benchmark_v3_p1_review_index_0.1"
P1_ARTIFACT_RULE_VERSION = "benchmark_v3_p1_static_artifact_0.1"
P1_EXECUTION_RULE_VERSION = "benchmark_v3_p1_pilot_execution_0.1"
P1_TRAFFIC_RULE_VERSION = "benchmark_v3_p1_scripted_traffic_layout_0.1"
P1_REVIEW_RULE_VERSION = "benchmark_v3_p1_review_package_0.1"
P1_ARTIFACT_ROOT = V3_ARTIFACT_ROOT / "p1"
P1_STATIC_ARTIFACT_ROOT = V3_ARTIFACT_ROOT / "p1_static"

# Offline P0 closure is an evidence disposition boundary.  It deliberately
# does not mutate the frozen registry/split or promote pilot assets into the
# canonical dataset.
P0_CLOSURE_PROTOCOL_VERSION = "V3-PLAN-20260727-P0-CLOSURE"
P0_CLOSURE_CONFIG_SCHEMA_VERSION = "benchmark_v3_p0_closure_config_0.1"
P0_CLOSURE_MANIFEST_SCHEMA_VERSION = "benchmark_v3_p0_closure_manifest_0.1"
P0_CLOSURE_DECISION_SCHEMA_VERSION = "benchmark_v3_p0_closure_decision_0.1"
P0_CLOSURE_STATUS_SCHEMA_VERSION = "benchmark_v3_p0_closure_status_0.1"
P0_CLOSURE_RULE_VERSION = "benchmark_v3_p0_closure_0.1"
P0_CLOSURE_OLD_RAMP_SAMPLE_IDS = (
    "p0_d7ee0d1cb82f2c85675ab74a",
    "p0_b4372e771ff8e1119f9485fd",
)
P0_CLOSURE_CORRECTIVE_SAMPLE_IDS = (
    "p0_corrective_2a14c780f499b39f_a1",
    "p0_corrective_2a14c780f499b39f_a2",
)

SUBSETS = ["development_train", "development_val", "core_test", "challenge_test"]
PHYSICAL_SUBSETS = ("train", "val", "test_shared")
DEVELOPMENT_SUBSETS = {"development_train", "development_val"}
TEST_SUBSETS = {"core_test", "challenge_test"}
OBSERVATION_FACTORS = ["height", "pitch", "relative_yaw", "traffic_density", "weather", "illumination"]
OFFICIAL_LABELS = ["road_mask_visible", "lane_polyline_projected"]
STRUCTURAL_CELLS = [
    "Straight-2Lane",
    "Straight-4Lane",
    "Straight-6Lane",
    "Straight-8Lane",
    "Curve-2Lane",
    "Curve-4Lane",
    "Curve-6Lane",
    "Curve-8Lane",
    "Intersection-Approach-2Lane",
    "Intersection-Approach-4Lane",
    "Ramp-1-to-Mainline-8",
]

# V3-PLAN-20260727-R4 is a new protocol boundary.  The historical S0/P0 and
# V3-P1 constants above remain available because their artifacts are immutable
# evidence and must continue to validate against their original contracts.
R4_PROTOCOL_VERSION = "V3-PLAN-20260727-R4"
R4_CONFIG_SCHEMA_VERSION = "benchmark_v3_r4_p1_corrective_config_0.1"
R4_CHALLENGE_SCHEMA_VERSION = "benchmark_v3_challenge_l18_r4_0.1"
R4_PLAN_SCHEMA_VERSION = "benchmark_v3_r4_p1_corrective_plan_0.1"
R4_LEDGER_SCHEMA_VERSION = "benchmark_v3_r4_expected_ledger_0.1"
R4_TRAFFIC_LEDGER_SCHEMA_VERSION = "benchmark_v3_r4_expected_traffic_ledger_0.1"
R4_RECIPE_TABLE_SCHEMA_VERSION = "benchmark_v3_r4_recipe_table_0.1"
R4_STATIC_MANIFEST_SCHEMA_VERSION = "benchmark_v3_r4_static_manifest_0.1"
R4_STATIC_STATUS_SCHEMA_VERSION = "benchmark_v3_r4_static_status_0.1"
R4_SOURCE_CHECKSUMS_SCHEMA_VERSION = "benchmark_v3_r4_source_checksums_0.1"
R4_PROTOCOL_DECISION_SCHEMA_VERSION = "benchmark_v3_r4_protocol_decision_0.1"
R4_REVIEW_INDEX_SCHEMA_VERSION = "benchmark_v3_r4_review_index_0.1"
R4_REVIEW_CARD_SCHEMA_VERSION = "benchmark_v3_r4_review_card_0.1"
R4_DEVELOPMENT_GEOMETRY_SCHEMA_VERSION = "benchmark_v3_r4_development_geometry_contract_0.1"
R4_TRAFFIC_LAYOUT_SCHEMA_VERSION = "benchmark_v3_r4_traffic_layout_0.1"
R4_TRAFFIC_LAYOUT_TOPOLOGY_V2_SCHEMA_VERSION = "benchmark_v3_r4_traffic_layout_topology_v2_0.1"
R4_TRAFFIC_TOPOLOGY_LAYOUT_SCHEMA_VERSION = "benchmark_v3_r4_traffic_topology_layout_0.1"
R4_TRAFFIC_TOPOLOGY_RULE_VERSION = "benchmark_v3_r4_carla_waypoint_seeded_lane_jitter_master_subset_0.2"
R4_CARLA_WAYPOINT_TOPOLOGY_RULE_VERSION = "benchmark_v3_r4_carla_waypoint_lane_topology_0.1"
R4_TRAFFIC_SUBSET_RULE_VERSION = "benchmark_v3_r4_density20_spatial_balanced_subset_0.1"
R4_CONFLICT_SAFE_TOPOLOGY_RULE_VERSION = "benchmark_v3_r4_carla_conflict_safe_master_layout_0.2"
R4_CONFLICT_EXCLUSION_RULE_VERSION = "benchmark_v3_r4_conflict_exclusion_junction_radius_0.1"
R4_CONFLICT_SAFE_TOPOLOGY_SCHEMA_VERSION = "benchmark_v3_r4_conflict_safe_traffic_layout_0.2"
# R4 closure correction keeps the original conflict-safe artifacts immutable.
# These versions are intentionally additive so an old static/run artifact can
# still be inspected with its historical rule identifiers while new layouts
# cannot be mistaken for the superseded v0.2 search.
R4_CONFLICT_SAFE_TOPOLOGY_RULE_V3_VERSION = "benchmark_v3_r4_carla_conflict_safe_master_layout_0.3"
R4_CONFLICT_EXCLUSION_RULE_V2_VERSION = "benchmark_v3_r4_conflict_exclusion_junction_radius_0.2"
R4_CONFLICT_SAFE_TOPOLOGY_SCHEMA_V3_VERSION = "benchmark_v3_r4_conflict_safe_traffic_layout_0.3"
R4_CONFLICT_SAFE_MASTER_LAYOUT_SCHEMA_V3_VERSION = "benchmark_v3_r4_conflict_safe_master_layout_0.3"
# Additive closure-correction v0.4 rule.  Older v0.2/v0.3 layouts remain
# immutable historical evidence and are never rewritten in place.
R4_CONFLICT_SAFE_TOPOLOGY_RULE_V4_VERSION = "benchmark_v3_r4_carla_conflict_safe_master_layout_0.4"
R4_CONFLICT_EXCLUSION_RULE_V3_VERSION = "benchmark_v3_r4_conflict_exclusion_junction_radius_0.3"
R4_CONFLICT_SAFE_TOPOLOGY_SCHEMA_V4_VERSION = "benchmark_v3_r4_conflict_safe_traffic_layout_0.4"
R4_CONFLICT_SAFE_MASTER_LAYOUT_SCHEMA_V4_VERSION = "benchmark_v3_r4_conflict_safe_master_layout_0.4"
R4_TOWN10_IDENTITY_RULE_VERSION = "benchmark_v3_r4_town10_identity_replay_0.1"
R4_RC_CALIBRATION_SCHEMA_VERSION = "benchmark_v3_r4_rc_height_calibration_0.1"
R4_ARTIFACT_RULE_VERSION = "benchmark_v3_r4_p1_corrective_static_artifact_0.1"
R4_RECIPE_RULE_VERSION = "benchmark_v3_r4_recipe_planner_0.1"
R4_DEVELOPMENT_RECIPE_RULE_VERSION = "benchmark_v3_r4_development_stratified_random_0.2"
R4_CORE_RECIPE_RULE_VERSION = "benchmark_v3_r4_core_15_condition_0.1"
R4_CHALLENGE_RECIPE_RULE_VERSION = "benchmark_v3_r4_challenge_l18_0.1"
R4_TRAFFIC_RULE_VERSION = "benchmark_v3_r4_seeded_lane_jitter_master_subset_0.1"
R4_PROJECTION_RULE_VERSION = "benchmark_v3_r4_recipe_specific_projection_0.1"
R4_QA_RULE_VERSION = "benchmark_v3_qa_policy_20260727_ramp_area_v2"

R4_CORE_HEIGHTS_M = (20.0, 40.0, 60.0, 80.0, 100.0)
R4_CORE_DEFAULT_HEIGHT_M = 40.0
R4_CHALLENGE_HEIGHT_LEVELS_M = {"1": 40.0, "2": 20.0, "3": 100.0}
R4_CORE_BUNDLE_SIZE = 15
R4_CHALLENGE_BUNDLE_SIZE = 18
R4_CORRECTIVE_ARTIFACT_ROOT = V3_ARTIFACT_ROOT / "p1_corrective_static"
R4_RC_CALIBRATION_CONFIG_PATH = V3_CONFIG_ROOT / "rc_height_calibration.json"

# R4 corrective CARLA execution is a separate evidence root.  It consumes an
# immutable R4 static artifact and must never write back into that artifact.
R4_EXECUTION_ARTIFACT_ROOT = V3_ARTIFACT_ROOT / "p1_corrective"
R4_EXECUTION_MANIFEST_SCHEMA_VERSION = "benchmark_v3_r4_execution_manifest_0.1"
R4_EXECUTION_STATUS_SCHEMA_VERSION = "benchmark_v3_r4_execution_status_0.1"
WORKER_RESULT_SCHEMA_VERSION = "benchmark_v3_worker_result_1.0"
R4_SAMPLE_METADATA_SCHEMA_VERSION = "benchmark_v3_r4_sample_metadata_0.1"
R4_EXECUTION_RULE_VERSION = "benchmark_v3_r4_corrective_pilot_execution_0.1"
R4_TRAFFIC_TOPOLOGY_SNAP_WARN_M = 3.5
R4_TRAFFIC_TOPOLOGY_SNAP_HARD_MAX_M = 4.5
R4_CONFLICT_ZONE_RADIUS_M = 10.0
