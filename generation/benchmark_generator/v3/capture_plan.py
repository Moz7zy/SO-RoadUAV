"""Offline V3-PLAN-20260727-R4 migration and P1 corrective planner.

This module deliberately has no CARLA import.  It freezes the R4 recipe
contract, the small corrective ledger, the continuous Development geometry
contract, and a replayable traffic master layout.  The old :mod:`p1` module is
kept separate so immutable Core-13/P1 evidence remains verifiable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any

from benchmark_generator.common.identity import canonical_json
from benchmark_generator.config import (
    ConfigError,
    ensure_path_within,
    forbid_legacy_source_path,
    load_json,
)

from .constants import (
    OFFICIAL_LABELS,
    R4_ARTIFACT_RULE_VERSION,
    R4_CHALLENGE_BUNDLE_SIZE,
    R4_CHALLENGE_HEIGHT_LEVELS_M,
    R4_CHALLENGE_SCHEMA_VERSION,
    R4_CONFIG_SCHEMA_VERSION,
    R4_CORE_BUNDLE_SIZE,
    R4_CORE_DEFAULT_HEIGHT_M,
    R4_CORE_HEIGHTS_M,
    R4_CORE_RECIPE_RULE_VERSION,
    R4_DEVELOPMENT_GEOMETRY_SCHEMA_VERSION,
    R4_DEVELOPMENT_RECIPE_RULE_VERSION,
    R4_LEDGER_SCHEMA_VERSION,
    R4_PLAN_SCHEMA_VERSION,
    R4_PROJECTION_RULE_VERSION,
    R4_PROTOCOL_DECISION_SCHEMA_VERSION,
    R4_PROTOCOL_VERSION,
    R4_QA_RULE_VERSION,
    R4_RECIPE_RULE_VERSION,
    R4_RECIPE_TABLE_SCHEMA_VERSION,
    R4_REVIEW_CARD_SCHEMA_VERSION,
    R4_REVIEW_INDEX_SCHEMA_VERSION,
    R4_RC_CALIBRATION_CONFIG_PATH,
    R4_RC_CALIBRATION_SCHEMA_VERSION,
    R4_SOURCE_CHECKSUMS_SCHEMA_VERSION,
    R4_STATIC_MANIFEST_SCHEMA_VERSION,
    R4_STATIC_STATUS_SCHEMA_VERSION,
    R4_TRAFFIC_LEDGER_SCHEMA_VERSION,
    R4_TRAFFIC_RULE_VERSION,
    R4_TRAFFIC_LAYOUT_SCHEMA_VERSION,
    R4_TRAFFIC_LAYOUT_TOPOLOGY_V2_SCHEMA_VERSION,
    R4_TRAFFIC_TOPOLOGY_LAYOUT_SCHEMA_VERSION,
    R4_TRAFFIC_TOPOLOGY_RULE_VERSION,
    R4_CARLA_WAYPOINT_TOPOLOGY_RULE_VERSION,
    R4_CONFLICT_EXCLUSION_RULE_VERSION,
    R4_CONFLICT_EXCLUSION_RULE_V2_VERSION,
    R4_CONFLICT_EXCLUSION_RULE_V3_VERSION,
    R4_CONFLICT_SAFE_TOPOLOGY_RULE_VERSION,
    R4_CONFLICT_SAFE_TOPOLOGY_RULE_V3_VERSION,
    R4_CONFLICT_SAFE_TOPOLOGY_RULE_V4_VERSION,
    R4_CONFLICT_SAFE_TOPOLOGY_SCHEMA_V3_VERSION,
    R4_CONFLICT_SAFE_MASTER_LAYOUT_SCHEMA_V3_VERSION,
    R4_CONFLICT_SAFE_TOPOLOGY_SCHEMA_V4_VERSION,
    R4_CONFLICT_SAFE_MASTER_LAYOUT_SCHEMA_V4_VERSION,
    R4_TRAFFIC_SUBSET_RULE_VERSION,
    R4_CORRECTIVE_ARTIFACT_ROOT,
    PACKAGE_ROOT,
    V3_ARTIFACT_ROOT,
    V3_CONFIG_ROOT,
    V3_SCHEMA_ROOT,
)
from .ids import derive_seed, make_recipe_id, make_sample_id, v3_digest
from benchmark_generator.common.io import file_sha256
from .recipes import (
    build_challenge_recipes,
    build_core_recipes,
    build_development_recipes,
    validate_l18_balance,
)
from .schema_support import validate_schema_instance
from .replay import REPLAY_CANONICAL_RULE_VERSION, REPLAY_CANONICAL_SCHEMA_VERSION, REPLAY_ROTATION_PRECISION, canonical_replay


R4_CONFIG_PATH = V3_CONFIG_ROOT / "p1_corrective_r4.json"
R4_MAPS = ("Town04_Opt", "Town05_Opt", "Town10HD_Opt")
R4_STATIC_SCHEMA_NAMES = (
    "r4_p1_corrective_config.schema.json",
    "r4_challenge_l18.schema.json",
    "r4_qa_policy.schema.json",
    "rc_height_calibration.schema.json",
    "r4_traffic_layout.schema.json",
    "r4_expected_ledger.schema.json",
    "r4_expected_traffic_ledger.schema.json",
    "r4_recipe_table.schema.json",
    "r4_static_plan.schema.json",
    "r4_static_manifest.schema.json",
    "r4_static_status.schema.json",
    "r4_source_checksums.schema.json",
    "r4_protocol_decision.schema.json",
    "r4_review_card.schema.json",
    "r4_review_index.schema.json",
    "r4_development_geometry_contract.schema.json",
)
R4_ASSETS = (
    "rgb.jpg",
    "road_mask_visible.png",
    "road_mask_visible_overlay.jpg",
    "lane_polyline_projected_overlay.jpg",
    "metadata.json",
)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _traffic_rule_version(config: dict[str, Any]) -> str:
    """Return the config-frozen traffic rule without changing legacy P1/R4."""

    return str(config.get("traffic", {}).get("layout_rule_version", R4_TRAFFIC_RULE_VERSION))


def _uses_waypoint_topology(config: dict[str, Any]) -> bool:
    return bool(config.get("traffic", {}).get("topology_layout_path"))


def _uses_spatial_subset(config: dict[str, Any]) -> bool:
    return config.get("traffic", {}).get("density20_subset_rule_version") == R4_TRAFFIC_SUBSET_RULE_VERSION


def _uses_conflict_safe_layout(config: dict[str, Any]) -> bool:
    return config.get("traffic", {}).get("conflict_exclusion_rule_version") in {
        R4_CONFLICT_EXCLUSION_RULE_VERSION,
        R4_CONFLICT_EXCLUSION_RULE_V2_VERSION,
        R4_CONFLICT_EXCLUSION_RULE_V3_VERSION,
    }


def _conflict_topology_rule_version(config: dict[str, Any]) -> str:
    value = config.get("traffic", {}).get("layout_rule_version")
    if value in {R4_CONFLICT_SAFE_TOPOLOGY_RULE_VERSION, R4_CONFLICT_SAFE_TOPOLOGY_RULE_V3_VERSION, R4_CONFLICT_SAFE_TOPOLOGY_RULE_V4_VERSION}:
        return str(value)
    return R4_CONFLICT_SAFE_TOPOLOGY_RULE_VERSION


def _conflict_exclusion_rule_version(config: dict[str, Any]) -> str:
    value = config.get("traffic", {}).get("conflict_exclusion_rule_version")
    if value in {R4_CONFLICT_EXCLUSION_RULE_VERSION, R4_CONFLICT_EXCLUSION_RULE_V2_VERSION, R4_CONFLICT_EXCLUSION_RULE_V3_VERSION}:
        return str(value)
    return R4_CONFLICT_EXCLUSION_RULE_VERSION


def _round_half_up(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def _schema(name: str) -> Path:
    path = ensure_path_within(V3_SCHEMA_ROOT / name, [V3_SCHEMA_ROOT], f"V3 R4 schema {name}")
    if not path.is_file():
        raise ConfigError(f"V3 R4 schema is missing: {path}")
    return path


def _verify_files(root: Path, expected: dict[str, str], label: str) -> dict[str, str]:
    actual: dict[str, str] = {}
    for name, digest in expected.items():
        path = ensure_path_within(root / name, [root], f"{label} file")
        if not path.is_file():
            raise ConfigError(f"{label} is missing {name}")
        actual[name] = file_sha256(path)
        if actual[name] != digest:
            raise ConfigError(f"{label} checksum drifted for {name}")
    return actual


def _load_source_root(source: dict[str, Any], parent: Path, label: str) -> Path:
    root = ensure_path_within(Path(source["artifact_root"]), [parent], label)
    if root.name != source["run_id"] or not root.is_dir():
        raise ConfigError(f"{label} run is missing or run_id mismatches: {root}")
    forbid_legacy_source_path(root, label)
    _verify_files(root, source["required_file_sha256"], label)
    return root


def _validate_r4_selection(config: dict[str, Any], final_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    final_sites = {row["site_id"]: row for row in load_json(final_root / "site_registry.json")["sites"]}
    final_anchors = {row["anchor_id"]: row for row in load_json(final_root / "anchor_registry.json")["anchors"]}
    split = load_json(final_root / "split_manifest.json")
    assignments = split.get("assignments", {})
    selected = config["selection"]["sites"]
    if len(selected) != 4 or len({row["site_id"] for row in selected}) != 4 or len({row["anchor_id"] for row in selected}) != 4:
        raise ConfigError("R4 corrective selection must contain four unique sites and anchors")
    if len({row["leakage_group_id"] for row in selected}) != 4:
        raise ConfigError("R4 corrective selection must use four distinct leakage groups")
    if {row["map_id"] for row in selected} != set(R4_MAPS):
        raise ConfigError("R4 corrective selection must cover all three maps")
    selected_sites: dict[str, Any] = {}
    selected_anchors: dict[str, Any] = {}
    for choice in selected:
        site = final_sites.get(choice["site_id"])
        anchor = final_anchors.get(choice["anchor_id"])
        if site is None or anchor is None:
            raise ConfigError(f"R4 corrective selection is not in frozen finalization: {choice}")
        for field in ("map_id", "structural_cell", "leakage_group_id"):
            if site.get(field) != choice[field]:
                raise ConfigError(f"R4 site {field} mismatch for {choice['site_id']}")
        if site.get("final_site_eligibility") != "strict_test_candidate":
            raise ConfigError(f"R4 corrective site is not strict_test_candidate: {choice['site_id']}")
        if site.get("subset_assignment") != choice["source_subset"] or anchor.get("subset_assignment") != choice["source_subset"]:
            raise ConfigError(f"R4 corrective source subset drifted for {choice['site_id']}")
        if anchor.get("site_id") != choice["site_id"] or anchor.get("map_id") != choice["map_id"]:
            raise ConfigError(f"R4 corrective anchor/site identity mismatch: {choice['anchor_id']}")
        if choice["leakage_group_id"] not in assignments.get(choice["source_subset"], []):
            raise ConfigError(f"R4 corrective leakage group is not in its frozen subset: {choice['leakage_group_id']}")
        selected_sites[site["site_id"]] = deepcopy(site)
        selected_anchors[anchor["anchor_id"]] = deepcopy(anchor)

    cells = {row["structural_cell"] for row in selected}
    if "Ramp-1-to-Mainline-8" not in cells or not any(item.startswith("Intersection-Approach") for item in cells):
        raise ConfigError("R4 corrective selection lacks Ramp or Intersection coverage")
    if not ({"Straight-6Lane", "Straight-8Lane"} & cells):
        raise ConfigError("R4 corrective selection lacks an ordinary multi-lane structure")
    lane_map = config["corrective_plan"]["density_anchor_by_lane_count"]
    for expected_lane_count, anchor_id in lane_map.items():
        anchor = selected_anchors.get(anchor_id)
        if anchor is None or int(anchor["lane_count_at_anchor"]) != int(expected_lane_count):
            raise ConfigError(f"R4 density anchor does not match lane count {expected_lane_count}: {anchor_id}")
    if set(config["corrective_plan"]["height_anchor_ids"]) - set(selected_anchors):
        raise ConfigError("R4 height sweep contains an unselected anchor")
    if set(config["corrective_plan"]["pressure_anchor_ids"]) - set(selected_anchors):
        raise ConfigError("R4 pressure sweep contains an unselected anchor")
    return selected_sites, selected_anchors


def build_r4_probe_config_override(config_path: Path, probe_anchor_id: str) -> dict[str, Any]:
    """Return an in-memory same-cell anchor replacement for topology probing.

    The override is deliberately limited to an existing strict candidate in the
    frozen finalization.  It is never serialized by this helper and cannot
    mutate the registry or split manifest.  A later corrective static config
    must record the accepted replacement explicitly.
    """

    path = ensure_path_within(Path(config_path), [V3_CONFIG_ROOT], "V3 R4 probe config")
    config = load_json(path)
    final_root = _load_source_root(config["source_finalization"], V3_ARTIFACT_ROOT / "finalization", "V3 R4 probe finalization source")
    final_sites = {row["site_id"]: row for row in load_json(final_root / "site_registry.json")["sites"]}
    final_anchors = {row["anchor_id"]: row for row in load_json(final_root / "anchor_registry.json")["anchors"]}
    split = load_json(final_root / "split_manifest.json")

    replacement = final_anchors.get(str(probe_anchor_id))
    if replacement is None:
        raise ConfigError(f"R4 probe anchor is not present in frozen finalization: {probe_anchor_id}")
    site_id = replacement.get("site_id")
    site = final_sites.get(site_id)
    if site is None:
        raise ConfigError(f"R4 probe anchor has no frozen site: {probe_anchor_id}")
    if site.get("final_site_eligibility") != "strict_test_candidate" or replacement.get("final_site_eligibility") != "strict_test_candidate":
        raise ConfigError(f"R4 probe anchor/site is not strict_test_candidate: {probe_anchor_id}")

    selected = list(config["selection"]["sites"])
    ramp_indices = [
        index for index, choice in enumerate(selected)
        if choice.get("structural_cell") == "Ramp-1-to-Mainline-8"
    ]
    if len(ramp_indices) != 1:
        raise ConfigError("R4 probe requires exactly one selected Ramp-1-to-Mainline-8 anchor")
    ramp_index = ramp_indices[0]
    original = selected[ramp_index]
    for field in ("map_id", "structural_cell", "source_subset"):
        site_value = site.get(field)
        if field == "source_subset" and site_value is None:
            site_value = site.get("subset_assignment")
        replacement_value = replacement.get(field)
        if field == "structural_cell" and replacement_value is None:
            replacement_value = site.get(field)
        if field == "source_subset" and replacement_value is None:
            replacement_value = replacement.get("subset_assignment")
        if site_value != original.get(field) or replacement_value != original.get(field):
            raise ConfigError(f"R4 probe replacement drifted for {field}: {probe_anchor_id}")
    assignments = split.get("assignments", {})
    if replacement.get("anchor_id") not in site.get("anchor_ids", []):
        raise ConfigError(f"R4 probe replacement anchor is not registered by its site: {probe_anchor_id}")
    replacement_leakage_group = replacement.get("leakage_group_id") or site.get("leakage_group_id")
    if replacement_leakage_group not in assignments.get(original["source_subset"], []):
        raise ConfigError(f"R4 probe replacement leakage group is outside frozen subset: {probe_anchor_id}")
    if replacement_leakage_group in {
        choice.get("leakage_group_id") for index, choice in enumerate(selected) if index != ramp_index
    }:
        raise ConfigError(f"R4 probe replacement would duplicate a selected leakage group: {probe_anchor_id}")

    replacement_choice = deepcopy(original)
    replacement_choice.update({
        "site_id": site["site_id"],
        "anchor_id": replacement["anchor_id"],
        "map_id": replacement["map_id"],
        "structural_cell": site["structural_cell"],
        "leakage_group_id": replacement_leakage_group,
        "source_subset": replacement["subset_assignment"],
    })
    config["selection"]["sites"] = [
        replacement_choice if index == ramp_index else deepcopy(choice)
        for index, choice in enumerate(selected)
    ]

    corrective = config.get("corrective_plan", {})
    for key in ("height_anchor_ids", "pressure_anchor_ids", "replacement_anchor_ids"):
        corrective[key] = [
            replacement["anchor_id"] if value == original["anchor_id"] else value
            for value in corrective.get(key, [])
        ]
    scope = corrective.get("resample_scope", {})
    scope["anchor_ids"] = [
        replacement["anchor_id"] if value == original["anchor_id"] else value
        for value in scope.get("anchor_ids", [])
    ]
    config["corrective_plan"] = corrective
    return config


def _config_file(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (PACKAGE_ROOT / path)


def load_capture_config(config_path: Path) -> dict[str, Any]:
    """Load the engineering-base generation config with no historical artifact dependency.

    The public generator only needs the frozen behavioural parameters of the
    engineering base (camera, development sampling, traffic layout, QA
    thresholds).  Historical R4 provenance roots, selection pins and output-root
    assertions are deliberately not consulted.
    """

    path = ensure_path_within(Path(config_path), [V3_CONFIG_ROOT], "Benchmark-V3 engineering base")
    config = load_json(path)
    if config["labels"]["official_labels"] != OFFICIAL_LABELS:
        raise ConfigError("official labels must be exactly the two-label contract")
    if config["lifecycle"]["adopt_existing"] is not False or config["lifecycle"]["worker_must_not_load_world"] is not True:
        raise ConfigError("lifecycle must own a new server and forbid worker map loading")
    if config["traffic"]["pedestrians"] or config["traffic"]["sumo"]:
        raise ConfigError("traffic must not use pedestrians or SUMO")
    if config["qa"]["policy_version"] != R4_QA_RULE_VERSION:
        raise ConfigError("QA policy version drifted")
    if tuple(float(value) for value in config["core"]["levels"]["height_m"]) != R4_CORE_HEIGHTS_M:
        raise ConfigError("Core height levels drifted")
    if float(config["core"]["defaults"]["height_m"]) != R4_CORE_DEFAULT_HEIGHT_M or int(config["core"]["bundle_size"]) != R4_CORE_BUNDLE_SIZE:
        raise ConfigError("Core default or bundle size drifted")
    if config["development"]["ranges"]["height_m"] != [20.0, 100.0]:
        raise ConfigError("Development height range drifted")
    if int(config["development"]["seeds"]["development_train"]) == int(config["development"]["seeds"]["development_val"]):
        raise ConfigError("Development Train and Val seeds must differ")
    if config["recipe_freeze"]["height_mapping"] != {"level1_m": 40.0, "level2_m": 20.0, "level3_m": 100.0}:
        raise ConfigError("Challenge height mapping drifted")

    challenge_path = _config_file(config["source_protocol"]["challenge_l18_path"])
    qa_path = _config_file(config["source_protocol"]["qa_policy_path"])
    calibration_path = _config_file(config["source_protocol"]["rc_height_calibration_path"])
    challenge = load_json(challenge_path)
    validate_l18_balance(challenge)
    qa_policy = load_json(qa_path)
    validate_schema_instance(qa_policy, load_json(_schema("r4_qa_policy.schema.json")), "Benchmark-V3 QA policy")
    calibration = load_json(calibration_path)
    validate_schema_instance(calibration, load_json(_schema("rc_height_calibration.schema.json")), "Benchmark-V3 height calibration")

    loaded = deepcopy(config)
    loaded.update(
        {
            "_config_path": str(path),
            "_config_sha256": file_sha256(path),
            "_challenge_l18_path": str(challenge_path),
            "_challenge_l18_sha256": file_sha256(challenge_path),
            "_qa_policy_path": str(qa_path),
            "_qa_policy_sha256": file_sha256(qa_path),
            "_rc_calibration_path": str(calibration_path),
            "_rc_calibration_sha256": file_sha256(calibration_path),
            "_challenge_l18": challenge,
            "_qa_policy": qa_policy,
            "_rc_calibration": calibration,
        }
    )
    return loaded


def load_r4_config(
    config_path: Path = R4_CONFIG_PATH,
    *,
    config_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load and fail-closed validate all R4 offline inputs."""

    path = ensure_path_within(Path(config_path), [V3_CONFIG_ROOT], "V3 R4 config")
    config = deepcopy(config_override) if config_override is not None else load_json(path)
    config_schema_name = (
        "r4_p1_corrective_config_correction.schema.json"
        if config.get("schema_version") == "benchmark_v3_r4_p1_corrective_config_0.2"
        else "r4_p1_corrective_config.schema.json"
    )
    validate_schema_instance(config, load_json(_schema(config_schema_name)), "V3 R4 config")
    if config["protocol_version"] != R4_PROTOCOL_VERSION or config["stage"] != "p1_corrective_static":
        raise ConfigError("V3 R4 protocol or stage drifted")
    if config["labels"]["official_labels"] != OFFICIAL_LABELS:
        raise ConfigError("V3 R4 official labels must be exactly the two-label contract")
    if not str(config["runtime"]["carla_python"]).strip():
        raise ConfigError("V3 R4 CARLA commands must use the carla conda interpreter")
    safety = config["safety_boundaries"]
    required_true = (
        "default_dry_run", "execute_requires_r4_corrective_confirmation", "execute_requires_server_lifecycle_confirmation",
        "worker_requires_r4_corrective_confirmation", "worker_requires_server_lifecycle_confirmation",
        "worker_must_not_load_world", "no_registry_write", "no_split_write", "pilot_evidence_only",
        "no_p0_mutation", "no_old_p1_mutation", "no_rc", "no_full", "no_training", "no_delete", "no_commit",
        "no_image_generation_in_static_phase",
    )
    if any(safety.get(name) is not True for name in required_true) or safety.get("canonical_dataset") is not False:
        raise ConfigError("V3 R4 safety boundaries drifted")
    if config["lifecycle"]["adopt_existing"] is not False or config["lifecycle"]["worker_must_not_load_world"] is not True:
        raise ConfigError("V3 R4 lifecycle must own a new server and forbid worker map loading")
    if config["traffic"]["pedestrians"] or config["traffic"]["sumo"]:
        raise ConfigError("V3 R4 corrective traffic must not use pedestrians or SUMO")
    if config["qa"]["policy_version"] != R4_QA_RULE_VERSION:
        raise ConfigError("V3 R4 QA policy version drifted")
    if float(config["qa"]["intersection_reference_area_min"]) != 0.05 or float(config["qa"]["ramp_reference_area_min"]) != 0.04:
        raise ConfigError("V3 R4 reference geometry thresholds drifted")
    if tuple(float(value) for value in config["core"]["levels"]["height_m"]) != R4_CORE_HEIGHTS_M:
        raise ConfigError("V3 R4 Core height levels drifted")
    if float(config["core"]["defaults"]["height_m"]) != R4_CORE_DEFAULT_HEIGHT_M or int(config["core"]["bundle_size"]) != R4_CORE_BUNDLE_SIZE:
        raise ConfigError("V3 R4 Core default or bundle size drifted")
    if config["development"]["ranges"]["height_m"] != [20.0, 100.0]:
        raise ConfigError("V3 R4 Development height range drifted")
    if int(config["development"]["seeds"]["development_train"]) == int(config["development"]["seeds"]["development_val"]):
        raise ConfigError("V3 R4 Development Train and Val seeds must differ")
    if config["recipe_freeze"]["height_mapping"] != {"level1_m": 40.0, "level2_m": 20.0, "level3_m": 100.0}:
        raise ConfigError("V3 R4 Challenge height mapping drifted")

    final_root = _load_source_root(config["source_finalization"], V3_ARTIFACT_ROOT / "finalization", "V3 R4 finalization source")
    closure_root = _load_source_root(config["source_p0_closure"], V3_ARTIFACT_ROOT / "p0_closure", "V3 R4 P0 closure source")
    challenge_path = ensure_path_within(Path(config["source_protocol"]["challenge_l18_path"]), [V3_CONFIG_ROOT], "V3 R4 Challenge config")
    qa_path = ensure_path_within(Path(config["source_protocol"]["qa_policy_path"]), [V3_CONFIG_ROOT], "V3 R4 QA config")
    calibration_path = ensure_path_within(Path(config["source_protocol"]["rc_height_calibration_path"]), [V3_CONFIG_ROOT], "V3 R4 calibration config")
    if file_sha256(challenge_path) != file_sha256(R4_CONFIG_PATH.parent / "challenge_l18_r4.json"):
        raise ConfigError("V3 R4 Challenge config path is not the frozen R4 input")
    challenge = load_json(challenge_path)
    validate_schema_instance(challenge, load_json(_schema("r4_challenge_l18.schema.json")), "V3 R4 Challenge L18")
    validate_l18_balance(challenge)
    if challenge["factor_level_values"]["height"] != R4_CHALLENGE_HEIGHT_LEVELS_M:
        raise ConfigError("V3 R4 Challenge height levels drifted")
    qa_policy = load_json(qa_path)
    validate_schema_instance(qa_policy, load_json(_schema("r4_qa_policy.schema.json")), "V3 R4 QA policy")
    calibration = load_json(calibration_path)
    validate_schema_instance(calibration, load_json(_schema("rc_height_calibration.schema.json")), "V3 R4 RC height calibration")
    selected_sites, selected_anchors = _validate_r4_selection(config, final_root)
    topology_path: Path | None = None
    topology: dict[str, Any] | None = None
    if _uses_waypoint_topology(config):
        topology_path = ensure_path_within(
            Path(config["traffic"]["topology_layout_path"]),
            [V3_CONFIG_ROOT],
            "V3 R4 CARLA topology layout",
        )
        if not topology_path.is_file():
            raise ConfigError(f"V3 R4 CARLA topology layout is missing: {topology_path}")
        topology = load_json(topology_path)
        topology_schema_name = (
            "r4_traffic_conflict_safe_layout_v4.schema.json"
            if _uses_conflict_safe_layout(config) and _conflict_topology_rule_version(config) == R4_CONFLICT_SAFE_TOPOLOGY_RULE_V4_VERSION
            else "r4_traffic_conflict_safe_layout_v3.schema.json"
            if _uses_conflict_safe_layout(config) and _conflict_topology_rule_version(config) == R4_CONFLICT_SAFE_TOPOLOGY_RULE_V3_VERSION
            else "r4_traffic_conflict_safe_layout.schema.json"
            if _uses_conflict_safe_layout(config)
            else "r4_traffic_topology_layout.schema.json"
        )
        validate_schema_instance(
            topology,
            load_json(_schema(topology_schema_name)),
            "V3 R4 CARLA topology layout",
        )
        allowed_topology_rules = {
            R4_CARLA_WAYPOINT_TOPOLOGY_RULE_VERSION,
            R4_CONFLICT_SAFE_TOPOLOGY_RULE_VERSION,
            R4_CONFLICT_SAFE_TOPOLOGY_RULE_V3_VERSION,
            R4_CONFLICT_SAFE_TOPOLOGY_RULE_V4_VERSION,
        }
        if topology["rule_version"] not in allowed_topology_rules:
            raise ConfigError("V3 R4 CARLA topology rule version drifted")
        if set(topology.get("map_ids", [])) != set(R4_MAPS):
            raise ConfigError("V3 R4 CARLA topology layout must cover all three maps")
        topology_anchors = {item["anchor_id"]: item for item in topology["anchors"]}
        for anchor_id in selected_anchors:
            item = topology_anchors.get(anchor_id)
            if item is None:
                raise ConfigError(f"V3 R4 CARLA topology layout is missing anchor {anchor_id}")
            if int(item["lane_count"]) != int(selected_anchors[anchor_id]["lane_count_at_anchor"]):
                raise ConfigError(f"V3 R4 CARLA topology lane count drifted for {anchor_id}")
        expected_layout_rule = (
            _conflict_topology_rule_version(config)
            if _uses_conflict_safe_layout(config)
            else R4_TRAFFIC_TOPOLOGY_RULE_VERSION
        )
        if config["traffic"].get("layout_rule_version") != expected_layout_rule:
            raise ConfigError("V3 R4 topology config layout rule drifted")
    if ensure_path_within(Path(config["output"]["static_root"]), [R4_CORRECTIVE_ARTIFACT_ROOT], "V3 R4 static output") != R4_CORRECTIVE_ARTIFACT_ROOT:
        raise ConfigError("V3 R4 static output must use the independent corrective root")
    loaded = deepcopy(config)
    loaded.update({
        "_config_path": str(path),
        "_config_sha256": file_sha256(path),
        "_finalization_root": str(final_root),
        "_p0_closure_root": str(closure_root),
        "_challenge_l18_path": str(challenge_path),
        "_challenge_l18_sha256": file_sha256(challenge_path),
        "_qa_policy_path": str(qa_path),
        "_qa_policy_sha256": file_sha256(qa_path),
        "_rc_calibration_path": str(calibration_path),
        "_rc_calibration_sha256": file_sha256(calibration_path),
        "_challenge_l18": challenge,
        "_qa_policy": qa_policy,
        "_rc_calibration": calibration,
        "_traffic_topology_path": str(topology_path) if topology_path is not None else None,
        "_traffic_topology_sha256": file_sha256(topology_path) if topology_path is not None else None,
        "_traffic_topology": topology,
        "_traffic_topology_schema_name": topology_schema_name if topology is not None else None,
        "_source_sites": selected_sites,
        "_source_anchors": selected_anchors,
        "_finalization_hashes": dict(config["source_finalization"]["required_file_sha256"]),
        "_p0_closure_hashes": dict(config["source_p0_closure"]["required_file_sha256"]),
    })
    return loaded


