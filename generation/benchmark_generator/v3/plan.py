"""CARLA-free planner for the Benchmark V3 Benchmark-V3 full-regeneration protocol.

Benchmark-V3 (V3-PLAN-20260815-FULL.3) regenerates the complete 18,624-image
Full.2 shared-test-pool composition (Train 12,000 / Val 1,584 /
Test-shared 5,040, 53 sites / 126 anchors / 25 leakage groups, 41 logical
slots / 40 unique images per test anchor) without reusing any old asset.
Development yaw is restored to the original protocol range [-15, +15] deg
(RC-v2 had narrowed it to [-10, +10]); Development recipes are therefore
re-sampled under the Benchmark-V3 rule namespace instead of being inherited from
Full.1.  The LABELFIX.1 physical-marking merge is composited into generation:
the 24 user-confirmed sites are generated through
``reconstruct_site_physical_chains -> project_physical_chains`` (with the
special drop ref for site_a0973528659b89f0); every other site keeps the
historical project-then-dedup default path, exactly reproducing the Full.2
effective (LABELFIX.1) label semantics at generation time.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import random
from typing import Any

from benchmark_generator.config import ConfigError, ensure_path_within, load_json

from .constants import INPUTS_ROOT, OFFICIAL_LABELS, PACKAGE_ROOT, V3_CONFIG_ROOT, V3_SCHEMA_ROOT, output_root
from .ids import derive_seed, make_control_bundle_id, make_display_name, make_recipe_id, make_sample_id
from benchmark_generator.common.io import file_sha256
from benchmark_generator.v3.capture_plan import R4_ASSETS, _camera_geometry, _execution_condition, _recipe_expected_projection, load_capture_config
from .recipes import _conditions, validate_l18_balance
from .schema_support import validate_schema_instance
from benchmark_generator.v3.sampling import (
    CELL_TO_SCENE,
    FULL_1_PROTOCOL_VERSION,
    _development_forward_capacity_m,
    _folded_edge_rank,
    _generate_master_layout,
    _stratified,
    _stratified_power,
    _traffic_plan as _full_1_traffic_plan,
    _vehicle_pool_and_lane_change,
)
from benchmark_generator.v3.constants import PHYSICAL_SUBSETS
from benchmark_generator.v3.label_merge import RULE_VERSION as LABEL_FIX_RULE_VERSION

CONFIG_PATH = V3_CONFIG_ROOT / "benchmark_v3.json"
PLAN_ROOT = output_root() / "_work" / "plan"
CAPTURE_ROOT = output_root() / "_work" / "run"
REVIEW_ROOT = output_root() / "_work" / "review"

PUBLIC_PROTOCOL_VERSION = "Benchmark-V3-1.0"
# Frozen sample-identity string. It feeds ``make_sample_id`` and therefore the
# on-disk file names, so it must never change; only the user-visible
# ``protocol_version`` field carries the public version.
IDENTITY_PROTOCOL_VERSION = "V3-PLAN-20260815-FULL.3"
PROTOCOL_VERSION = PUBLIC_PROTOCOL_VERSION
# Frozen planning-rule namespace. It feeds recipe ids, world/sensor seeds and
# therefore every sample id; renaming it would renumber the dataset.
RULE_VERSION = "benchmark_v3_full_3_planning_0.1"
ASSETS = tuple(R4_ASSETS)
MAP_IDS = ("Town04_Opt", "Town05_Opt", "Town10HD_Opt")
SPLIT_DECISION = INPUTS_ROOT / "split_decision.json"
SITE_CELLS = INPUTS_ROOT / "site_structural_cells.json"
TEST_ANCHOR_CLASSIFICATION = INPUTS_ROOT / "test_anchor_classification.json"
INPUT_CHECKSUMS = INPUTS_ROOT / "input_checksums.json"

LABEL_RULE_DEFAULT = "lane_marking_projection_default_1.0"


def _package_path(value: "str | Path") -> Path:
    """Resolve a config path value against the package root."""

    path = Path(value)
    return path if path.is_absolute() else (PACKAGE_ROOT / path)

_OFAT_DEFAULT = {"height": 20.0, "pitch": -80.0, "yaw": 0.0, "traffic": 0.0, "weather": "dry", "illumination": 75.0}
OFAT_SPECS = [
    ("height", str(v), {**_OFAT_DEFAULT, "height": float(v)}) for v in (40, 60, 80, 100)
] + [
    ("pitch", str(v), {**_OFAT_DEFAULT, "pitch": float(v)}) for v in (-90, -70)
] + [
    ("relative_yaw", str(v), {**_OFAT_DEFAULT, "yaw": float(v)}) for v in (5, 10, 15)
] + [
    ("traffic_stress", str(v), {**_OFAT_DEFAULT, "traffic": float(v)}) for v in (20, 40)
] + [
    ("weather", v, {**_OFAT_DEFAULT, "weather": v}) for v in ("rain-wet", "fog")
] + [
    ("illumination", str(v), {**_OFAT_DEFAULT, "illumination": float(v)}) for v in (15, -5)
]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _verify_frozen_inputs() -> None:
    """Fail closed on the published planning inputs."""

    manifest = load_json(INPUT_CHECKSUMS)
    for rel, expected in sorted(manifest["files"].items()):
        path = INPUTS_ROOT / rel
        if not path.is_file():
            raise ConfigError(f"generation input missing: {path}")
        if file_sha256(path) != expected:
            raise ConfigError(f"generation input checksum drifted: {rel}")


def load_benchmark_v3_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config_path = ensure_path_within(Path(path), [V3_CONFIG_ROOT], "Benchmark-V3 config")
    config = load_json(config_path)
    validate_schema_instance(config, load_json(V3_SCHEMA_ROOT / "benchmark_v3_config.schema.json"), "Benchmark-V3 config")
    if config["official_labels"] != list(OFFICIAL_LABELS):
        raise ConfigError("Benchmark-V3 official labels drifted")
    config["_config_path"] = str(config_path.resolve())
    config["_config_sha256"] = file_sha256(config_path)

    _verify_frozen_inputs()
    config["_split_decision"] = load_json(SPLIT_DECISION)
    config["_site_cells"] = load_json(SITE_CELLS)["sites"]
    classification = load_json(TEST_ANCHOR_CLASSIFICATION)
    config["_test_anchors"] = {item["anchor_id"]: item for item in classification["anchors"]}
    config["_reference_conditions"] = classification["reference_conditions"]

    road_library_path = INPUTS_ROOT / "road_library.json"
    sites = {item["site_id"]: item for item in load_json(INPUTS_ROOT / "site_registry.json")["sites"]}
    anchors: dict[str, dict[str, Any]] = {}
    for item in load_json(INPUTS_ROOT / "anchor_registry.json")["anchors"]:
        anchors[item["anchor_id"]] = item
    config["_source_sites"] = sites
    config["_source_anchors"] = anchors
    config["_road_library"] = load_json(road_library_path)

    challenge = load_json(_package_path(config["challenge_l18_path"]))
    if challenge.get("protocol_version") != "V3-PLAN-20260731-RC-V2.0":
        raise ConfigError("Benchmark-V3 L18 source is not the frozen L18 mapping")
    validate_l18_balance(challenge)
    config["_challenge_l18"] = challenge

    base = load_capture_config(_package_path(config["engineering_base_config"]))
    base["_source_sites"] = sites
    base["_source_anchors"] = anchors
    base["_road_library"] = config["_road_library"]
    base["lifecycle"].update(config["lifecycle"])
    base["limits"].update(config["limits"])
    base["runtime"].update(config["runtime"])
    base["sharding"].update(config["sharding"])
    base["traffic"]["layout_rule_version"] = config["traffic"]["layout_rule_version"]
    base["traffic"]["master_layout_seed"] = int(config["seeds"]["traffic_master"])
    base["traffic"]["vehicle_blueprint_id"] = config["traffic"]["vehicle_pool"][0]
    base["traffic"]["qa"]["longitudinal_coverage_ratio_min"] = 0.0
    base["traffic"].pop("topology_layout_path", None)
    base["traffic"].pop("conflict_exclusion_rule_version", None)
    base["traffic"].pop("conflict_zone_radius_m", None)
    base.pop("_traffic_topology", None)
    base.pop("_traffic_topology_path", None)
    base["core"]["defaults"].update({"height_m": 20.0, "pitch_deg": -80.0, "yaw_relative_deg": 0.0, "traffic_density_vehicles_per_lane_km": 0.0, "weather_family": "dry", "sun_altitude_deg": 75.0})
    base["_config_path"] = config["_config_path"]
    base["_config_sha256"] = config["_config_sha256"]
    base["_label_rule"] = config["label_rule"]
    base["_confirmed_merge_site_ids"] = sorted(config["label_rule"]["confirmed_site_ids"])
    base["_special_drop_site_id"] = config["label_rule"]["special_drop_site_id"]
    base["_special_drop_source_ref"] = tuple(config["label_rule"]["special_drop_source_ref"])
    config["_execution_config"] = base

    _validate_label_rule(config, sites)
    return config


def _validate_label_rule(config: dict[str, Any], sites: dict[str, dict[str, Any]]) -> None:
    rule = config["label_rule"]
    confirmed = sorted(rule["confirmed_site_ids"])
    if len(confirmed) != 24 or len(set(confirmed)) != 24:
        raise ConfigError("Benchmark-V3 LABELFIX confirmed-site scope is not the frozen 24-site adjudication")
    for site_id in confirmed:
        if site_id not in sites:
            raise ConfigError(f"Benchmark-V3 confirmed site missing from registry: {site_id}")
    special = rule["special_drop_site_id"]
    if special not in confirmed:
        raise ConfigError("Benchmark-V3 special drop site is outside the confirmed scope")
    if tuple(rule["special_drop_source_ref"]) != (11, 0, -2, "right", "Solid"):
        raise ConfigError("Benchmark-V3 special drop ref drifted from LABELFIX.1")
    default_multi = sorted(rule["default_path_multi_span_sites"])
    if set(default_multi) & set(confirmed):
        raise ConfigError("Benchmark-V3 default-path exception sites overlap the confirmed scope")
    for site_id in default_multi:
        if site_id not in sites or len(sites[site_id].get("source_spans", [])) <= 1:
            raise ConfigError(f"Benchmark-V3 default-path exception site is not multi-span: {site_id}")


def _weather(config: dict[str, Any], family: str) -> dict[str, Any]:
    return {"family": family, **deepcopy(config["weather_representatives"][family])}


def _ofat_conditions(config: dict[str, Any], yaw_sign: int, values: dict[str, Any]) -> dict[str, Any]:
    conditions = _conditions(
        height_m=values["height"], pitch_deg=values["pitch"], yaw_relative_deg=float(yaw_sign * values["yaw"]),
        traffic_density=values["traffic"], weather=_weather(config, values["weather"]), sun_altitude_deg=values["illumination"],
    )
    conditions["lane_change_phase"] = 0.5 if values["traffic"] > 0 else None
    return conditions


def _l18_conditions(config: dict[str, Any], challenge: dict[str, Any], row: dict[str, Any], yaw_sign: int) -> dict[str, Any]:
    factors = challenge["factor_order"]
    values = {factor: challenge["factor_level_values"][factor][str(row["levels"][factor])] for factor in factors}
    conditions = _conditions(
        height_m=values["height"], pitch_deg=values["pitch"], yaw_relative_deg=float(values["relative_yaw"]) * yaw_sign,
        traffic_density=values["traffic_density"], weather=_weather(config, values["weather"]), sun_altitude_deg=values["illumination"],
    )
    conditions["lane_change_phase"] = 0.5 if float(values["traffic_density"]) > 0 else None
    return conditions


def _axis_conditions(config: dict[str, Any], axis_id: str, yaw_sign: int, anchor_rank: int, anchor_count: int) -> dict[str, Any]:
    axis_seed = int(config["seeds"]["challenge_axis"])
    defaults = {"height": 20.0, "pitch": -80.0, "yaw": 0.0, "traffic": 0.0, "weather": "dry", "illumination": 75.0}
    values = {
        "height": {**defaults, "height": _stratified(anchor_rank, anchor_count, 70, 90, axis_seed, "axis_height")},
        "nadir_view": {**defaults, "pitch": -90.0},
        "yaw": {**defaults, "yaw": float(15 * yaw_sign)},
        "traffic": {**defaults, "traffic": _stratified(anchor_rank, anchor_count, 30, 40, axis_seed, "axis_traffic")},
        "weather_rain": {**defaults, "weather": "rain-wet"},
        "weather_fog": {**defaults, "weather": "fog"},
        "illumination": {**defaults, "illumination": -5.0},
    }[axis_id]
    conditions = _conditions(
        height_m=values["height"], pitch_deg=values["pitch"], yaw_relative_deg=float(values["yaw"]),
        traffic_density=values["traffic"], weather=_weather(config, values["weather"]), sun_altitude_deg=values["illumination"],
    )
    conditions["lane_change_phase"] = 0.5 if values["traffic"] > 0 else None
    return conditions


def _new_development_recipes(config: dict[str, Any], anchor: dict[str, Any], subset: str, anchor_rank: int, anchor_count: int) -> list[dict[str, Any]]:
    """Benchmark-V3 dedicated stratified Development sampling (yaw restored to [-15,15])."""
    seed = int(config["seeds"]["development_train" if subset == "train" else "development_val"])
    ranges = config["development"]["ranges"]
    exponent = float(config["development"]["capacity_rank_exponent"])
    per_anchor = int(config["selection"]["development_recipes_per_anchor"])
    recipes = []
    zero_first = derive_seed(seed, anchor["anchor_id"], "traffic_half") % 2 == 0
    for ordinal in range(per_anchor):
        rank = anchor_rank * per_anchor + ordinal
        count = anchor_count * per_anchor
        traffic_zero = (ordinal % 2 == 0) == (not zero_first)
        half = ordinal // 2
        density = 0.0 if traffic_zero else _stratified(half, per_anchor // 2, *map(float, config["development"]["traffic_nonzero_range"]), seed, "traffic")
        phase = None if traffic_zero else _stratified(half, per_anchor // 2, *map(float, config["development"]["lane_change_phase_range"]), seed, "lane_change_phase")
        family = config["development"]["weather_families"][(ordinal + derive_seed(seed, "weather")) % 3]
        conditions = _conditions(
            height_m=_stratified_power(rank, count, *map(float, ranges["height_m"]), seed, "height", exponent),
            pitch_deg=_stratified_power(rank, count, *map(float, ranges["pitch_deg"]), seed, "pitch", exponent),
            yaw_relative_deg=_stratified(_folded_edge_rank(ordinal, per_anchor), per_anchor, *map(float, ranges["yaw_relative_deg"]), seed, "yaw"),
            traffic_density=density, weather=_weather(config, family),
            sun_altitude_deg=_stratified(ordinal, per_anchor, *map(float, ranges["sun_altitude_deg"]), seed, "sun"),
        )
        conditions["lane_change_phase"] = phase
        recipes.append(_recipe(
            {"subset": subset, "anchor_id": anchor["anchor_id"], "ordinal": ordinal, "conditions": conditions},
            "development_stratified", subset, conditions, is_reference=False,
            changed_factor="multi_factor_random", factor_level=None, sampling_seed=seed,
            development_traffic_branch="zero" if traffic_zero else "nonzero",
        ))
    return recipes


def _recipe(payload: dict[str, Any], family: str, subset: str, conditions: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        # "full3" is a frozen hash namespace: it feeds every sample id and file
        # name, so it must never be renamed.
        "observation_recipe_id": make_recipe_id("full3", {"rule_version": RULE_VERSION, **payload}),
        "recipe_family": family, "subset_scope": subset, "rule_version": RULE_VERSION,
        "requested_conditions": conditions, "factor_levels": fields.pop("factor_levels", {}),
        "is_reference": fields.pop("is_reference", False), "changed_factor": fields.pop("changed_factor", None),
        "factor_level": fields.pop("factor_level", None), "challenge_row_id": fields.pop("challenge_row_id", None),
        "challenge_level_tuple": fields.pop("challenge_level_tuple", None), "challenge_axis_id": fields.pop("challenge_axis_id", None),
        "test_view": fields.pop("test_view", None), "independent_capture_semantics": fields.pop("independent_capture_semantics", False),
        **fields,
    }


def _benchmark_v3_row(config: dict[str, Any], site: dict[str, Any], anchor: dict[str, Any], recipe: dict[str, Any], yaw_sign: int | None, traffic_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    condition = _execution_condition(recipe["requested_conditions"], config["_execution_config"])
    world_seed = derive_seed(int(config["seeds"]["traffic_master"]), RULE_VERSION, "world", anchor["anchor_id"], recipe["observation_recipe_id"])
    sensor_seed = derive_seed(int(config["seeds"]["review"]), RULE_VERSION, "sensor", anchor["anchor_id"], recipe["observation_recipe_id"])
    magnitude = abs(float(condition["yaw_relative_deg"]))
    sign = 0 if magnitude == 0 else (1 if float(condition["yaw_relative_deg"]) > 0 else -1)
    sample_id = make_sample_id(site["map_id"], site["site_id"], anchor["anchor_id"], recipe["observation_recipe_id"], world_seed, sensor_seed, IDENTITY_PROTOCOL_VERSION)
    is_test = site["subset"] == "test_shared"
    reference_sample_id = None
    control_bundle = make_control_bundle_id(anchor["anchor_id"]) if is_test else None
    merge_rule = site["site_id"] in config["_execution_config"]["_confirmed_merge_site_ids"]
    return {
        "protocol_version": PROTOCOL_VERSION, "sample_id": sample_id,
        "display_name": make_display_name(site["map_id"], site["structural_cell"], site["site_id"], anchor["anchor_id"], recipe["recipe_family"], sample_id),
        "physical_subset": site["subset"], "pilot_kind": "full_3_full_regeneration", "logical_test_view": "test_core" if is_test and recipe["test_view"] in ("core_scene", "core_observation", "core_axis") else ("test_challenge" if is_test and recipe["test_view"] == "challenge_l18" else None),
        "test_view": recipe["test_view"], "test_shared": is_test,
        "map_id": site["map_id"], "site_id": site["site_id"], "anchor_id": anchor["anchor_id"], "leakage_group_id": site["leakage_group_id"],
        "structural_cell": site["structural_cell"], "scene": CELL_TO_SCENE[site["structural_cell"]],
        "road_layout": site["road_layout"], "lane_configuration": site["lane_configuration"],
        "observation_recipe_id": recipe["observation_recipe_id"], "recipe_family": recipe["recipe_family"],
        "requested_conditions": condition, "factor_levels": deepcopy(recipe["factor_levels"]),
        "is_reference": bool(recipe["is_reference"]), "reference_sample_id": reference_sample_id,
        "control_bundle_id": control_bundle,
        "changed_factor": recipe["changed_factor"], "factor_level": recipe["factor_level"],
        "challenge_row_id": recipe.get("challenge_row_id"), "challenge_level_tuple": recipe.get("challenge_level_tuple"),
        "challenge_axis_id": recipe.get("challenge_axis_id"),
        "yaw_magnitude_deg": magnitude, "yaw_sign": sign, "anchor_yaw_sign": yaw_sign,
        "world_seed": int(world_seed), "sensor_seed": int(sensor_seed), "camera_geometry": _camera_geometry(anchor, condition),
        "recipe_expected_projection": _recipe_expected_projection(site, anchor, {**recipe, "requested_conditions": condition}, config["_execution_config"]),
        "development_qa_tier": None, "official_labels": list(OFFICIAL_LABELS), "qa_policy_version": config["_execution_config"]["qa"]["policy_version"],
        "qa_status": "pending_carla",
        "generation_status": "new_capture",
        "label_rule_version": LABEL_FIX_RULE_VERSION if merge_rule else LABEL_RULE_DEFAULT,
        "v3_physical_marking_merge": merge_rule,
        "independent_capture_semantics": bool(recipe["independent_capture_semantics"]),
        "canonical_dataset": True, "full_3_status": "planned_pending_execution",
        "traffic_plan": traffic_plan,
        "asset_paths": {name: f"canonical/{sample_id}/{name}" for name in ASSETS},
        "source_uri": f"full-3-ledger://{config['release_id']}/{sample_id}",
    }


def _site_table(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    split = config["_split_decision"]
    # The Finalization-v2 site registry predates the scene reclassification, so
    # the frozen per-site cell mapping (not the registry field) is authoritative.
    cell_by_site = config["_site_cells"]
    table = {}
    for role, bucket in (("development_train", "train"), ("development_val", "val"), ("core_test", "test_shared"), ("challenge_test", "test_shared")):
        for site_id in split["site_assignment"][role]:
            site = config["_source_sites"][site_id]
            table[site_id] = {
                "site_id": site_id, "subset": bucket, "map_id": site["map_id"],
                "structural_cell": cell_by_site.get(site_id, site["structural_cell"]),
                "scene": CELL_TO_SCENE[cell_by_site.get(site_id, site["structural_cell"])],
                "leakage_group_id": site["leakage_group_id"], "anchor_ids": sorted(site.get("anchor_ids", [])),
            }
    return table


def build_plan(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = load_benchmark_v3_config(config_path)
    site_table = _site_table(config)
    f1_yaw = {anchor_id: item["yaw_sign"] for anchor_id, item in config["_test_anchors"].items()}
    challenge = config["_challenge_l18"]
    rows: list[dict[str, Any]] = []
    masters: dict[str, dict[str, Any]] = {}

    def master_for(site: dict[str, Any], anchor: dict[str, Any]) -> dict[str, Any]:
        if anchor["anchor_id"] not in masters:
            masters[anchor["anchor_id"]] = _vehicle_pool_and_lane_change(
                config, site, anchor, _generate_master_layout(site, anchor, config["_execution_config"]),
            )
        return masters[anchor["anchor_id"]]

    def emit(site: dict[str, Any], anchor: dict[str, Any], recipe: dict[str, Any], yaw_sign: int | None) -> None:
        condition = _execution_condition(recipe["requested_conditions"], config["_execution_config"])
        # ``_traffic_plan`` keys the per-row scripted lane-change phase on
        # ``subset.startswith("development_")``; Benchmark-V3 physical subsets are
        # train/val, so pass the protocol development role for that check.
        traffic_subset = {"train": "development_train", "val": "development_val"}.get(site["subset"], site["subset"])
        traffic_plan = _full_1_traffic_plan(config, master_for(site, anchor), {**recipe, "requested_conditions": condition}, traffic_subset)
        rows.append(_benchmark_v3_row(config, site, anchor, recipe, yaw_sign, traffic_plan))

    dev_ranks: dict[str, dict[str, int]] = {}
    for subset in ("train", "val"):
        anchors = []
        for entry in site_table.values():
            if entry["subset"] != subset:
                continue
            for aid in entry["anchor_ids"]:
                anchors.append((entry, config["_source_anchors"][aid]))
        anchors.sort(key=lambda pair: (_development_forward_capacity_m(config["_source_sites"][pair[0]["site_id"]], pair[1]), pair[1]["anchor_id"]))
        dev_ranks[subset] = {a["anchor_id"]: rank for rank, (_, a) in enumerate(anchors)}

    for subset in ("train", "val"):
        for site_id, entry in sorted(site_table.items(), key=lambda kv: kv[0]):
            if entry["subset"] != subset:
                continue
            site = deepcopy(config["_source_sites"][site_id])
            site["subset"] = entry["subset"]
            site["structural_cell"] = entry["structural_cell"]
            for aid in entry["anchor_ids"]:
                anchor = config["_source_anchors"][aid]
                for recipe in _new_development_recipes(config, anchor, subset, dev_ranks[subset][aid], len(dev_ranks[subset])):
                    emit(site, anchor, recipe, None)

    for site_id, entry in sorted(site_table.items(), key=lambda kv: kv[0]):
        if entry["subset"] != "test_shared":
            continue
        site = deepcopy(config["_source_sites"][site_id])
        site["subset"] = entry["subset"]
        site["structural_cell"] = entry["structural_cell"]
        test_anchors = entry["anchor_ids"]
        for rank, aid in enumerate(sorted(test_anchors)):
            anchor = config["_source_anchors"][aid]
            anchor_class = config["_test_anchors"].get(aid)
            if anchor_class is None:
                raise ConfigError(f"test anchor missing from the frozen classification: {aid}")
            yaw_sign = anchor_class["yaw_sign"]
            is_orig_core = anchor_class["is_orig_core"]
            # reference (aliases L18 R01)
            cond = deepcopy(config["_reference_conditions"])
            cond["yaw_relative_deg"] = anchor_class["yaw_relative_deg"]
            recipe = _recipe(
                {"anchor_yaw_sign": yaw_sign, "slot": "reference", "conditions": cond},
                "shared_reference", "test_shared", cond, is_reference=True,
                changed_factor="reference", factor_level="default", challenge_row_id="RCV2_L18_R01",
                test_view=["core_scene", "challenge_l18"],
            )
            emit(site, anchor, recipe, yaw_sign)
            reference_id = rows[-1]["sample_id"]
            for ordinal, (factor, level, values) in enumerate(OFAT_SPECS):
                conditions = _ofat_conditions(config, yaw_sign, values)
                emit(site, anchor, _recipe(
                    {"anchor_yaw_sign": yaw_sign, "factor": factor, "level": level, "conditions": conditions},
                    "ofat", "test_shared", conditions,
                    factor_levels={factor: level}, is_reference=False, changed_factor=factor, factor_level=level,
                    test_view=["core_scene", "core_observation"], independent_capture_semantics=False,
                ), yaw_sign)
            for ordinal, lrow in enumerate(challenge["rows"]):
                if lrow["challenge_row_id"] == "RCV2_L18_R01":
                    continue
                conditions = _l18_conditions(config, challenge, lrow, yaw_sign)
                level_tuple = [int(lrow["levels"][f]) for f in challenge["factor_order"]]
                emit(site, anchor, _recipe(
                    {"anchor_yaw_sign": yaw_sign, "row_id": lrow["challenge_row_id"], "conditions": conditions},
                    "l18", "test_shared", conditions,
                    factor_levels={f: f"L{lrow['levels'][f]}" for f in challenge["factor_order"]},
                    is_reference=False, changed_factor="multi_factor", factor_level=None,
                    challenge_row_id=lrow["challenge_row_id"], challenge_level_tuple=level_tuple,
                    test_view=["core_scene", "challenge_l18"],
                ), yaw_sign)
            for axis_id in config["challenge_axis"]["axis_ids"]:
                conditions = _axis_conditions(config, axis_id, yaw_sign, rank, len(test_anchors))
                emit(site, anchor, _recipe(
                    {"anchor_yaw_sign": yaw_sign, "axis_id": axis_id, "conditions": conditions},
                    "axis", "test_shared", conditions, is_reference=False,
                    changed_factor=axis_id, factor_level="axis", challenge_axis_id=axis_id,
                    test_view=["core_scene", "core_axis"], independent_capture_semantics=True,
                ), yaw_sign)
            for row in rows:
                if row["anchor_id"] == aid and row["test_shared"]:
                    row["reference_sample_id"] = reference_id

    rows.sort(key=lambda r: (r["physical_subset"], r["map_id"], r["site_id"], r["anchor_id"], r["sample_id"]))
    plan = _validate_plan(config, rows, site_table)
    subset_counts = {}
    for subset in PHYSICAL_SUBSETS:
        sub = [r for r in rows if r["physical_subset"] == subset]
        subset_counts[subset] = {"images": len(sub), "anchors": len({r["anchor_id"] for r in sub}), "sites": len({r["site_id"] for r in sub}), "leakage_groups": len({r["leakage_group_id"] for r in sub})}
    return {
        "config": config, "execution_config": config["_execution_config"], "site_table": site_table,
        "rows": rows, "masters": masters, "plan": plan,
        "new_count": len(rows), "reuse_count": 0,
    }


def _validate_plan(config: dict[str, Any], rows: list[dict[str, Any]], site_table: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != 18624:
        raise ConfigError(f"Benchmark-V3 total drifted: {len(rows)}")
    if len({r["sample_id"] for r in rows}) != len(rows):
        raise ConfigError("Benchmark-V3 sample ID collision")
    train = [r for r in rows if r["physical_subset"] == "train"]
    val = [r for r in rows if r["physical_subset"] == "val"]
    test = [r for r in rows if r["physical_subset"] == "test_shared"]
    if len(train) != 12000 or len(val) != 1584 or len(test) != 5040:
        raise ConfigError(f"Benchmark-V3 subset counts drifted: {len(train)}/{len(val)}/{len(test)}")
    if any(r["generation_status"] != "new_capture" for r in rows):
        raise ConfigError("Benchmark-V3 must regenerate every sample (no reuse)")
    if any(r["protocol_version"] != PROTOCOL_VERSION for r in rows):
        raise ConfigError("Benchmark-V3 protocol version drifted")
    for subset, rows_ in (("train", train), ("val", val)):
        zero = sum(float(r["requested_conditions"]["traffic_density_vehicles_per_lane_km"]) == 0 for r in rows_)
        if zero * 2 != len(rows_):
            raise ConfigError(f"Benchmark-V3 {subset} traffic 50/50 failed")
        yaws = [float(r["requested_conditions"]["yaw_relative_deg"]) for r in rows_]
        if min(yaws) > -14.0 or max(yaws) < 14.0:
            raise ConfigError(f"Benchmark-V3 {subset} development yaw coverage drifted: [{min(yaws):.4f}, {max(yaws):.4f}]")
        if any(abs(y) > 15.0 for y in yaws):
            raise ConfigError(f"Benchmark-V3 {subset} development yaw exceeds [-15,15]")
        per_anchor = Counter(r["anchor_id"] for r in rows_)
        if any(v != int(config["selection"]["development_recipes_per_anchor"]) for v in per_anchor.values()):
            raise ConfigError(f"Benchmark-V3 {subset} per-anchor development rows drifted")
    by_anchor = defaultdict(list)
    for r in test:
        by_anchor[r["anchor_id"]].append(r)
    for aid, anchor_rows in by_anchor.items():
        if len(anchor_rows) != 40 or len({r["sample_id"] for r in anchor_rows}) != 40:
            raise ConfigError(f"Benchmark-V3 test anchor union drifted: {aid} {len(anchor_rows)}")
        refs = [r for r in anchor_rows if r["is_reference"]]
        if len(refs) != 1:
            raise ConfigError(f"Benchmark-V3 anchor reference count: {aid} {len(refs)}")
        if sum(r["challenge_row_id"] == "RCV2_L18_R01" for r in anchor_rows) != 1:
            raise ConfigError(f"Benchmark-V3 L18 R01 alias missing: {aid}")
    groups = defaultdict(set)
    for r in rows:
        groups[r["leakage_group_id"]].add(r["physical_subset"])
    cross = {g: s for g, s in groups.items() if len(s) > 1}
    if cross:
        raise ConfigError(f"Benchmark-V3 leakage crosses subset: {cross}")

    def count(view: str) -> int:
        return sum(1 for r in test if view in (r["test_view"] or ()))
    views = {"core_scene": count("core_scene"), "core_observation": count("core_observation"), "core_axis": count("core_axis"), "challenge_l18": count("challenge_l18")}
    if views != {"core_scene": 5040, "core_observation": 1890, "core_axis": 882, "challenge_l18": 2268}:
        raise ConfigError(f"Benchmark-V3 view counts drifted: {views}")
    axis_ids = {r["sample_id"] for r in test if r["challenge_axis_id"]}
    ofat_ids = {r["sample_id"] for r in test if r["changed_factor"] and r["changed_factor"] != "reference" and not r["challenge_row_id"] and not r["challenge_axis_id"]}
    if axis_ids & ofat_ids:
        raise ConfigError("Benchmark-V3 Axis/OFAT sample ID overlap")
    scene_views = {s: {"core_scene": 0, "core_observation": 0, "core_axis": 0, "challenge_l18": 0} for s in CELL_TO_SCENE.values()}
    for r in test:
        for v in (r["test_view"] or ()):
            scene_views[CELL_TO_SCENE[r["structural_cell"]]][v] += 1
    expected_scene = {
        "straight_1_4": (320, 120, 56, 144), "straight_5_8": (1320, 495, 231, 594),
        "curve_1_4": (1040, 390, 182, 468), "curve_5_8": (760, 285, 133, 342),
        "intersection": (640, 240, 112, 288), "ramp": (960, 360, 168, 432),
    }
    for scene, (cs, co, ca, cl) in expected_scene.items():
        actual = scene_views[scene]
        if (actual["core_scene"], actual["core_observation"], actual["core_axis"], actual["challenge_l18"]) != (cs, co, ca, cl):
            raise ConfigError(f"Benchmark-V3 scene view drifted {scene}: {actual}")
    if len({r["sample_id"] for r in rows}) != len(rows):
        raise ConfigError("Benchmark-V3 sample IDs are not unique")
    # label-rule scope: merge exactly on the confirmed set
    confirmed = set(config["label_rule"]["confirmed_site_ids"])
    merge_sites = {r["site_id"] for r in rows if r["v3_physical_marking_merge"]}
    if merge_sites != confirmed:
        raise ConfigError(f"Benchmark-V3 merge scope drifted: {sorted(merge_sites ^ confirmed)[:5]}")
    if any(r["label_rule_version"] == LABEL_FIX_RULE_VERSION and not r["v3_physical_marking_merge"] for r in rows):
        raise ConfigError("Benchmark-V3 merge rule version applied without merge flag")
    test_sites = {r["site_id"] for r in test}
    if len(test_sites) != 53 or len(by_anchor) != 126 or len({r["leakage_group_id"] for r in test}) != 25:
        raise ConfigError("Benchmark-V3 Test-shared site/anchor/group inventory drifted")
    return {
        "status": "pass", "protocol_version": PROTOCOL_VERSION, "quantity": len(rows), "subset_counts": {s: len([r for r in rows if r["physical_subset"] == s]) for s in PHYSICAL_SUBSETS},
        "view_counts": views, "test_union": {"sites": len(test_sites), "anchors": len(by_anchor), "groups": len({r["leakage_group_id"] for r in test})},
        "traffic_50_50": "pass", "leakage_isolation": "pass", "axis_ofat_disjoint": "pass",
        "new_capture": len(rows), "reuse": 0,
        "development_yaw_range": [-15.0, 15.0],
        "merge_site_count": len(merge_sites), "default_path_site_count": len({r["site_id"] for r in rows} - confirmed),
    }


__all__ = [
    "ASSETS", "CONFIG_PATH", "CAPTURE_ROOT", "PROTOCOL_VERSION",
    "REVIEW_ROOT", "PLAN_ROOT", "LABEL_RULE_DEFAULT", "PHYSICAL_SUBSETS",
    "build_plan", "load_benchmark_v3_config",
]
