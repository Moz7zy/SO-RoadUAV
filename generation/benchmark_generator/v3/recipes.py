"""Deterministic Development, Core (13/15), and Challenge-L18 recipes.

The historical Core-13 path remains the default for old S0/P1 configs.  R4
opts in to the same planner with an explicit five-level Height contract, so
old immutable pilot artifacts keep their original recipe IDs and rule names.
"""

from __future__ import annotations

from copy import deepcopy
import random
from typing import Any

from benchmark_generator.config import ConfigError
from benchmark_generator.common.identity import canonical_json

from .constants import (
    CHALLENGE_RECIPE_RULE_VERSION,
    CORE_RECIPE_RULE_VERSION,
    DEVELOPMENT_RECIPE_RULE_VERSION,
    OBSERVATION_FACTORS,
    RECIPE_REGISTRY_SCHEMA_VERSION,
)
from .ids import derive_seed, make_recipe_id


def build_recipe_registry(config: dict[str, Any], challenge: dict[str, Any]) -> dict[str, Any]:
    development_train = build_development_recipes(config, "development_train")
    development_val = build_development_recipes(config, "development_val")
    core = build_core_recipes(config)
    challenge_recipes = build_challenge_recipes(challenge)
    all_recipes = development_train + development_val + core + challenge_recipes
    recipe_ids = [item["observation_recipe_id"] for item in all_recipes]
    if len(recipe_ids) != len(set(recipe_ids)):
        raise ConfigError("V3 observation_recipe_id collision")
    _validate_recipe_separation(development_train + development_val, core, challenge_recipes)
    return {
        "schema_version": RECIPE_REGISTRY_SCHEMA_VERSION,
        "protocol_version": config["protocol_version"],
        "registry_status": "s0_dry_run_fixture_not_frozen_for_release",
        "recipes": all_recipes,
        "summary": {
            "development_train": len(development_train),
            "development_val": len(development_val),
            "core_13": len(core),
            "challenge_l18": len(challenge_recipes),
        },
    }


def build_development_recipes(config: dict[str, Any], subset: str) -> list[dict[str, Any]]:
    development = config["development"]
    count = int(development["fixture_budgets"][subset])
    seed = int(development["seeds"][subset])
    ranges = development["ranges"]
    continuous = {
        "height_m": ranges["height_m"],
        "pitch_deg": ranges["pitch_deg"],
        "yaw_relative_deg": ranges["yaw_relative_deg"],
        "traffic_density_vehicles_per_lane_km": ranges["traffic_density_vehicles_per_lane_km"],
        "sun_altitude_deg": ranges["sun_altitude_deg"],
    }
    rule_version = str(development.get("algorithm_version", DEVELOPMENT_RECIPE_RULE_VERSION))
    values = {
        name: _stratified_values(seed, subset, name, bounds, count, rule_version=rule_version)
        for name, bounds in continuous.items()
    }
    weather_families = _stratified_categories(
        seed, subset, list(ranges["weather_families"]), count, rule_version=rule_version
    )
    recipes = []
    for index in range(count):
        recipe_seed = derive_seed(seed, rule_version, subset, index)
        conditions = _conditions(
            height_m=values["height_m"][index],
            pitch_deg=values["pitch_deg"][index],
            yaw_relative_deg=values["yaw_relative_deg"][index],
            traffic_density=values["traffic_density_vehicles_per_lane_km"][index],
            weather=_sample_weather(ranges, weather_families[index], recipe_seed, rule_version=rule_version),
            sun_altitude_deg=values["sun_altitude_deg"][index],
        )
        payload = {
            "rule_version": rule_version,
            "subset": subset,
            "ordinal": index,
            "sampling_seed": recipe_seed,
            "requested_conditions": conditions,
        }
        recipes.append({
            "observation_recipe_id": make_recipe_id("development", payload),
            "recipe_family": "development_stratified_random",
            "subset_scope": subset,
            "rule_version": rule_version,
            "ordinal": index,
            "sampling_seed": recipe_seed,
            "requested_conditions": conditions,
            "factor_levels": {name: None for name in OBSERVATION_FACTORS},
            "is_reference": False,
            "changed_factor": None,
            "factor_level": None,
            "challenge_row_id": None,
            "challenge_level_tuple": None,
        })
    return recipes