def _factor_values(condition: dict[str, Any]) -> dict[str, Any]:
    return {
        "height": condition["height_m"],
        "pitch": condition["pitch_deg"],
        "relative_yaw": condition["yaw_relative_deg"],
        "traffic_density": condition["traffic_density_vehicles_per_lane_km"],
        "weather": condition["weather"]["family"],
        "illumination": condition["sun_altitude_deg"],
    }


def _condition_signature(condition: dict[str, Any]) -> str:
    return canonical_json(condition)


def _execution_condition(condition: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(condition)
    result.update({
        "fov_degrees": float(config["camera"]["fov_degrees"]),
        "image_width": int(config["camera"]["image_width"]),
        "image_height": int(config["camera"]["image_height"]),
        "fixed_delta_seconds": float(config["camera"]["fixed_delta_seconds"]),
        "warmup_ticks": int(config["camera"]["warmup_ticks"]),
        "exposure": {"exposure_mode": str(config["camera"]["exposure_mode"])},
        "exposure_mode": str(config["camera"]["exposure_mode"]),
        "sun_azimuth_relative_to_road_deg": float(config["camera"]["sun_azimuth_relative_to_road_deg"]),
    })
    return result


def _validate_r4_recipes(core: list[dict[str, Any]], challenge: list[dict[str, Any]], development: list[dict[str, Any]], config: dict[str, Any]) -> None:
    if len(core) != 15 or len(challenge) != 18 or len(development) != 20:
        raise ConfigError("R4 recipe fixture counts must be Core=15, Challenge=18, Development=20")
    if sum(bool(item["is_reference"]) for item in core) != 1 or sum(bool(item["is_reference"]) for item in challenge) != 1:
        raise ConfigError("R4 Core and Challenge must each have exactly one reference")
    core_reference = next(item for item in core if item["is_reference"])
    if _factor_values(core_reference["requested_conditions"]) != {
        "height": 40.0, "pitch": -80.0, "relative_yaw": 0.0, "traffic_density": 0.0, "weather": "dry", "illumination": 75.0,
    }:
        raise ConfigError("R4 Core reference is not the frozen 40m default")
    changed_counts = Counter(item["changed_factor"] for item in core if not item["is_reference"])
    if changed_counts != Counter({"height": 4, "pitch": 2, "relative_yaw": 2, "traffic_density": 2, "weather": 2, "illumination": 2}):
        raise ConfigError(f"R4 Core factor counts drifted: {changed_counts}")
    if len({item["observation_recipe_id"] for item in core}) != 15:
        raise ConfigError("R4 Core recipe IDs are not unique")
    validate_l18_balance(config["_challenge_l18"])
    if len({tuple(item["challenge_level_tuple"]) for item in challenge}) != 18:
        raise ConfigError("R4 Challenge tuples are not unique")
    if tuple(challenge[0]["challenge_level_tuple"]) != (1, 1, 1, 1, 1, 1):
        raise ConfigError("R4 Challenge first row is not the default reference")
    core_signatures = {_condition_signature(item["requested_conditions"]) for item in core}
    challenge_signatures = {_condition_signature(item["requested_conditions"]) for item in challenge}
    development_signatures = {_condition_signature(item["requested_conditions"]) for item in development}
    if (challenge_signatures - {_condition_signature(challenge[0]["requested_conditions"])}) & core_signatures:
        raise ConfigError("R4 non-reference Challenge recipe collides with Core")
    if (challenge_signatures | core_signatures) & development_signatures:
        raise ConfigError("R4 Development recipe collides with Core/Challenge")
    if {float(item["requested_conditions"]["height_m"]) for item in development} == {40.0}:
        raise ConfigError("R4 Development height sampling collapsed to the default")
    for item in development:
        height = float(item["requested_conditions"]["height_m"])
        if not 20.0 <= height <= 100.0:
            raise ConfigError("R4 Development height left [20,100]m")


def build_r4_recipe_table(config: dict[str, Any]) -> dict[str, Any]:
    core = build_core_recipes(config)
    challenge = build_challenge_recipes(config["_challenge_l18"])
    development = build_development_recipes(config, "development_train") + build_development_recipes(config, "development_val")
    _validate_r4_recipes(core, challenge, development, config)
    payload = {"core": core, "challenge": challenge, "development": development, "seed": int(config["recipe_freeze"]["seed"]), "rule_version": R4_RECIPE_RULE_VERSION}
    return {
        "schema_version": R4_RECIPE_TABLE_SCHEMA_VERSION,
        "protocol_version": R4_PROTOCOL_VERSION,
        "stage": "p1_corrective_static",
        "status": "frozen_before_corrective_execution",
        "core": core,
        "challenge": challenge,
        "development": development,
        "core_bundle_size": 15,
        "challenge_bundle_size": 18,
        "height_mapping": deepcopy(config["recipe_freeze"]["height_mapping"]),
        "recipe_table_sha256": _sha256_json(payload),
    }


def traffic_actor_count(density: float, lane_count: int, valid_window_m_each_side: float = 100.0) -> tuple[float, int]:
    if float(density) < 0.0 or int(lane_count) < 1 or float(valid_window_m_each_side) <= 0.0:
        raise ConfigError("R4 traffic density, lane count, and valid window are invalid")
    lane_km = round((2.0 * float(valid_window_m_each_side) * int(lane_count)) / 1000.0, 6)
    return lane_km, _round_half_up(float(density) * lane_km)


def _normalize_yaw(value: float) -> float:
    return ((float(value) + 180.0) % 360.0) - 180.0


def _traversal_yaw(site: dict[str, Any], anchor: dict[str, Any]) -> float:
    pose = anchor["pose_world"]
    yaw = _normalize_yaw(-float(pose["heading_deg"]))
    source_ref = anchor["locator"].get("source_ref")
    span = next((item for item in site.get("source_spans", []) if item.get("source_ref") == source_ref), None)
    if span is not None and int(span.get("orientation", 1)) < 0:
        yaw = _normalize_yaw(yaw + 180.0)
    if str(anchor["locator"].get("target_direction")) != "along_corridor_reference":
        yaw = _normalize_yaw(yaw + 180.0)
    return yaw


def _actor_transform(site: dict[str, Any], anchor: dict[str, Any], longitudinal_offset: float, lateral_offset: float, lane_index: int, config: dict[str, Any]) -> dict[str, Any]:
    pose = anchor["pose_world"]
    yaw = _traversal_yaw(site, anchor)
    radians = math.radians(yaw)
    forward = (math.cos(radians), math.sin(radians))
    left = (-math.sin(radians), math.cos(radians))
    base_x = float(pose["x"])
    base_y = -float(pose["y"])
    return {
        "location": {
            "x": round(base_x + forward[0] * longitudinal_offset + left[0] * lateral_offset, 6),
            "y": round(base_y + forward[1] * longitudinal_offset + left[1] * lateral_offset, 6),
            "z": round(float(pose["z"]) + float(config["traffic"]["vehicle_ground_clearance_m"]), 6),
        },
        "rotation": {"pitch": 0.0, "yaw": round(yaw, 6), "roll": 0.0},
        "frame": "carla_unreal_mirrored_lateral_axis",
        "logical_lane_index": int(lane_index),
    }


def _layout_qa(
    actors: list[dict[str, Any]],
    lane_count: int,
    config: dict[str, Any],
    *,
    use_actual_geometry: bool = False,
) -> dict[str, Any]:
    traffic = config["traffic"]
    qa_config = traffic["qa"]
    by_lane: dict[int, list[float]] = defaultdict(list)
    for actor in actors:
        by_lane[int(actor["logical_lane_index"])].append(float(actor["longitudinal_offset_m"]))
    gaps = [right - left for positions in by_lane.values() for left, right in zip(sorted(positions), sorted(positions)[1:])]
    same_lane_min_gap = round(min(gaps), 6) if gaps else float("inf")
    low, high = [float(value) for value in traffic["longitudinal_range_m"]]
    lane_coverage = [((max(values) - min(values)) / (2.0 * float(traffic["valid_window_m_each_side"]))) for values in by_lane.values() if values]
    coverage_ratio = round(min(lane_coverage), 6) if lane_coverage else 0.0
    lane_counts = {str(index): len(by_lane.get(index, [])) for index in range(lane_count)}
    balance = max(lane_counts.values(), default=0) - min(lane_counts.values(), default=0)
    near_threshold = float(qa_config["cross_lane_near_row_threshold_m"])
    cross_lane_pair_count = 0
    near_pair_count = 0
    cross_lane_min_distance = float("inf")
    for left_index, left in enumerate(actors):
        for right in actors[left_index + 1:]:
            if int(left["logical_lane_index"]) == int(right["logical_lane_index"]):
                continue
            cross_lane_pair_count += 1
            delta_longitudinal = abs(float(left["longitudinal_offset_m"]) - float(right["longitudinal_offset_m"]))
            if use_actual_geometry:
                left_location = left["transform"]["location"]
                right_location = right["transform"]["location"]
                distance = math.sqrt(
                    (float(left_location["x"]) - float(right_location["x"])) ** 2
                    + (float(left_location["y"]) - float(right_location["y"])) ** 2
                )
            else:
                lateral_delta = abs(float(left["lateral_offset_m"]) - float(right["lateral_offset_m"]))
                distance = math.sqrt(delta_longitudinal ** 2 + lateral_delta ** 2)
            cross_lane_min_distance = min(cross_lane_min_distance, distance)
            if delta_longitudinal <= near_threshold:
                near_pair_count += 1
    # A near-row event is a cross-lane pair whose longitudinal centers are
    # close.  Pair-normalization avoids treating a single close pair as a
    # whole-row event and makes the metric comparable across lane counts.
    near_row_rate = round(near_pair_count / cross_lane_pair_count, 6) if cross_lane_pair_count else 0.0
    collision_distance = float(qa_config["cross_lane_collision_distance_m"])
    static_reasons: list[str] = []
    if actors and same_lane_min_gap < float(qa_config["same_lane_min_gap_m"]):
        static_reasons.append("same_lane_min_gap_below_threshold")
    if actors and coverage_ratio < float(qa_config["longitudinal_coverage_ratio_min"]):
        static_reasons.append("longitudinal_coverage_below_threshold")
    if balance > int(qa_config["lane_count_balance_max_difference"]):
        static_reasons.append("lane_count_balance_exceeded")
    if near_row_rate > float(qa_config["cross_lane_near_row_rate_max"]):
        static_reasons.append("cross_lane_near_row_rate_exceeded")
    if cross_lane_min_distance < collision_distance:
        static_reasons.append("static_cross_lane_collision_exclusion_failed")
    if low >= high:
        static_reasons.append("invalid_longitudinal_window")
    return {
        "status": "pass" if not static_reasons else "fail_closed",
        "reasons": static_reasons,
        "same_lane_min_gap_m": same_lane_min_gap if math.isfinite(same_lane_min_gap) else None,
        "longitudinal_coverage_ratio": coverage_ratio,
        "lane_actor_counts": lane_counts,
        "lane_count_balance": balance,
        "cross_lane_near_row_threshold_m": near_threshold,
        "cross_lane_near_row_rate": near_row_rate,
        "cross_lane_near_row_pair_count": near_pair_count,
        "cross_lane_cross_pair_count": cross_lane_pair_count,
        "cross_lane_min_center_distance_m": round(cross_lane_min_distance, 6) if math.isfinite(cross_lane_min_distance) else None,
        "spawn_collision": {"status": "pending_carla_hard_gate", "static_proxy": "pass" if not static_reasons else "fail"},
        "topology": {
            "status": "frozen_carla_waypoint_layout_pending_spawn_replay" if use_actual_geometry else "pending_carla_waypoint_and_conflict_exclusion_hard_gate",
            "static_proxy": "carla_waypoint_geometry_and_lane_count" if use_actual_geometry else "lane_count_and_window_only",
        },
        "replay": {"status": "pending_carla_replay", "algorithm": R4_TRAFFIC_RULE_VERSION},
    }


def _apply_conflict_safe_coverage_policy(
    qa: dict[str, Any],
    actors: list[dict[str, Any]],
    candidate_audit: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Gate coverage on the audited conflict-safe window without hiding nominal loss."""

    by_lane_candidates: dict[int, list[float]] = defaultdict(list)
    for item in candidate_audit:
        if item.get("accepted"):
            by_lane_candidates[int(item["lane_index"])].append(float(item["longitudinal_offset_m"]))
    by_lane_actors: dict[int, list[float]] = defaultdict(list)
    for actor in actors:
        by_lane_actors[int(actor["logical_lane_index"])].append(float(actor["longitudinal_offset_m"]))
    effective_by_lane: dict[str, float] = {}
    safe_window_by_lane: dict[str, list[float]] = {}
    for lane_index, positions in sorted(by_lane_candidates.items()):
        if not positions or not by_lane_actors.get(lane_index):
            effective_by_lane[str(lane_index)] = 0.0
            continue
        safe_low, safe_high = min(positions), max(positions)
        actor_low, actor_high = min(by_lane_actors[lane_index]), max(by_lane_actors[lane_index])
        safe_window_by_lane[str(lane_index)] = [round(safe_low, 6), round(safe_high, 6)]
        safe_span = safe_high - safe_low
        effective_by_lane[str(lane_index)] = round(
            (actor_high - actor_low) / safe_span if safe_span > 0.0 else 0.0,
            6,
        )
    effective_ratio = min(effective_by_lane.values(), default=0.0)
    nominal_ratio = qa.get("longitudinal_coverage_ratio")
    threshold = float(config["traffic"]["qa"]["longitudinal_coverage_ratio_min"])
    qa["nominal_longitudinal_coverage_ratio"] = nominal_ratio
    qa["effective_longitudinal_coverage_ratio"] = effective_ratio
    qa["effective_longitudinal_coverage_ratio_min"] = threshold
    qa["conflict_safe_coverage_policy"] = "nominal_valid_window_formal_gate_effective_window_diagnostic_only"
    qa["conflict_safe_window_by_lane_m"] = safe_window_by_lane
    if nominal_ratio is not None and float(nominal_ratio) < threshold:
        qa["capacity_finding"] = {
            "status": "recorded",
            "reason": "nominal_window_reduced_by_hard_conflict_exclusion",
            "nominal_ratio": float(nominal_ratio),
            "threshold": threshold,
            "no_actor_truncation": True,
            "formal_gate": "fail_closed_nominal_window",
        }
    # The effective safe window is useful to explain how much capacity was
    # removed by conflict exclusion, but it is never the denominator for the
    # frozen protocol gate.  In particular, do not remove the nominal failure
    # reason or turn a fail-closed layout into pass here.
    qa["effective_window_diagnostic"] = {
        "status": "pass" if effective_ratio >= threshold else "below_nominal_threshold_diagnostic",
        "formal_gate_uses": "nominal_longitudinal_coverage_ratio",
        "threshold": threshold,
    }
    return qa


def formal_traffic_gate(
    traffic_qa: dict[str, Any],
    *,
    traffic: dict[str, Any] | None = None,
    world_state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    threshold: float | None = None,
    require_nominal: bool = False,
) -> dict[str, Any]:
    """Evaluate the frozen R4 traffic gates without mutating their evidence.

    This is intentionally shared by worker validation, execution finalization,
    and closure construction.  ``effective_longitudinal_coverage_ratio`` is
    excluded from the formal calculation by design.
    """

    traffic = traffic or {}
    qa_config = (config or {}).get("traffic", {}).get("qa", {}) or traffic.get("qa_config") or {}
    threshold_value = float(threshold if threshold is not None else qa_config.get("longitudinal_coverage_ratio_min", 0.70))
    actor_count = int(traffic.get("actual_actor_count", traffic.get("planned_actor_count", 0)))
    reasons: list[str] = []
    if actor_count <= 0:
        return {"status": "pass", "applicable": False, "reasons": [], "threshold": threshold_value}

    status = str(traffic_qa.get("status", "missing"))
    if status != "pass":
        reasons.append(f"traffic_status_{status}")
    coverage = traffic_qa.get("nominal_longitudinal_coverage_ratio")
    if require_nominal and coverage is None:
        reasons.append("nominal_longitudinal_coverage_missing")
    if coverage is None and not require_nominal:
        coverage = traffic_qa.get("longitudinal_coverage_ratio")
    if coverage is None or float(coverage) < threshold_value:
        reasons.append("nominal_longitudinal_coverage_below_threshold")
    elif "nominal_longitudinal_coverage_ratio" in traffic_qa and abs(float(coverage) - float(traffic_qa.get("longitudinal_coverage_ratio", coverage))) > 1e-9:
        reasons.append("nominal_coverage_field_drift")

    min_gap = traffic_qa.get("same_lane_min_gap_m")
    if min_gap is None or float(min_gap) < float(qa_config.get("same_lane_min_gap_m", 10.0)):
        reasons.append("same_lane_min_gap_below_threshold")
    balance = traffic_qa.get("lane_count_balance")
    if balance is None or int(balance) > int(qa_config.get("lane_count_balance_max_difference", 1)):
        reasons.append("lane_count_balance_exceeded")
    near_row = traffic_qa.get("cross_lane_near_row_rate")
    if near_row is None or float(near_row) > float(qa_config.get("cross_lane_near_row_rate_max", 0.35)):
        reasons.append("cross_lane_near_row_rate_exceeded")

    spawn = traffic_qa.get("spawn_collision") or {}
    if spawn.get("status") != "pass":
        reasons.append("spawn_collision_not_pass")
    topology = traffic_qa.get("topology") or {}
    conflict = topology.get("conflict_exclusion") or {}
    if conflict.get("status") != "pass":
        reasons.append("conflict_exclusion_not_pass")
    junction_allowed = topology.get("junction_policy") == "rc_v2_driving_waypoint_allowed"
    if int(topology.get("junction_waypoint_count", -1)) != 0 and not junction_allowed:
        reasons.append("junction_waypoint_count_nonzero")

    replay = traffic_qa.get("replay") or {}
    if replay.get("status") != "pass" or replay.get("match") is not True:
        reasons.append("planned_actual_replay_not_pass")
    if replay.get("planned_replay_hash") != replay.get("actual_replay_hash"):
        reasons.append("planned_actual_replay_hash_mismatch")
    if world_state is not None and world_state.get("traffic_replay_hash") != replay.get("actual_replay_hash"):
        reasons.append("world_state_replay_hash_mismatch")

    return {
        "status": "pass" if not reasons else "fail_closed",
        "applicable": True,
        "reasons": sorted(set(reasons)),
        "threshold": threshold_value,
        "nominal_longitudinal_coverage_ratio": float(coverage) if coverage is not None else None,
        "effective_longitudinal_coverage_ratio_diagnostic": traffic_qa.get("effective_longitudinal_coverage_ratio"),
    }


def assert_formal_traffic_gate(
    traffic_qa: dict[str, Any],
    *,
    traffic: dict[str, Any] | None = None,
    world_state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    threshold: float | None = None,
    require_nominal: bool = False,
    sample_id: str = "unknown",
) -> dict[str, Any]:
    result = formal_traffic_gate(
        traffic_qa, traffic=traffic, world_state=world_state, config=config,
        threshold=threshold, require_nominal=require_nominal,
    )
    if result["status"] != "pass":
        raise ConfigError(f"R4 formal traffic gate failed for {sample_id}: {result['reasons']}")
    return result


def _generate_master_layout(site: dict[str, Any], anchor: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    lane_count = int(anchor["lane_count_at_anchor"])
    strata = int(config["traffic"]["strata_per_lane"])
    low, high = [float(value) for value in config["traffic"]["longitudinal_range_m"]]
    jitter = float(config["traffic"]["stratum_jitter_m"])
    traffic_rule_version = _traffic_rule_version(config)
    master_seed = derive_seed(int(config["traffic"]["master_layout_seed"]), traffic_rule_version, anchor["anchor_id"], "density40_master")
    lane_width = float(config["traffic"]["lane_width_m"])
    conflict_safe = _uses_conflict_safe_layout(config)
    topology_anchor = None
    if _uses_waypoint_topology(config):
        topology = config.get("_traffic_topology") or {}
        topology_anchor = next((item for item in topology.get("anchors", []) if item.get("anchor_id") == anchor["anchor_id"]), None)
        if topology_anchor is None:
            raise ConfigError(f"R4 CARLA topology layout has no anchor {anchor['anchor_id']}")
        if len(topology_anchor.get("actors", [])) != strata * lane_count:
            raise ConfigError(f"R4 CARLA topology actor count is not {strata} per lane for {anchor['anchor_id']}")
    qa: dict[str, Any] | None = None
    actors: list[dict[str, Any]] = []
    chosen_attempt = None
    if topology_anchor is not None and conflict_safe:
        # Conflict-safe CARLA probing is the authority for the actual lane
        # topology and candidate-pool result.  Re-sampling positions here
        # would silently replace the audited CARLA layout with a different
        # offline layout.
        candidate = deepcopy(topology_anchor["actors"])
        candidate.sort(key=lambda item: (int(item["logical_lane_index"]), int(item["master_ordinal"])))
        if len(candidate) != strata * lane_count:
            raise ConfigError(f"R4 conflict-safe topology actor count drifted for {anchor['anchor_id']}")
        seen_keys: set[tuple[int, int]] = set()
        for actor in candidate:
            key = (int(actor["logical_lane_index"]), int(actor["master_ordinal"]))
            if key in seen_keys:
                raise ConfigError(f"R4 conflict-safe topology actor key duplicated for {anchor['anchor_id']}: {key}")
            seen_keys.add(key)
            expected_lateral = round((key[0] - (lane_count - 1) / 2.0) * lane_width, 6)
            if abs(float(actor["lateral_offset_m"]) - expected_lateral) > 1e-5:
                raise ConfigError(f"R4 conflict-safe topology lateral offset drifted for {anchor['anchor_id']}: {key}")
            if actor["vehicle_blueprint_id"] != config["traffic"]["vehicle_blueprint_id"]:
                raise ConfigError(f"R4 conflict-safe topology vehicle blueprint drifted for {anchor['anchor_id']}: {key}")
            if actor["control"] != config["traffic"]["actor_control"]:
                raise ConfigError(f"R4 conflict-safe topology actor control drifted for {anchor['anchor_id']}: {key}")
            if actor["topology_rule_version"] != _conflict_topology_rule_version(config):
                raise ConfigError(f"R4 conflict-safe topology rule drifted for {anchor['anchor_id']}: {key}")
            if actor["conflict_exclusion_rule_version"] != _conflict_exclusion_rule_version(config):
                raise ConfigError(f"R4 conflict-safe topology exclusion rule drifted for {anchor['anchor_id']}: {key}")
        candidate_qa = _layout_qa(candidate, lane_count, config, use_actual_geometry=True)
        candidate_qa = _apply_conflict_safe_coverage_policy(
            candidate_qa,
            candidate,
            topology_anchor["candidate_audit"],
            config,
        )
        candidate_qa["replay"]["algorithm"] = traffic_rule_version
        if candidate_qa["status"] != "pass":
            raise ConfigError(f"R4 conflict-safe topology failed static QA for {anchor['anchor_id']}: {candidate_qa['reasons']}")
        actors = candidate
        qa = candidate_qa
        chosen_attempt = int(topology_anchor.get("qa", {}).get("layout_attempt", 0))
    else:
        for attempt in range(64):
            candidate = []
            for lane_index in range(lane_count):
                rng = random.Random(derive_seed(master_seed, "lane", lane_index, attempt))
                positions: list[float] = []
                for ordinal in range(strata):
                    center = low + (ordinal + 0.5) * ((high - low) / strata)
                    position = round(max(low + 0.25, min(high - 0.25, center + rng.uniform(-jitter, jitter))), 6)
                    positions.append(position)
                for ordinal, position in enumerate(sorted(positions)):
                    lateral = (lane_index - (lane_count - 1) / 2.0) * lane_width
                    actor_id = "r4_actor_" + v3_digest(
                        "r4_traffic_actor",
                        {"anchor_id": anchor["anchor_id"], "lane_index": lane_index, "master_ordinal": ordinal},
                        length=16,
                    )
                    actor = {
                        "actor_id": actor_id,
                        "carla_actor_id": None,
                        "vehicle_blueprint_id": config["traffic"]["vehicle_blueprint_id"],
                        "master_ordinal": ordinal,
                        "logical_lane_index": lane_index,
                        "lane_id": f"logical_lane_{lane_index:02d}",
                        "longitudinal_offset_m": position,
                        "lateral_offset_m": round(lateral, 6),
                        "transform": _actor_transform(site, anchor, position, lateral, lane_index, config),
                        "control": config["traffic"]["actor_control"],
                    }
                    if topology_anchor is not None:
                        frozen = next(
                            (
                                item for item in topology_anchor["actors"]
                                if int(item["logical_lane_index"]) == lane_index
                                and int(item["master_ordinal"]) == ordinal
                            ),
                            None,
                        )
                        if frozen is None:
                            raise ConfigError(
                                f"R4 CARLA topology actor is missing lane={lane_index} ordinal={ordinal} "
                                f"for {anchor['anchor_id']}"
                            )
                        if abs(float(frozen["longitudinal_offset_m"]) - position) > 1e-5:
                            raise ConfigError(f"R4 CARLA topology offset drifted for {anchor['anchor_id']}")
                        actor.update({
                            "lane_id": str(frozen["frozen_lane_id"]),
                            "frozen_lane_id": int(frozen["frozen_lane_id"]),
                            "waypoint_road_id": int(frozen["waypoint_road_id"]),
                            "waypoint_section_id": int(frozen["waypoint_section_id"]),
                            "waypoint_s_m": float(frozen["waypoint_s_m"]),
                            "transform": deepcopy(frozen["transform"]),
                            "topology_rule_version": _conflict_topology_rule_version(config) if conflict_safe else R4_CARLA_WAYPOINT_TOPOLOGY_RULE_VERSION,
                        })
                    candidate.append(actor)
            candidate.sort(key=lambda item: (int(item["logical_lane_index"]), int(item["master_ordinal"])))
            candidate_qa = _layout_qa(candidate, lane_count, config, use_actual_geometry=topology_anchor is not None)
            if topology_anchor is not None:
                candidate_qa["replay"]["algorithm"] = traffic_rule_version
            if candidate_qa["status"] == "pass":
                actors = candidate
                qa = candidate_qa
                chosen_attempt = attempt
                break
    if qa is None:
        raise ConfigError(f"R4 traffic master layout failed static QA for {anchor['anchor_id']}")
    payload = {
        "schema_version": (
            R4_CONFLICT_SAFE_MASTER_LAYOUT_SCHEMA_V4_VERSION
            if conflict_safe and _conflict_topology_rule_version(config) == R4_CONFLICT_SAFE_TOPOLOGY_RULE_V4_VERSION
            else R4_CONFLICT_SAFE_MASTER_LAYOUT_SCHEMA_V3_VERSION
            if conflict_safe and _conflict_topology_rule_version(config) == R4_CONFLICT_SAFE_TOPOLOGY_RULE_V3_VERSION
            else "benchmark_v3_r4_conflict_safe_master_layout_0.2"
            if conflict_safe
            else R4_TRAFFIC_LAYOUT_TOPOLOGY_V2_SCHEMA_VERSION if topology_anchor is not None else R4_TRAFFIC_LAYOUT_SCHEMA_VERSION
        ),
        "protocol_version": R4_PROTOCOL_VERSION,
        "rule_version": traffic_rule_version,
        "anchor_id": anchor["anchor_id"],
        "map_id": anchor["map_id"],
        "site_id": anchor["site_id"],
        "lane_count": lane_count,
        "master_layout_seed": int(master_seed),
        "master_density": 40.0,
        "layout_attempt": int(chosen_attempt),
        "actors": actors,
        "qa": {**qa, "seed_replay": {"master_seed": int(master_seed), "attempt": int(chosen_attempt)}},
    }
    if conflict_safe:
        payload["conflict_exclusion"] = deepcopy((topology_anchor or {}).get("conflict_exclusion", {}))
        if not payload["conflict_exclusion"]:
            payload["conflict_exclusion"] = {
                "rule_version": _conflict_exclusion_rule_version(config),
                "is_junction_hard_reject": True,
                "conflict_zone_radius_m": float(config["traffic"].get("conflict_zone_radius_m", 10.0)),
                "topology_rule_version": _conflict_topology_rule_version(config),
                "candidate_pool_rule": "carla_probe_required_before_static_freeze",
            }
    payload["master_layout_hash"] = _sha256_json({key: value for key, value in payload.items() if key != "master_layout_hash"})
    layout_schema = (
        "r4_conflict_safe_master_layout_v4.schema.json"
        if conflict_safe and _conflict_topology_rule_version(config) == R4_CONFLICT_SAFE_TOPOLOGY_RULE_V4_VERSION
        else "r4_conflict_safe_master_layout_v3.schema.json"
        if conflict_safe and _conflict_topology_rule_version(config) == R4_CONFLICT_SAFE_TOPOLOGY_RULE_V3_VERSION
        else "r4_conflict_safe_master_layout.schema.json"
        if conflict_safe
        else "r4_traffic_layout_topology_v2.schema.json" if topology_anchor is not None else "r4_traffic_layout.schema.json"
    )
    validate_schema_instance(payload, load_json(_schema(layout_schema)), f"R4 master traffic layout {anchor['anchor_id']}")
    return payload


def _select_density_subset(master: dict[str, Any], density: float, config: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    if density == 0.0:
        return [], None
    if density == 40.0:
        return deepcopy(master["actors"]), None
    if density != 20.0:
        raise ConfigError(f"R4 corrective traffic only freezes 0/20/40, got {density}")
    if _uses_spatial_subset(config):
        patterns = ((0, 2, 5, 7), (0, 3, 4, 7))
        selected: dict[int, set[int]] = {}
        for lane_index in range(int(master["lane_count"])):
            pattern_index = int(
                derive_seed(
                    int(master["master_layout_seed"]),
                    R4_TRAFFIC_SUBSET_RULE_VERSION,
                    master["anchor_id"],
                    "lane",
                    lane_index,
                )
                % len(patterns)
            )
            selected[lane_index] = set(patterns[pattern_index])
        actors = [
            deepcopy(item)
            for item in master["actors"]
            if int(item["master_ordinal"]) in selected[int(item["logical_lane_index"])]
        ]
        return actors, None
    parity = int(
        derive_seed(
            int(master["master_layout_seed"]),
            str(master.get("rule_version", R4_TRAFFIC_RULE_VERSION)),
            master["anchor_id"],
            "density20_subset",
        )
        % 2
    )
    actors = [deepcopy(item) for item in master["actors"] if int(item["master_ordinal"]) % 2 == parity]
    return actors, parity


def build_traffic_actor_plan(site: dict[str, Any], anchor: dict[str, Any], recipe: dict[str, Any], sample_id: str, config: dict[str, Any], master: dict[str, Any]) -> dict[str, Any]:
    density = float(recipe["requested_conditions"]["traffic_density_vehicles_per_lane_km"])
    lane_count = int(anchor["lane_count_at_anchor"])
    lane_km, target_count = traffic_actor_count(density, lane_count, float(config["traffic"]["valid_window_m_each_side"]))
    actors, subset_parity = _select_density_subset(master, density, config)
    if len(actors) != target_count:
        raise ConfigError(f"R4 traffic capacity mismatch for {sample_id}: target={target_count}, planned={len(actors)}")
    qa = (
        _layout_qa(
            actors,
            lane_count,
            config,
            use_actual_geometry=_uses_waypoint_topology(config),
        )
        if _uses_waypoint_topology(config)
        else deepcopy(master["qa"])
    )
    if config.get("correction_provenance"):
        # The formal denominator is always this recipe's frozen nominal valid
        # window.  Keep the field explicit in the correction ledger so a
        # downstream closure builder cannot fall back to an effective
        # conflict-safe window.  Historical R4 static inputs retain their
        # original byte-level ledger representation and remain verifiable.
        qa["nominal_longitudinal_coverage_ratio"] = float(qa["longitudinal_coverage_ratio"])
        qa.setdefault("effective_longitudinal_coverage_ratio", qa["nominal_longitudinal_coverage_ratio"])
        qa["effective_window_diagnostic"] = {
            "status": "diagnostic_only",
            "formal_gate_uses": "nominal_longitudinal_coverage_ratio",
            "threshold": float(config["traffic"]["qa"]["longitudinal_coverage_ratio_min"]),
        }
    qa["density"] = density
    qa["planned_actor_count"] = len(actors)
    qa["target_actor_count"] = target_count
    subset_selection = {
        "master_density": 40.0,
        "subset_density": density,
        "selection_rule": "per-lane deterministic alternating master ordinals",
        "selected_parity": subset_parity,
        "strict_subset": density == 20.0,
        "prefix_requirement": False,
    }
    if _uses_spatial_subset(config):
        subset_selection.update({
            "selection_rule": "per-lane deterministic spatial-balanced ordinal patterns",
            "selection_rule_version": R4_TRAFFIC_SUBSET_RULE_VERSION,
        })
    qa["subset_selection"] = subset_selection
    if density == 20.0:
        master_ids = {item["actor_id"] for item in master["actors"]}
        selected_ids = {item["actor_id"] for item in actors}
        if not selected_ids < master_ids or actors == master["actors"][: len(actors)]:
            raise ConfigError(f"R4 density20 layout is not a spatial strict subset for {sample_id}")
        qa["subset_selection"]["actor_id_transform_inheritance"] = all(
            next(item for item in master["actors"] if item["actor_id"] == selected["actor_id"])["transform"] == selected["transform"]
            for selected in actors
        )
        if _uses_spatial_subset(config):
            qa["subset_selection"]["selected_ordinals_by_lane"] = {
                str(lane_index): sorted({int(item["master_ordinal"]) for item in actors if int(item["logical_lane_index"]) == lane_index})
                for lane_index in range(lane_count)
            }
    qa["spawn_collision"] = {"status": "pending_carla_hard_gate", "static_proxy": "pass"}
    qa["replay"] = {"status": "pending_carla_replay", "master_layout_hash": master["master_layout_hash"]}
    planned_world_state_hash = _sha256_json({
        "protocol_version": R4_PROTOCOL_VERSION,
        "sample_id": sample_id,
        "recipe_id": recipe["observation_recipe_id"],
        "density": density,
        "actors": actors,
        "master_layout_hash": master["master_layout_hash"],
    })
    if _uses_conflict_safe_layout(config):
        replay_hash = canonical_replay(
            protocol_version=R4_PROTOCOL_VERSION,
            master_layout_hash=master["master_layout_hash"],
            density=density,
            planned_actors=actors,
        )["planned_hash"]
        qa["replay"].update({
            "schema_version": REPLAY_CANONICAL_SCHEMA_VERSION,
            "rule_version": REPLAY_CANONICAL_RULE_VERSION,
            "canonical_transform_precision": REPLAY_ROTATION_PRECISION,
        })
    else:
        replay_hash = _sha256_json({"master_layout_hash": master["master_layout_hash"], "density": density, "actors": actors})
    qa["replay"]["planned_replay_hash"] = replay_hash
    return {
        "density_unit": "vehicles_per_lane_km",
        "requested_density": density,
        "valid_lane_count": lane_count,
        "valid_window_m": round(2.0 * float(config["traffic"]["valid_window_m_each_side"]), 6),
        "lane_km": lane_km,
        "target_actor_count": target_count,
        "planned_actor_count": len(actors),
        "capacity_status": "sufficient",
        "master_layout_seed": int(master["master_layout_seed"]),
        "master_layout_hash": master["master_layout_hash"],
        "layout_rule_version": str(master.get("rule_version", R4_TRAFFIC_RULE_VERSION)),
        "actors": actors,
        "nested_parent_density": 40.0 if density == 20.0 else None,
        "planned_world_state_hash": planned_world_state_hash,
        "replay_hash": replay_hash,
        "qa": qa,
    }


def _recipe_for_core_value(core: list[dict[str, Any]], factor: str, value: Any) -> dict[str, Any]:
    for recipe in core:
        values = _factor_values(recipe["requested_conditions"])
        if factor == "reference" and recipe["is_reference"]:
            return recipe
        if not recipe["is_reference"] and recipe["changed_factor"] == factor and values[factor] == value:
            return recipe
    raise ConfigError(f"R4 Core recipe value is missing: {factor}={value}")


def _pressure_recipe(anchor_id: str, config: dict[str, Any]) -> dict[str, Any]:
    conditions = {
        "height_m": 100.0,
        "pitch_deg": -73.5,
        "yaw_relative_deg": 7.5,
        "traffic_density_vehicles_per_lane_km": 40.0,
        "weather": {"family": "fog", "cloudiness": 55.0, "precipitation": 0.0, "wetness": 10.0, "fog_density": 45.0, "fog_distance_m": 25.0},
        "sun_altitude_deg": 5.0,
        "sun_azimuth_relative_deg": 90.0,
    }
    payload = {"rule_version": R4_RECIPE_RULE_VERSION, "anchor_id": anchor_id, "kind": "high_height_high_traffic_pressure", "conditions": conditions}
    return {
        "observation_recipe_id": make_recipe_id("r4_corrective_pressure", payload),
        "recipe_family": "corrective_pressure",
        "subset_scope": "corrective_pilot",
        "rule_version": R4_RECIPE_RULE_VERSION,
        "ordinal": None,
        "sampling_seed": derive_seed(int(config["recipe_freeze"]["seed"]), R4_RECIPE_RULE_VERSION, anchor_id, "pressure"),
        "requested_conditions": conditions,
        "factor_levels": {"height": None, "pitch": None, "relative_yaw": None, "traffic_density": None, "weather": None, "illumination": None},
        "is_reference": False,
        "changed_factor": "multi_factor",
        "factor_level": None,
        "challenge_row_id": None,
        "challenge_level_tuple": None,
    }


def _camera_geometry(anchor: dict[str, Any], condition: dict[str, Any]) -> dict[str, Any]:
    road_z = round(float(anchor["pose_world"]["z"]), 6)
    relative_height = round(float(condition["height_m"]), 6)
    return {
        "road_reference_z_m": road_z,
        "requested_relative_height_m": relative_height,
        "expected_camera_world_z_m": round(road_z + relative_height, 6),
        "source": "frozen_anchor_pose_world_z",
        "actual_resolution_at_execution": "CARLA_waypoint_ground_z_and_camera_transform",
    }


def _recipe_expected_projection(site: dict[str, Any], anchor: dict[str, Any], recipe: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    condition = _execution_condition(recipe["requested_conditions"], config)
    geometry = _camera_geometry(anchor, condition)
    downward_angle = max(1.0, min(89.0, abs(float(condition["pitch_deg"]))))
    forward_ground_intersection = round(float(condition["height_m"]) / math.tan(math.radians(downward_angle)), 6)
    payload = {
        "mode": "r4_recipe_specific_projection_contract",
        "projection_rule_version": R4_PROJECTION_RULE_VERSION,
        "source_opendrive_sha256": site["source_opendrive_sha256"],
        "site_id": site["site_id"],
        "anchor_id": anchor["anchor_id"],
        "recipe_id": recipe["observation_recipe_id"],
        "camera_conditions": condition,
        "camera_geometry": geometry,
        "derived_projection_expectation": {
            "downward_angle_deg": downward_angle,
            "forward_ground_intersection_from_camera_m": forward_ground_intersection,
            "fov_degrees": float(condition["fov_degrees"]),
            "image_size": [int(condition["image_width"]), int(condition["image_height"])],
        },
        "qa_expectation": {
            "compare_actual_geometry": True,
            "target_corridor_semantics": "target-corridor-lane-recovery",
            "default_reference_area_gate": 0.04 if site["structural_cell"] == "Ramp-1-to-Mainline-8" else (0.05 if site["structural_cell"].startswith("Intersection-Approach") else None),
            "non_default_area_gate": "recipe_specific_projection_not_default_reference_threshold",
            "boundary_length_diagonal_ratio_min": 0.15,
            "opendrive_lane_coverage_min": 0.95,
            "roadlines_explained_recall_min": 0.75,
        },
    }
    payload["projection_fingerprint"] = _sha256_json(payload)
    return payload


def _row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (R4_MAPS.index(row["map_id"]), row["site_id"], row["anchor_id"], row["pilot_kind"], row["observation_recipe_id"])


def _build_corrective_rows(config: dict[str, Any], recipes: dict[str, Any], masters: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    sites = config["_source_sites"]
    anchors = config["_source_anchors"]
    choices = {item["anchor_id"]: item for item in config["selection"]["sites"]}
    core = recipes["core"]
    rows: list[dict[str, Any]] = []

    def append_row(anchor_id: str, recipe: dict[str, Any], pilot_kind: str, recipe_origin: str) -> None:
        choice = choices[anchor_id]
        site = sites[choice["site_id"]]
        anchor = anchors[anchor_id]
        world_seed = derive_seed(int(config["recipe_freeze"]["seed"]), R4_RECIPE_RULE_VERSION, "world", anchor_id, recipe["observation_recipe_id"])
        sensor_seed = derive_seed(int(config["recipe_freeze"]["seed"]), R4_RECIPE_RULE_VERSION, "sensor", anchor_id, recipe["observation_recipe_id"])
        condition = _execution_condition(recipe["requested_conditions"], config)
        layout_fingerprint = None
        if config["traffic"].get("sample_id_bind_master_layout_hash") and float(condition["traffic_density_vehicles_per_lane_km"]) > 0.0:
            layout_fingerprint = str(masters[anchor_id]["master_layout_hash"])
        sample_id = make_sample_id(
            site["map_id"], site["site_id"], anchor_id, recipe["observation_recipe_id"],
            world_seed, sensor_seed, R4_PROTOCOL_VERSION,
            layout_fingerprint=layout_fingerprint,
        )
        traffic_plan = build_traffic_actor_plan(site, anchor, {"observation_recipe_id": recipe["observation_recipe_id"], "requested_conditions": condition}, sample_id, config, masters[anchor_id])
        rows.append({
            "protocol_version": R4_PROTOCOL_VERSION,
            "sample_id": sample_id,
            "subset": "corrective_pilot",
            "source_subset": choice["source_subset"],
            "pilot_kind": pilot_kind,
            "recipe_origin": recipe_origin,
            "map_id": site["map_id"],
            "site_id": site["site_id"],
            "anchor_id": anchor_id,
            "leakage_group_id": site["leakage_group_id"],
            "structural_cell": site["structural_cell"],
            "observation_recipe_id": recipe["observation_recipe_id"],
            "recipe_family": recipe["recipe_family"],
            "requested_conditions": condition,
            "factor_levels": deepcopy(recipe["factor_levels"]),
            "is_reference": bool(recipe["is_reference"]),
            "reference_sample_id": None,
            "changed_factor": recipe["changed_factor"],
            "challenge_row_id": recipe["challenge_row_id"],
            "challenge_level_tuple": recipe["challenge_level_tuple"],
            "world_seed": int(world_seed),
            "sensor_seed": int(sensor_seed),
            "camera_geometry": _camera_geometry(anchor, condition),
            "recipe_expected_projection": _recipe_expected_projection(site, anchor, recipe, config),
            "traffic_plan": traffic_plan,
            "official_labels": list(OFFICIAL_LABELS),
            "qa_policy_version": config["qa"]["policy_version"],
            "qa_status": "pending_carla",
            "generation_status": "planned",
            "pilot_evidence": True,
            "canonical_dataset": False,
            "formal_split": "not_run",
            "p0": "closed_not_mutated",
            "old_p1": "immutable_not_mutated",
            "rc": "not_entered",
            "full": "not_entered",
            "asset_paths": {name: f"samples/{sample_id}/{name}" for name in R4_ASSETS},
            "incomplete_bundle_disposition": "not_a_pass_until_full_formal_bundle_or_corrective_scope_is_explicit",
        })

    for anchor_id in config["corrective_plan"]["height_anchor_ids"]:
        for height in R4_CORE_HEIGHTS_M:
            recipe = _recipe_for_core_value(core, "reference" if height == 40.0 else "height", height if height != 40.0 else None)
            append_row(anchor_id, recipe, "height_sweep", "r4_core_15_height_recipe")
    for lane_count, anchor_id in sorted(config["corrective_plan"]["density_anchor_by_lane_count"].items(), key=lambda item: int(item[0])):
        for density in (20.0, 40.0):
            append_row(anchor_id, _recipe_for_core_value(core, "traffic_density", density), "density_nested", "r4_core_15_traffic_recipe")
    for anchor_id in config["corrective_plan"]["pressure_anchor_ids"]:
        append_row(anchor_id, _pressure_recipe(anchor_id, config), "high_height_high_traffic_pressure", "r4_custom_pressure_recipe")

    rows.sort(key=_row_sort_key)
    if len(rows) != 24 or len({row["sample_id"] for row in rows}) != 24:
        raise ConfigError("R4 corrective ledger must contain 24 unique planned samples")
    references = {row["anchor_id"]: row["sample_id"] for row in rows if row["is_reference"]}
    for row in rows:
        # The density-only 8-lane anchor is intentionally not expanded into a
        # fifth height sweep.  Its targeted rows therefore carry null rather
        # than borrowing a reference from another anchor.
        row["reference_sample_id"] = references.get(row["anchor_id"])
    return rows


def _development_geometry_contract(config: dict[str, Any], recipes: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for recipe in recipes["development"]:
        for anchor_id, anchor in sorted(config["_source_anchors"].items()):
            geometry = _camera_geometry(anchor, recipe["requested_conditions"])
            rows.append({
                "development_recipe_id": recipe["observation_recipe_id"],
                "subset": recipe["subset_scope"],
                "sampling_seed": int(recipe["sampling_seed"]),
                "anchor_id": anchor_id,
                "map_id": anchor["map_id"],
                "road_reference_z_m": geometry["road_reference_z_m"],
                "requested_relative_height_m": geometry["requested_relative_height_m"],
                "expected_camera_world_z_m": geometry["expected_camera_world_z_m"],
                "height_stratum_contract": "stratified_continuous_uniform",
                "actual_camera_world_z_recorded_at_execution": True,
            })
    return {
        "schema_version": R4_DEVELOPMENT_GEOMETRY_SCHEMA_VERSION,
        "protocol_version": R4_PROTOCOL_VERSION,
        "stage": "p1_corrective_static",
        "algorithm_version": R4_DEVELOPMENT_RECIPE_RULE_VERSION,
        "seed_by_subset": deepcopy(config["development"]["seeds"]),
        "height_range_m": [20.0, 100.0],
        "rows": rows,
        "materialization": "recipe_and_geometry_contract_only_no_images",
    }


def _validate_traffic_nesting(rows: list[dict[str, Any]], config: dict[str, Any] | None = None) -> None:
    grouped: dict[str, dict[float, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row["pilot_kind"] != "density_nested":
            continue
        grouped[row["anchor_id"]][float(row["traffic_plan"]["requested_density"])] = row["traffic_plan"]
    expected_anchors = set((config or {}).get("corrective_plan", {}).get("density_anchor_by_lane_count", {}).values()) or {"anchor_b0f005cc6ffea491", "anchor_10b297bc6ffea491", "anchor_c837c6d0fa98b3e7"}
    if set(grouped) != expected_anchors:
        raise ConfigError("R4 traffic corrective rows do not cover 4/6/8 lane anchors")
    for anchor_id, densities in grouped.items():
        low = densities.get(20.0)
        high = densities.get(40.0)
        if low is None or high is None or not low["actors"] or len(low["actors"]) >= len(high["actors"]):
            raise ConfigError(f"R4 traffic 20/40 pair is incomplete for {anchor_id}")
        low_ids = {item["actor_id"] for item in low["actors"]}
        high_ids = {item["actor_id"] for item in high["actors"]}
        if not low_ids < high_ids or low["actors"] == high["actors"][: len(low["actors"])]:
            raise ConfigError(f"R4 traffic 20 is not a non-prefix strict subset for {anchor_id}")
        high_by_id = {item["actor_id"]: item for item in high["actors"]}
        for actor in low["actors"]:
            if high_by_id[actor["actor_id"]]["transform"] != actor["transform"]:
                raise ConfigError(f"R4 traffic 20 transform inheritance drifted for {anchor_id}")


def _traffic_ledger_row(row: dict[str, Any]) -> dict[str, Any]:
    plan = row["traffic_plan"]
    return {
        "sample_id": row["sample_id"],
        "map_id": row["map_id"],
        "site_id": row["site_id"],
        "anchor_id": row["anchor_id"],
        "structural_cell": row["structural_cell"],
        "requested_density": plan["requested_density"],
        "density_unit": plan["density_unit"],
        "lane_km": plan["lane_km"],
        "valid_lane_count": plan["valid_lane_count"],
        "valid_window_m": plan["valid_window_m"],
        "target_actor_count": plan["target_actor_count"],
        "planned_actor_count": plan["planned_actor_count"],
        "capacity_status": plan["capacity_status"],
        "master_layout_seed": plan["master_layout_seed"],
        "master_layout_hash": plan["master_layout_hash"],
        "actors": plan["actors"],
        "inheritance": {"nested_parent_density": plan["nested_parent_density"], "layout_rule_version": plan["layout_rule_version"]},
        "qa": plan["qa"],
        "planned_world_state_hash": plan["planned_world_state_hash"],
        "replay_hash": plan["replay_hash"],
    }


def validate_r4_formal_bundle(rows: list[dict[str, Any]], subset: str) -> dict[str, Any]:
    """Reject incomplete formal Core-15 or Challenge-L18 bundles."""

    expected = 15 if subset == "core_test" else 18 if subset == "challenge_test" else None
    if expected is None:
        raise ConfigError(f"R4 formal bundle subset is invalid: {subset}")
    if len(rows) != expected or len({row["observation_recipe_id"] for row in rows}) != expected:
        raise ConfigError(f"R4 incomplete {subset} bundle rejected: {len(rows)}/{expected}")
    if sum(bool(row.get("is_reference")) for row in rows) != 1:
        raise ConfigError(f"R4 {subset} bundle must have exactly one default reference")
    if subset == "core_test":
        changed_counts = Counter(row.get("changed_factor") for row in rows if not row.get("is_reference"))
        if changed_counts != Counter({"height": 4, "pitch": 2, "relative_yaw": 2, "traffic_density": 2, "weather": 2, "illumination": 2}):
            raise ConfigError(f"R4 Core-15 factor counts drifted: {changed_counts}")
    else:
        tuples = {tuple(row.get("challenge_level_tuple") or ()) for row in rows}
        if len(tuples) != 18 or () in tuples:
            raise ConfigError("R4 Challenge-L18 bundle has incomplete or duplicate tuples")
    return {"subset": subset, "expected": expected, "completed": len(rows), "status": "complete"}


def validate_r4_execution_parameters(
    config: dict[str, Any],
    *,
    host: str,
    rpc_port: int,
    traffic_manager_port: int,
    graphics_adapter: int,
    output_root: Path,
) -> None:
    """Fail closed on future corrective wrapper identity and path drift."""

    lifecycle = config["lifecycle"]
    if host != lifecycle["host"]:
        raise ConfigError("R4 corrective host does not match the frozen lifecycle")
    if int(rpc_port) != int(lifecycle["rpc_port"]):
        raise ConfigError("R4 corrective RPC port does not match the frozen lifecycle")
    if int(traffic_manager_port) != int(lifecycle["traffic_manager_port"]):
        raise ConfigError("R4 corrective TrafficManager port does not match the frozen lifecycle")
    if int(graphics_adapter) != int(lifecycle["graphics_adapter"]):
        raise ConfigError("R4 corrective GPU does not match the frozen lifecycle")
    if ensure_path_within(Path(output_root), [R4_CORRECTIVE_ARTIFACT_ROOT], "R4 corrective execution output") != R4_CORRECTIVE_ARTIFACT_ROOT:
        raise ConfigError("R4 corrective output path escapes the independent artifact root")


def build_r4_protocol_decision(config: dict[str, Any], source_checksums: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": R4_PROTOCOL_DECISION_SCHEMA_VERSION,
        "protocol_version": R4_PROTOCOL_VERSION,
        "stage": "p1_corrective_static",
        "decision_status": "frozen_for_r4_corrective_static_preparation",
        "historical_p1": {
            "status": "immutable_pilot_evidence",
            "artifact_root": str(V3_ARTIFACT_ROOT / "p1" / "benchmark_v3_p1_20260727_082648"),
            "old_core_bundle_size": 13,
            "old_total_images": 93,
            "old_height_contract": "30/45/60m with 45m default",
            "mutation": "forbidden",
        },
        "primary_finalization": {
            "artifact_root": config["source_finalization"]["artifact_root"],
            "run_id": config["source_finalization"]["run_id"],
            "reason": "retain Core structure coverage under frozen leakage-group split",
            "split_mutation": "forbidden",
            "eligibility_mutation": "forbidden",
            "leakage_group_split": "forbidden",
        },
        "challenge_coverage_limitations": deepcopy(config["selection"]["coverage_limitations"]),
        "safety": {
            "carla_connection": "not_used_in_static_phase",
            "image_generation": "not_run_in_static_phase",
            "registry_write": False,
            "split_write": False,
            "old_p1_mutation": False,
            "p0_mutation": False,
            "rc": "not_entered",
            "full": "not_entered",
            "training": False,
            "commit": False,
        },
        "source_checksums": source_checksums,
    }


def _source_checksums(config: dict[str, Any]) -> dict[str, str]:
    checksums: dict[str, str] = {}
    checksums.update({f"finalization/{name}": digest for name, digest in config["_finalization_hashes"].items()})
    checksums.update({f"p0_closure/{name}": digest for name, digest in config["_p0_closure_hashes"].items()})
    checksums.update({
        f"config/{Path(config['_config_path']).name}": config["_config_sha256"],
        "config/challenge_l18_r4.json": config["_challenge_l18_sha256"],
        "config/qa_policies_ramp_v2_r4.json": config["_qa_policy_sha256"],
        "config/rc_height_calibration.json": config["_rc_calibration_sha256"],
    })
    schema_names = list(R4_STATIC_SCHEMA_NAMES)
    if config.get("schema_version") == "benchmark_v3_r4_p1_corrective_config_0.2":
        schema_names.append("r4_p1_corrective_config_correction.schema.json")
    if _uses_waypoint_topology(config):
        topology_name = Path(config["_traffic_topology_path"]).name
        checksums[f"config/{topology_name}"] = str(config["_traffic_topology_sha256"])
        schema_names.extend(["r4_traffic_topology_layout.schema.json", "r4_traffic_layout_topology_v2.schema.json"])
        if _uses_conflict_safe_layout(config):
            if _conflict_topology_rule_version(config) == R4_CONFLICT_SAFE_TOPOLOGY_RULE_V4_VERSION:
                schema_names.extend(["r4_traffic_conflict_safe_layout_v4.schema.json", "r4_conflict_safe_master_layout_v4.schema.json"])
            elif _conflict_topology_rule_version(config) == R4_CONFLICT_SAFE_TOPOLOGY_RULE_V3_VERSION:
                schema_names.extend(["r4_traffic_conflict_safe_layout_v3.schema.json", "r4_conflict_safe_master_layout_v3.schema.json"])
            else:
                schema_names.extend(["r4_traffic_conflict_safe_layout.schema.json", "r4_conflict_safe_master_layout.schema.json"])
            schema_names.append("r4_replay_canonical.schema.json")
    for name in schema_names:
        checksums[f"schema/{name}"] = file_sha256(_schema(name))
    return dict(sorted(checksums.items()))


def build_r4_plan(
    config_path: Path = R4_CONFIG_PATH,
    *,
    config_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the complete deterministic R4 corrective plan without CARLA."""

    config = load_r4_config(config_path, config_override=config_override)
    recipe_table = build_r4_recipe_table(config)
    recipes = {"core": recipe_table["core"], "challenge": recipe_table["challenge"], "development": recipe_table["development"]}
    masters = {
        anchor_id: _generate_master_layout(
            config["_source_sites"][anchor["site_id"]], anchor, config
        )
        for anchor_id, anchor in sorted(config["_source_anchors"].items())
    }
    rows = _build_corrective_rows(config, recipes, masters)
    _validate_traffic_nesting(rows, config)
    traffic_rows = [_traffic_ledger_row(row) for row in rows]
    traffic_rows.sort(key=lambda row: row["sample_id"])
    source_checksums = _source_checksums(config)
    development_contract = _development_geometry_contract(config, recipes)
    validate_schema_instance(development_contract, load_json(_schema("r4_development_geometry_contract.schema.json")), "R4 Development geometry contract")
    projection_rows = [
        {
            "sample_id": row["sample_id"],
            "anchor_id": row["anchor_id"],
            "height_m": row["requested_conditions"]["height_m"],
            "is_reference": row["is_reference"],
            "camera_world_z_m": row["camera_geometry"]["expected_camera_world_z_m"],
            "projection_fingerprint": row["recipe_expected_projection"]["projection_fingerprint"],
        }
        for row in rows
    ]
    plan = {
        "schema_version": R4_PLAN_SCHEMA_VERSION,
        "protocol_version": R4_PROTOCOL_VERSION,
        "stage": "p1_corrective_static",
        "status": "static_ready_waiting_explicit_r4_corrective_carla_approval",
        "selected_sites": [
            {
                **choice,
                "source_refs": list(config["_source_sites"][choice["site_id"]]["source_refs"]),
                "source_opendrive_sha256": config["_source_sites"][choice["site_id"]]["source_opendrive_sha256"],
                "anchor_pose_world": deepcopy(config["_source_anchors"][choice["anchor_id"]]["pose_world"]),
                "lane_count_at_anchor": int(config["_source_anchors"][choice["anchor_id"]]["lane_count_at_anchor"]),
                "road_reference_z_m": float(config["_source_anchors"][choice["anchor_id"]]["pose_world"]["z"]),
                "final_site_eligibility": config["_source_sites"][choice["site_id"]]["final_site_eligibility"],
            }
            for choice in config["selection"]["sites"]
        ],
        "coverage_audit": {
            "map_ids": list(R4_MAPS),
            "selected_site_count": 4,
            "distinct_leakage_group_count": 4,
            "structural_cells": dict(sorted(Counter(row["structural_cell"] for row in rows).items())),
            "height_sweep": {"height_values_m": list(R4_CORE_HEIGHTS_M), "anchor_count": 3, "row_count": 15},
            "density_anchors": {str(item["traffic_plan"]["valid_lane_count"]): item["anchor_id"] for item in rows if item["pilot_kind"] == "density_nested" and item["traffic_plan"]["requested_density"] == 40.0},
            "coverage_limitations": deepcopy(config["selection"]["coverage_limitations"]),
            "straight_2lane": "Core/Challenge unavailable/N/A",
            "fail_closed_if_frozen_source_changes": True,
        },
        "recipe_summary": {
            "core_per_anchor": 15,
            "challenge_per_anchor": 18,
            "core_height_levels_m": list(R4_CORE_HEIGHTS_M),
            "challenge_height_mapping_m": dict(R4_CHALLENGE_HEIGHT_LEVELS_M),
            "core_total_if_full_bundle": 15 * len(config["corrective_plan"]["height_anchor_ids"]),
            "challenge_total_if_full_bundle": 18 * 0,
            "corrective_expected_rows": len(rows),
            "development_recipe_count": len(recipes["development"]),
            "recipe_table_sha256": recipe_table["recipe_table_sha256"],
            "challenge_selected_columns": list(config["recipe_freeze"]["challenge_selected_columns"]),
            "development_collision_count": 0,
            "allowed_shared_reference": "Challenge L18 default reference may equal Core default by protocol design; corrective rows are not a formal subset",
        },
        "expected_image_count": len(rows),
        "official_labels": list(OFFICIAL_LABELS),
        "pilot_evidence": True,
        "canonical_dataset": False,
        "source_checksums": source_checksums,
        "development_sampling_audit": {
            "algorithm_version": R4_DEVELOPMENT_RECIPE_RULE_VERSION,
            "height_range_m": [20.0, 100.0],
            "train_seed": int(config["development"]["seeds"]["development_train"]),
            "val_seed": int(config["development"]["seeds"]["development_val"]),
            "recipe_count": len(recipes["development"]),
            "geometry_contract_rows": len(development_contract["rows"]),
            "recorded_fields": ["road_reference_z_m", "requested_relative_height_m", "expected_camera_world_z_m", "actual_camera_world_z_at_execution"],
            "stratified_height_values_m": sorted({row["requested_conditions"]["height_m"] for row in recipes["development"]}),
        },
        "projection_audit": {
            "projection_rule_version": R4_PROJECTION_RULE_VERSION,
            "reference_height_m": 40.0,
            "audited_height_values_m": sorted({row["requested_conditions"]["height_m"] for row in rows}),
            "old_45m_projection_reused": False,
            "recipe_specific_expectations": projection_rows,
            "non_default_area_gate": "not_mechanical_default_area_gate",
            "actual_geometry_comparison_required": True,
        },
        "traffic_qa_contract": {
            "master_density": 40.0,
            "subset_density": 20.0,
            "rule_version": _traffic_rule_version(config),
            "thresholds": deepcopy(config["traffic"]["qa"]),
            "density20_is_prefix": False,
            "spawn_collision_status": "pending_carla_hard_gate",
            "topology_status": "frozen_carla_waypoint_layout_pending_spawn_replay" if _uses_waypoint_topology(config) else "pending_carla_waypoint_and_conflict_exclusion_hard_gate",
        },
        "safety": {
            "carla_connection": "not_used",
            "image_generation": "not_run",
            "registry_write": False,
            "split_write": False,
            "old_p1_mutation": False,
            "p0_mutation": False,
            "rc": "not_entered",
            "full": "not_entered",
            "training": False,
            "commit": False,
        },
    }
    # selected_sites is deliberately an explicit four-item list in the schema;
    # this validation is kept here as a guard against accidental fixture edits.
    plan_schema = load_json(_schema("r4_static_plan.schema.json"))
    plan_schema["properties"]["selected_sites"] = {"type": "array", "minItems": 4, "maxItems": 4}
    validate_schema_instance(plan, plan_schema, "V3 R4 static plan")
    expected_bundle = {
        "schema_version": R4_LEDGER_SCHEMA_VERSION,
        "protocol_version": R4_PROTOCOL_VERSION,
        "stage": "p1_corrective_static",
        "pilot_evidence": True,
        "canonical_dataset": False,
        "expected_row_count": 24,
        "rows": rows,
    }
    traffic_bundle = {
        "schema_version": R4_TRAFFIC_LEDGER_SCHEMA_VERSION,
        "protocol_version": R4_PROTOCOL_VERSION,
        "stage": "p1_corrective_static",
        "density_unit": "vehicles_per_lane_km",
        "master_layouts": list(masters.values()),
        "expected_row_count": 24,
        "rows": traffic_rows,
    }
    validate_schema_instance(expected_bundle, load_json(_schema("r4_expected_ledger.schema.json")), "V3 R4 expected ledger")
    validate_schema_instance(traffic_bundle, load_json(_schema("r4_expected_traffic_ledger.schema.json")), "V3 R4 traffic ledger")
    protocol_decision = build_r4_protocol_decision(config, source_checksums)
    validate_schema_instance(protocol_decision, load_json(_schema("r4_protocol_decision.schema.json")), "V3 R4 protocol decision")
    return {
        "config": config,
        "recipes": recipes,
        "recipe_table": recipe_table,
        "masters": masters,
        "rows": rows,
        "traffic_rows": traffic_rows,
        "expected_bundle": expected_bundle,
        "traffic_bundle": traffic_bundle,
        "development_contract": development_contract,
        "protocol_decision": protocol_decision,
        "plan": plan,
        "source_sites": config["_source_sites"],
        "source_anchors": config["_source_anchors"],
    }


def build_r4_review_index(rows: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["anchor_id"]].append(row)
    # The first R4 corrective package used a fixed fixture list.  Closure
    # correction may replace anchors from the same frozen subset/structural
    # cell, so the review package must validate the frozen cardinality while
    # taking the actual anchor identities from the immutable plan.
    if len(groups) != 4:
        raise ConfigError(f"R4 review package must contain four corrective anchor cards, got {len(groups)}")
    bundles = []
    for anchor_id, group in sorted(groups.items()):
        first = group[0]
        bundles.append({
            "bundle_key": f"corrective_pilot:{anchor_id}",
            "card_path": f"cards/corrective_pilot_{anchor_id}.json",
            "map_id": first["map_id"],
            "site_id": first["site_id"],
            "anchor_id": anchor_id,
            "structural_cell": first["structural_cell"],
            "planned_row_count": len(group),
            "formal_core_bundle_size": 15,
            "formal_challenge_bundle_size": 18,
        })
    return {
        "schema_version": R4_REVIEW_INDEX_SCHEMA_VERSION,
        "review_rule_version": R4_ARTIFACT_RULE_VERSION,
        "run_id": run_id,
        "pilot_evidence": True,
        "canonical_dataset": False,
        "bundle_count": len(bundles),
        "bundles": bundles,
        "official_labels": list(OFFICIAL_LABELS),
    }


def build_r4_review_cards(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["anchor_id"]].append(row)
    cards: list[dict[str, Any]] = []
    for anchor_id, group in sorted(groups.items()):
        first = group[0]
        card = {
            "schema_version": R4_REVIEW_CARD_SCHEMA_VERSION,
            "review_rule_version": R4_ARTIFACT_RULE_VERSION,
            "bundle_key": f"corrective_pilot:{anchor_id}",
            "map_id": first["map_id"],
            "site_id": first["site_id"],
            "anchor_id": anchor_id,
            "structural_cell": first["structural_cell"],
            "rows": [
                {
                    "sample_id": row["sample_id"],
                    "recipe_id": row["observation_recipe_id"],
                    "pilot_kind": row["pilot_kind"],
                    "is_reference": row["is_reference"],
                    "requested_conditions": row["requested_conditions"],
                    "camera_geometry": row["camera_geometry"],
                    "recipe_expected_projection": row["recipe_expected_projection"],
                    "traffic": row["traffic_plan"],
                    "asset_paths": row["asset_paths"],
                    "asset_status": "pending_carla_capture",
                }
                for row in sorted(group, key=lambda item: item["sample_id"])
            ],
            "asset_contract": {
                "rgb": "samples/<sample_id>/rgb.jpg",
                "road_mask_visible_overlay": "samples/<sample_id>/road_mask_visible_overlay.jpg",
                "lane_polyline_projected_overlay": "samples/<sample_id>/lane_polyline_projected_overlay.jpg",
                "recipe": "rows[].requested_conditions and rows[].recipe_id",
                "camera_geometry": "rows[].camera_geometry",
                "traffic": "rows[].traffic",
            },
            "traffic_summary": {
                "density_levels": sorted({row["traffic_plan"]["requested_density"] for row in group}),
                "actor_counts": sorted({row["traffic_plan"]["planned_actor_count"] for row in group}),
                "master_layout_hashes": sorted({row["traffic_plan"]["master_layout_hash"] for row in group}),
                "qa_thresholds": group[0]["traffic_plan"]["qa"].get("same_lane_min_gap_m"),
            },
            "bundle_status": "corrective_targeted_not_formal_core_or_challenge_bundle",
            "incomplete_formal_bundle_disposition": "not_a_pass",
        }
        cards.append(card)
    return cards


__all__ = [
    "R4_ASSETS", "R4_CONFIG_PATH", "build_r4_plan", "build_r4_recipe_table", "build_r4_review_cards",
    "build_r4_review_index", "build_traffic_actor_plan", "load_r4_config", "traffic_actor_count",
    "validate_r4_execution_parameters", "validate_r4_formal_bundle",
]
