"""Immutable static artifact materialization for Benchmark V3 Benchmark-V3."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Any

from benchmark_generator.common.io import append_jsonl, utc_now, write_json
from benchmark_generator.config import ConfigError, ensure_path_within, load_json
from benchmark_generator.common.io import file_sha256

from benchmark_generator.v3.plan import (
    CONFIG_PATH,
    PROTOCOL_VERSION,
    PLAN_ROOT,
    LABEL_RULE_DEFAULT,
    SPLIT_DECISION,
    PHYSICAL_SUBSETS,
    build_plan,
)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _immutable_files(root: Path) -> dict[str, str]:
    excluded = {"static_manifest.json", "run_status.json", "execution_breadcrumbs.jsonl"}
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in excluded
    }


def materialize_plan(config_path: Path = CONFIG_PATH, run_id: str | None = None) -> dict[str, Any]:
    bundle = build_plan(config_path)
    if run_id is None:
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    if not run_id or "/" in run_id:
        raise ConfigError("plan run_id is invalid")
    root = ensure_path_within(PLAN_ROOT, [PLAN_ROOT.parent], "Benchmark-V3 static root")
    root.mkdir(parents=True, exist_ok=True)
    run_dir = ensure_path_within(root / run_id, [root], "Benchmark-V3 static run")
    if run_dir.exists():
        raise ConfigError(f"Benchmark-V3 static run already exists: {run_dir}")
    for name in ("manifests/subsets", "manifests/test_core/core_scene/by_scene",
                 "manifests/test_core/core_observation/by_factor", "manifests/test_core/core_observation/by_scene",
                 "manifests/test_core/core_axis/by_axis", "manifests/test_core/core_axis/by_scene",
                 "manifests/test_challenge/challenge_l18/by_recipe", "manifests/test_challenge/challenge_l18/by_scene",
                 "indexes", "protocol"):
        (run_dir / name).mkdir(parents=True, exist_ok=False)
    append_jsonl(run_dir / "execution_breadcrumbs.jsonl", {"event": "plan_started", "timestamp": utc_now(), "run_id": run_id, "details": {"carla_connection": "not_used", "image_generation": "not_run"}})

    rows = bundle["rows"]
    _write_jsonl(run_dir / "expected_sample_ledger.jsonl", rows)
    for subset in PHYSICAL_SUBSETS:
        _write_jsonl(run_dir / "manifests" / "subsets" / f"{subset}.jsonl", [r for r in rows if r["physical_subset"] == subset])
    test = [r for r in rows if r["physical_subset"] == "test_shared"]

    def views(v: str) -> list[dict[str, Any]]:
        return [r for r in test if v in (r["test_view"] or ())]

    core_scene = views("core_scene")
    _write_jsonl(run_dir / "manifests" / "test_core" / "core_scene.jsonl", core_scene)
    for scene in sorted({r["scene"] for r in core_scene}):
        _write_jsonl(run_dir / "manifests" / "test_core" / "core_scene" / "by_scene" / f"{scene}.jsonl", [r for r in core_scene if r["scene"] == scene])

    core_obs = views("core_observation")
    _write_jsonl(run_dir / "manifests" / "test_core" / "core_observation.jsonl", core_obs)
    by_factor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in core_obs:
        by_factor[f"{r['changed_factor']}_{r['factor_level']}"].append(r)
    for key, values in sorted(by_factor.items()):
        _write_jsonl(run_dir / "manifests" / "test_core" / "core_observation" / "by_factor" / f"{key}.jsonl", values)
    for scene in sorted({r["scene"] for r in core_obs}):
        _write_jsonl(run_dir / "manifests" / "test_core" / "core_observation" / "by_scene" / f"{scene}.jsonl", [r for r in core_obs if r["scene"] == scene])
    pairs = []
    ref_by_anchor = {r["anchor_id"]: r for r in core_scene if r["is_reference"]}
    for r in core_obs:
        reference = ref_by_anchor[r["anchor_id"]]
        pairs.append({"sample_id": r["sample_id"], "reference_sample_id": reference["sample_id"], "anchor_id": r["anchor_id"], "changed_factor": r["changed_factor"], "factor_level": r["factor_level"]})
    _write_jsonl(run_dir / "manifests" / "test_core" / "core_observation_pairs.jsonl", pairs)

    core_axis = views("core_axis")
    _write_jsonl(run_dir / "manifests" / "test_core" / "core_axis.jsonl", core_axis)
    for axis in sorted({r["challenge_axis_id"] for r in core_axis}):
        _write_jsonl(run_dir / "manifests" / "test_core" / "core_axis" / "by_axis" / f"{axis}.jsonl", [r for r in core_axis if r["challenge_axis_id"] == axis])
    for scene in sorted({r["scene"] for r in core_axis}):
        _write_jsonl(run_dir / "manifests" / "test_core" / "core_axis" / "by_scene" / f"{scene}.jsonl", [r for r in core_axis if r["scene"] == scene])

    chal_l18 = views("challenge_l18")
    _write_jsonl(run_dir / "manifests" / "test_challenge" / "challenge_l18.jsonl", chal_l18)
    for row_id in sorted({r["challenge_row_id"] for r in chal_l18}):
        _write_jsonl(run_dir / "manifests" / "test_challenge" / "challenge_l18" / "by_recipe" / f"{row_id}.jsonl", [r for r in chal_l18 if r["challenge_row_id"] == row_id])
    for scene in sorted({r["scene"] for r in chal_l18}):
        _write_jsonl(run_dir / "manifests" / "test_challenge" / "challenge_l18" / "by_scene" / f"{scene}.jsonl", [r for r in chal_l18 if r["scene"] == scene])

    write_json(run_dir / "indexes" / "test_index.json", {"schema_version": "benchmark_v3_test_index_1.0", "test_shared": True, "samples": [r["sample_id"] for r in test]})
    shutil.copy2(bundle["config"]["_config_path"], run_dir / "protocol" / "config.json")
    shutil.copy2(SPLIT_DECISION, run_dir / "protocol" / "split_decision.json")
    label_rule = bundle["config"]["label_rule"]
    merge_rows = [r for r in rows if r["v3_physical_marking_merge"]]
    write_json(run_dir / "protocol" / "label_rule_scope.json", {
        "schema_version": "benchmark_v3_label_rule_scope_1.0",
        "protocol_version": PROTOCOL_VERSION,
        "label_rule_version": label_rule["rule_version"],
        "default_rule_version": LABEL_RULE_DEFAULT,
        "source_adjudication": label_rule["source_adjudication"],
        "confirmed_site_ids": sorted(label_rule["confirmed_site_ids"]),
        "special_drop_site_id": label_rule["special_drop_site_id"],
        "special_drop_source_ref": list(label_rule["special_drop_source_ref"]),
        "default_path_multi_span_sites": sorted(label_rule["default_path_multi_span_sites"]),
        "default_path_multi_span_reason": label_rule["default_path_multi_span_reason"],
        "merge_site_count": len({r["site_id"] for r in merge_rows}),
        "merge_sample_count": len(merge_rows),
        "default_path_sample_count": len(rows) - len(merge_rows),
        "merge_site_ids": sorted({r["site_id"] for r in merge_rows}),
    })
    write_json(run_dir / "run_status.json", {"schema_version": "benchmark_v3_plan_status_1.0", "run_id": run_id, "status": "plan_ready", "planned": len(rows), "completed": 0, "carla_connection": "not_used", "image_generation": "not_run"})

    manifest = {
        "schema_version": "benchmark_v3_plan_manifest_1.0", "run_id": run_id,
        "protocol_version": PROTOCOL_VERSION, "stage": "benchmark_v3_plan",
        "config_path": bundle["config"]["_config_path"], "config_sha256": bundle["config"]["_config_sha256"],
        "source_checksums": bundle["config"].get("_source_checksums", {}),
        "immutable_files": _immutable_files(run_dir),
        "counts": bundle["plan"]["subset_counts"], "sample_ids": [r["sample_id"] for r in rows],
        "reuse": 0, "new_capture": len(rows),
        "view_counts": bundle["plan"]["view_counts"],
        "label_rule": {"merge_site_count": bundle["plan"]["merge_site_count"], "default_path_site_count": bundle["plan"]["default_path_site_count"]},
        "status": "plan_ready",
        "official_labels": list(bundle["rows"][0]["official_labels"]),
        "carla_connection": "not_used", "image_generation": "not_run", "baseline": False,
    }
    write_json(run_dir / "static_manifest.json", manifest)
    append_jsonl(run_dir / "execution_breadcrumbs.jsonl", {"event": "plan_complete", "timestamp": utc_now(), "run_id": run_id, "details": {"expected_images": len(rows), "reuse": 0, "new": len(rows), "merge_sites": bundle["plan"]["merge_site_count"]}})
    return {"run_id": run_id, "run_dir": str(run_dir), "expected_image_count": len(rows), "counts": bundle["plan"]["subset_counts"], "reuse": 0, "new_capture": len(rows)}


def validate_plan(run_dir: Path) -> dict[str, Any]:
    root = ensure_path_within(Path(run_dir), [PLAN_ROOT], "Benchmark-V3 static validation root")
    manifest = load_json(root / "static_manifest.json")
    if root.name != manifest["run_id"] or _immutable_files(root) != manifest["immutable_files"]:
        raise ConfigError("Benchmark-V3 static artifact checksum drifted")
    bundle = build_plan(Path(manifest["config_path"]))
    stored = [json.loads(line) for line in (root / "expected_sample_ledger.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if stored != bundle["rows"]:
        raise ConfigError("Benchmark-V3 static ledger drifted")
    return manifest


__all__ = ["materialize_plan", "validate_plan"]