def build_core_recipes(config: dict[str, Any]) -> list[dict[str, Any]]:
    core = config["core"]
    defaults = core["defaults"]
    rule_version = str(core.get("recipe_rule_version", CORE_RECIPE_RULE_VERSION))
    recipe_family = str(
        core.get(
            "recipe_family",
            "core_15_condition" if int(core.get("bundle_size", 13)) == 15 else "core_13_condition",
        )
    )
    recipes = []
    recipes.append(
        _core_recipe(
            "reference", "default", defaults, defaults, is_reference=True,
            rule_version=rule_version, recipe_family=recipe_family,
        )
    )
    factor_specs = [
        ("height", "height_m", core["levels"]["height_m"]),
        ("pitch", "pitch_deg", core["levels"]["pitch_deg"]),
        ("relative_yaw", "yaw_relative_deg", core["levels"]["yaw_relative_deg"]),
        (
            "traffic_density",
            "traffic_density_vehicles_per_lane_km",
            core["levels"]["traffic_density_vehicles_per_lane_km"],
        ),
        ("weather", "weather_family", core["levels"]["weather_family"]),
        ("illumination", "sun_altitude_deg", core["levels"]["sun_altitude_deg"]),
    ]
    for factor, field, levels in factor_specs:
        default_value = defaults[field]
        for level_index, value in enumerate(levels, start=1):
            if value == default_value:
                continue
            changed = deepcopy(defaults)
            changed[field] = value
            level_name = f"L{level_index}"
            recipes.append(
                _core_recipe(
                    factor, level_name, defaults, changed, is_reference=False,
                    rule_version=rule_version, recipe_family=recipe_family,
                )
            )
    expected_count = 1 + sum(
        sum(value != defaults[field] for value in levels)
        for _, field, levels in factor_specs
    )
    configured_count = int(core.get("bundle_size", expected_count))
    if expected_count != configured_count or len(recipes) != configured_count:
        raise ConfigError(
            f"V3 Core recipe bundle count drifted: expected {configured_count}, got {len(recipes)}"
        )
    return recipes


def _core_recipe(
    factor: str,
    factor_level: str,
    defaults: dict[str, Any],
    values: dict[str, Any],
    *,
    is_reference: bool,
    rule_version: str = CORE_RECIPE_RULE_VERSION,
    recipe_family: str = "core_13_condition",
) -> dict[str, Any]:
    weather = _frozen_core_weather(values["weather_family"])
    conditions = _conditions(
        height_m=values["height_m"],
        pitch_deg=values["pitch_deg"],
        yaw_relative_deg=values["yaw_relative_deg"],
        traffic_density=values["traffic_density_vehicles_per_lane_km"],
        weather=weather,
        sun_altitude_deg=values["sun_altitude_deg"],
    )
    levels = {
        "height": _core_level_name("height", defaults["height_m"]),
        "pitch": _core_level_name("pitch", defaults["pitch_deg"]),
        "relative_yaw": _core_level_name("relative_yaw", defaults["yaw_relative_deg"]),
        "traffic_density": _core_level_name(
            "traffic_density", defaults["traffic_density_vehicles_per_lane_km"]
        ),
        "weather": _core_level_name("weather", defaults["weather_family"]),
        "illumination": _core_level_name("illumination", defaults["sun_altitude_deg"]),
    }
    if not is_reference:
        levels[factor] = _core_level_name(factor, values[_core_condition_field(factor)])
    payload = {
        "rule_version": rule_version,
        "changed_factor": factor,
        "factor_level": factor_level,
        "requested_conditions": conditions,
    }
    return {
        "observation_recipe_id": make_recipe_id("core", payload),
        "recipe_family": recipe_family,
        "subset_scope": "core_test",
        "rule_version": rule_version,
        "ordinal": 0 if is_reference else None,
        "sampling_seed": None,
        "requested_conditions": conditions,
        "factor_levels": levels,
        "is_reference": is_reference,
        "changed_factor": "reference" if is_reference else factor,
        "factor_level": None if is_reference else levels[factor],
        "challenge_row_id": None,
        "challenge_level_tuple": None,
    }


def build_challenge_recipes(challenge: dict[str, Any]) -> list[dict[str, Any]]:
    factors = list(OBSERVATION_FACTORS)
    level_values = challenge["factor_level_values"]
    rule_version = str(challenge.get("recipe_rule_version", CHALLENGE_RECIPE_RULE_VERSION))
    recipes = []
    for ordinal, row in enumerate(challenge["rows"]):
        levels = {factor: f"L{int(row['levels'][factor])}" for factor in factors}
        values = {factor: level_values[factor][str(row["levels"][factor])] for factor in factors}
        conditions = _conditions(
            height_m=values["height"],
            pitch_deg=values["pitch"],
            yaw_relative_deg=values["relative_yaw"],
            traffic_density=values["traffic_density"],
            weather=_frozen_core_weather(values["weather"]),
            sun_altitude_deg=values["illumination"],
        )
        level_tuple = [int(row["levels"][factor]) for factor in factors]
        payload = {
            "rule_version": rule_version,
            "challenge_row_id": row["challenge_row_id"],
            "level_tuple": level_tuple,
            "requested_conditions": conditions,
        }
        recipes.append({
            "observation_recipe_id": make_recipe_id("challenge", payload),
            "recipe_family": "challenge_l18",
            "subset_scope": "challenge_test",
            "rule_version": rule_version,
            "ordinal": ordinal,
            "sampling_seed": None,
            "requested_conditions": conditions,
            "factor_levels": levels,
            "is_reference": ordinal == 0,
            "changed_factor": "reference" if ordinal == 0 else "multi_factor",
            "factor_level": "default" if ordinal == 0 else None,
            "challenge_row_id": row["challenge_row_id"],
            "challenge_level_tuple": level_tuple,
        })
    if len(recipes) != 18 or sum(item["is_reference"] for item in recipes) != 1:
        raise ConfigError("V3 Challenge must contain exactly 18 rows with one default reference")
    return recipes


def validate_l18_balance(challenge: dict[str, Any]) -> None:
    factors = list(OBSERVATION_FACTORS)
    rows = challenge.get("rows", [])
    if len(rows) != 18:
        raise ConfigError("V3 Challenge L18 must contain exactly 18 rows")
    row_ids = [str(row["challenge_row_id"]) for row in rows]
    if len(row_ids) != len(set(row_ids)):
        raise ConfigError("V3 Challenge L18 challenge_row_id values must be unique")
    for factor in factors:
        counts = {level: 0 for level in (1, 2, 3)}
        for row in rows:
            counts[int(row["levels"][factor])] += 1
        if counts != {1: 6, 2: 6, 3: 6}:
            raise ConfigError(f"V3 Challenge L18 factor is unbalanced: {factor}={counts}")
    for left_index, left in enumerate(factors):
        for right in factors[left_index + 1:]:
            pairs = {(a, b): 0 for a in (1, 2, 3) for b in (1, 2, 3)}
            for row in rows:
                pairs[(int(row["levels"][left]), int(row["levels"][right]))] += 1
            if set(pairs.values()) != {2}:
                raise ConfigError(f"V3 Challenge L18 pair is not balanced: {left}/{right}")
    first_tuple = [int(rows[0]["levels"][factor]) for factor in factors]
    if first_tuple != [1] * len(factors):
        raise ConfigError("V3 Challenge L18 first row must be the default reference")


def _stratified_values(
    seed: int,
    subset: str,
    name: str,
    bounds: list[float],
    count: int,
    *,
    rule_version: str = DEVELOPMENT_RECIPE_RULE_VERSION,
) -> list[float]:
    low, high = [float(item) for item in bounds]
    order = list(range(count))
    random.Random(derive_seed(seed, rule_version, subset, name, "order")).shuffle(order)
    jitter_rng = random.Random(derive_seed(seed, rule_version, subset, name, "jitter"))
    return [round(low + (high - low) * ((order[index] + jitter_rng.random()) / count), 6) for index in range(count)]


def _stratified_categories(
    seed: int,
    subset: str,
    categories: list[str],
    count: int,
    *,
    rule_version: str = DEVELOPMENT_RECIPE_RULE_VERSION,
) -> list[str]:
    values = [categories[index % len(categories)] for index in range(count)]
    random.Random(derive_seed(seed, rule_version, subset, "weather_order")).shuffle(values)
    return values


def _sample_weather(
    ranges: dict[str, Any],
    family: str,
    seed: int,
    *,
    rule_version: str = DEVELOPMENT_RECIPE_RULE_VERSION,
) -> dict[str, Any]:
    spec = ranges["weather_parameters"][family]
    rng = random.Random(derive_seed(seed, rule_version, "weather", family))
    result: dict[str, Any] = {"family": family}
    for name, bounds in spec.items():
        if bounds is None:
            result[name] = None
        elif isinstance(bounds, list):
            result[name] = round(rng.uniform(float(bounds[0]), float(bounds[1])), 6)
        else:
            result[name] = float(bounds)
    return result


def _frozen_core_weather(family: str) -> dict[str, Any]:
    values = {
        "dry": {"cloudiness": 10.0, "precipitation": 0.0, "wetness": 0.0, "fog_density": 0.0, "fog_distance_m": None},
        "rain-wet": {"cloudiness": 80.0, "precipitation": 60.0, "wetness": 80.0, "fog_density": 0.0, "fog_distance_m": None},
        "fog": {"cloudiness": 50.0, "precipitation": 0.0, "wetness": 0.0, "fog_density": 40.0, "fog_distance_m": 20.0},
    }
    if family not in values:
        raise ConfigError(f"unknown V3 weather family: {family}")
    return {"family": family, **values[family]}


def _conditions(
    *,
    height_m: float,
    pitch_deg: float,
    yaw_relative_deg: float,
    traffic_density: float,
    weather: dict[str, Any],
    sun_altitude_deg: float,
) -> dict[str, Any]:
    return {
        "height_m": round(float(height_m), 6),
        "pitch_deg": round(float(pitch_deg), 6),
        "yaw_relative_deg": round(float(yaw_relative_deg), 6),
        "traffic_density_vehicles_per_lane_km": round(float(traffic_density), 6),
        "weather": weather,
        "sun_altitude_deg": round(float(sun_altitude_deg), 6),
        "sun_azimuth_relative_deg": 90.0,
    }


def _condition_signature(recipe: dict[str, Any]) -> str:
    return canonical_json(recipe["requested_conditions"])


def _validate_recipe_separation(
    development: list[dict[str, Any]],
    core: list[dict[str, Any]],
    challenge: list[dict[str, Any]],
) -> None:
    challenge_ids = {item["observation_recipe_id"] for item in challenge}
    other_ids = {item["observation_recipe_id"] for item in development + core}
    if challenge_ids & other_ids:
        raise ConfigError("V3 Challenge recipe IDs must not appear in Development/Core")
    frozen_signatures = {_condition_signature(item) for item in core + challenge}
    collisions = [item["observation_recipe_id"] for item in development if _condition_signature(item) in frozen_signatures]
    if collisions:
        raise ConfigError(f"V3 Development recipe matches a frozen Core/Challenge condition: {collisions}")


def _core_condition_field(factor: str) -> str:
    return {
        "height": "height_m",
        "pitch": "pitch_deg",
        "relative_yaw": "yaw_relative_deg",
        "traffic_density": "traffic_density_vehicles_per_lane_km",
        "weather": "weather_family",
        "illumination": "sun_altitude_deg",
    }[factor]


def _core_level_name(factor: str, value: Any) -> str:
    suffixes = {
        "height": "m",
        "pitch": "deg",
        "relative_yaw": "deg",
        "traffic_density": "vehicles_per_lane_km",
        "weather": "",
        "illumination": "deg",
    }
    numeric = int(value) if isinstance(value, (int, float)) and float(value).is_integer() else value
    return f"{numeric}{suffixes[factor]}"
