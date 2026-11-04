"""Execution context, canonical publication, four-view manifests and
review for Benchmark-V3 (full regeneration, no asset reuse)."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import csv
import json
import math
from pathlib import Path
from typing import Any

from benchmark_generator.common.io import append_jsonl, utc_now, write_json
from benchmark_generator.config import ConfigError, ensure_path_within, load_json

from .constants import OFFICIAL_LABELS, V3_SCHEMA_ROOT
from benchmark_generator.common.io import file_sha256
from benchmark_generator.v3.plan import (
    PLAN_ROOT,
    CAPTURE_ROOT,
    PROTOCOL_VERSION,
    PHYSICAL_SUBSETS,
    build_plan,
)
from benchmark_generator.v3.plan_artifacts import validate_plan
from .schema_support import validate_schema_instance


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_execution_bundle(static_run: Path) -> dict[str, Any]:
    root = ensure_path_within(Path(static_run), [PLAN_ROOT], "Benchmark-V3 plan input")
    manifest = validate_plan(root)
    bundle = build_plan(Path(manifest["config_path"]))
    return {**bundle, "static_run": root, "static_manifest": manifest}


def create_execution_context(bundle: dict[str, Any], argv: list[str], gpu_report: dict[str, Any], resume_run: Path | None = None) -> dict[str, Any]:
    from datetime import datetime, timezone
    root = ensure_path_within(CAPTURE_ROOT, [CAPTURE_ROOT.parent], "Benchmark-V3 execution root")
    root.mkdir(parents=True, exist_ok=True)
    if resume_run is None:
        run_id = f"benchmark_v3_capture_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        run_dir = ensure_path_within(root / run_id, [root], "Benchmark-V3 execution run")
        if run_dir.exists():
            raise ConfigError(f"Benchmark-V3 execution run exists: {run_dir}")
        run_dir.mkdir(parents=True)
        for name in ("canonical", "_staging", "_workers", "carla_logs", "manifests/subsets",
                     "manifests/test_core/core_scene/by_scene", "manifests/test_core/core_observation/by_factor",
                     "manifests/test_core/core_observation/by_scene", "manifests/test_core/core_axis/by_axis",
                     "manifests/test_core/core_axis/by_scene", "manifests/test_challenge/challenge_l18/by_recipe",
                     "manifests/test_challenge/challenge_l18/by_scene", "indexes", "qa", "review", "review/review_decisions"):
            (run_dir / name).mkdir(parents=True, exist_ok=False)
        for name in ("execution_breadcrumbs.jsonl", "results.jsonl", "failures.jsonl", "retries.jsonl", "skips.jsonl"):
            (run_dir / name).touch()
        static = bundle["static_run"]
        manifest = {
            "schema_version": "benchmark_v3_execution_manifest_1.0", "run_id": run_id,
            "protocol_version": bundle["plan"].get("protocol_version", PROTOCOL_VERSION), "stage": "benchmark_v3_generation",
            "release_id": bundle["config"]["release_id"],
            "config_path": bundle["config"]["_config_path"], "config_sha256": bundle["config"]["_config_sha256"],
            "static_run": str(static), "static_manifest_sha256": file_sha256(static / "static_manifest.json"),
            "expected_ledger_sha256": file_sha256(static / "expected_sample_ledger.jsonl"),
            "sample_ids": [row["sample_id"] for row in bundle["rows"]],
            "gpu": gpu_report, "command": argv,
            "lifecycle": {**bundle["config"]["lifecycle"], "mode": "one_new_owned_process_group_per_map", "worker_map_load": "forbidden"},
            "canonical_dataset": True, "baseline_training": False, "baseline_inference": False, "baseline_evaluation": False,
            "completion_status": "in_progress",
        }
        write_json(run_dir / "run_manifest.json", manifest)
        _write_jsonl(run_dir / "expected_sample_ledger.jsonl", bundle["rows"])
        write_json(run_dir / "run_status.json", {"run_id": run_id, "status": "in_progress", "planned": len(bundle["rows"]), "accepted": 0, "rejected": 0, "retried": 0})
        append_jsonl(run_dir / "execution_breadcrumbs.jsonl", {"event": "capture_initialized", "timestamp": utc_now(), "run_id": run_id, "details": {"planned": len(bundle["rows"]), "gpu": gpu_report}})
    else:
        run_dir = ensure_path_within(Path(resume_run), [root], "Benchmark-V3 resume run")
        manifest = load_json(run_dir / "run_manifest.json")
        run_id = run_dir.name
        immutable = {"run_id": run_id, "config_sha256": bundle["config"]["_config_sha256"],
                     "static_run": str(bundle["static_run"]), "static_manifest_sha256": file_sha256(bundle["static_run"] / "static_manifest.json"),
                     "expected_ledger_sha256": file_sha256(bundle["static_run"] / "expected_sample_ledger.jsonl"),
                     "sample_ids": [row["sample_id"] for row in bundle["rows"]]}
        drifted = [key for key, value in immutable.items() if manifest.get(key) != value]
        if drifted:
            raise ConfigError(f"Benchmark-V3 resume identity drifted: {drifted}")
        append_jsonl(run_dir / "execution_breadcrumbs.jsonl", {"event": "capture_resumed", "timestamp": utc_now(), "run_id": run_id, "details": {"gpu_preflight": gpu_report}})
    return {**bundle, "run_id": run_id, "run_dir": run_dir, "manifest": manifest}


def _finalize_bundles(context: dict[str, Any]) -> list[dict[str, Any]]:
    run_dir = Path(context["run_dir"])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in context["rows"]:
        if row["physical_subset"] == "test_shared":
            grouped[row["anchor_id"]].append(row)
    rejections = []
    for anchor_id, rows in sorted(grouped.items()):
        missing = [row["sample_id"] for row in rows if not (run_dir / "canonical" / row["sample_id"]).is_dir()]
        if len(rows) != 40 or missing:
            rejections.append({"anchor_id": anchor_id, "expected": 40, "planned": len(rows), "missing_or_rejected": missing, "disposition": "entire_anchor_rejected"})
    return rejections


def _manifest_row(run_dir: Path, row: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": row["sample_id"], "display_name": row["display_name"], "physical_subset": row["physical_subset"],
        "logical_test_view": row.get("logical_test_view"), "test_view": row.get("test_view"), "test_shared": row["test_shared"],
        "site_id": row["site_id"], "leakage_group_id": row["leakage_group_id"], "anchor_id": row["anchor_id"],
        "map_id": row["map_id"], "structural_cell": row["structural_cell"], "scene": row["scene"],
        "observation_recipe_id": row["observation_recipe_id"], "requested_conditions": row["requested_conditions"],
        "factor_levels": row["factor_levels"], "control_bundle_id": row["control_bundle_id"],
        "reference_sample_id": row["reference_sample_id"], "is_reference": row["is_reference"],
        "changed_factor": row["changed_factor"], "factor_level": row["factor_level"],
        "challenge_row_id": row["challenge_row_id"], "challenge_level_tuple": row["challenge_level_tuple"],
        "challenge_axis_id": row["challenge_axis_id"],
        "yaw_magnitude_deg": row["yaw_magnitude_deg"], "yaw_sign": row["yaw_sign"],
        "generation_status": row["generation_status"],
        "independent_capture_semantics": row.get("independent_capture_semantics"),
        "label_rule_version": row["label_rule_version"],
        "paths": {"rgb": f"canonical/{row['sample_id']}/rgb.jpg",
                  "road_mask_visible": f"canonical/{row['sample_id']}/road_mask_visible.png",
                  "metadata": f"canonical/{row['sample_id']}/metadata.json"},
        "lane_instance_count": len(metadata["labels"]["lane_polyline_projected"]["instances"]),
        "visibility_semantics": metadata["labels"]["lane_polyline_projected"]["visibility_semantics"],
        "qa_status": metadata["qa"]["status"], "qa_policy_version": metadata["qa"]["policy_version"],
        "metadata_sha256": file_sha256(run_dir / "canonical" / row["sample_id"] / "metadata.json"),
        "source_uri": metadata["source_uri"],
    }


def _build_views(run_dir: Path, context: dict[str, Any], manifest_rows: list[dict[str, Any]]) -> None:
    test = [r for r in manifest_rows if r["test_shared"]]

    def emit(path: Path, rows: list[dict[str, Any]]) -> None:
        _write_jsonl(run_dir / path, rows)

    core_scene = [r for r in test if "core_scene" in (r["test_view"] or [])]
    emit(Path("manifests/test_core/core_scene.jsonl"), core_scene)
    for scene in sorted({r["scene"] for r in core_scene}):
        emit(Path("manifests/test_core/core_scene/by_scene") / f"{scene}.jsonl", [r for r in core_scene if r["scene"] == scene])
    core_obs = [r for r in test if "core_observation" in (r["test_view"] or [])]
    emit(Path("manifests/test_core/core_observation.jsonl"), core_obs)
    by_factor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in core_obs:
        by_factor[f"{r['changed_factor']}_{r['factor_level']}"].append(r)
    for key, values in sorted(by_factor.items()):
        emit(Path("manifests/test_core/core_observation/by_factor") / f"{key}.jsonl", values)
    for scene in sorted({r["scene"] for r in core_obs}):
        emit(Path("manifests/test_core/core_observation/by_scene") / f"{scene}.jsonl", [r for r in core_obs if r["scene"] == scene])
    pairs = []
    refs = {r["anchor_id"]: r for r in test if r["is_reference"]}
    for r in core_obs:
        pairs.append({"sample_id": r["sample_id"], "reference_sample_id": refs[r["anchor_id"]]["sample_id"], "anchor_id": r["anchor_id"], "changed_factor": r["changed_factor"], "factor_level": r["factor_level"]})
    _write_jsonl(run_dir / "manifests/test_core/core_observation_pairs.jsonl", pairs)
    core_axis = [r for r in test if "core_axis" in (r["test_view"] or [])]
    emit(Path("manifests/test_core/core_axis.jsonl"), core_axis)
    for axis in sorted({r["challenge_axis_id"] for r in core_axis}):
        emit(Path("manifests/test_core/core_axis/by_axis") / f"{axis}.jsonl", [r for r in core_axis if r["challenge_axis_id"] == axis])
    for scene in sorted({r["scene"] for r in core_axis}):
        emit(Path("manifests/test_core/core_axis/by_scene") / f"{scene}.jsonl", [r for r in core_axis if r["scene"] == scene])
    chal = [r for r in test if "challenge_l18" in (r["test_view"] or [])]
    emit(Path("manifests/test_challenge/challenge_l18.jsonl"), chal)
    for row_id in sorted({r["challenge_row_id"] for r in chal}):
        emit(Path("manifests/test_challenge/challenge_l18/by_recipe") / f"{row_id}.jsonl", [r for r in chal if r["challenge_row_id"] == row_id])
    for scene in sorted({r["scene"] for r in chal}):
        emit(Path("manifests/test_challenge/challenge_l18/by_scene") / f"{scene}.jsonl", [r for r in chal if r["scene"] == scene])


def finalize_execution(context: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(context["run_dir"])
    bundle_rejections = _finalize_bundles(context)
    manifest_rows = []
    accepted_rows = []
    for row in context["rows"]:
        md_path = run_dir / "canonical" / row["sample_id"] / "metadata.json"
        if not md_path.is_file():
            continue
        metadata = load_json(md_path)
        validate_schema_instance(metadata, load_json(V3_SCHEMA_ROOT / "sample_metadata.schema.json"), f"Benchmark-V3 metadata {row['sample_id']}")
        accepted_rows.append(row)
        manifest_rows.append(_manifest_row(run_dir, row, metadata))
    manifest_rows.sort(key=lambda item: item["sample_id"])
    for subset in PHYSICAL_SUBSETS:
        _write_jsonl(run_dir / "manifests" / "subsets" / f"{subset}.jsonl", [row for row in manifest_rows if row["physical_subset"] == subset])
    _build_views(run_dir, context, manifest_rows)
    with (run_dir / "indexes" / "sample_index.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = ["sample_id", "display_name", "physical_subset", "map_id", "structural_cell", "scene", "site_id", "anchor_id", "leakage_group_id", "observation_recipe_id", "qa_status", "generation_status", "label_rule_version", "source_uri"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row[key] for key in fields} for row in manifest_rows)
    subset_counts = {}
    for subset in PHYSICAL_SUBSETS:
        values = [row for row in accepted_rows if row["physical_subset"] == subset]
        subset_counts[subset] = {"images": len(values), "anchors": len({row["anchor_id"] for row in values}), "sites": len({row["site_id"] for row in values}), "leakage_groups": len({row["leakage_group_id"] for row in values})}
    failure_rows = [json.loads(line) for line in (run_dir / "failures.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    retry_rows = [json.loads(line) for line in (run_dir / "retries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    planned = len(context["rows"])
    accepted = len(accepted_rows)
    rejected = planned - accepted
    train = [row for row in accepted_rows if row["physical_subset"] == "train"]
    val = [row for row in accepted_rows if row["physical_subset"] == "val"]
    merge_accepted = [row for row in accepted_rows if row["v3_physical_marking_merge"]]
    qa_summary = {
        "schema_version": "benchmark_v3_qa_summary_1.0", "planned": planned, "accepted": accepted,
        "rejected": rejected, "retried": len(retry_rows), "reuse": 0, "new_capture": accepted,
        "subset_counts": subset_counts,
        "development_traffic": {
            "train": {"zero": sum(float(r["requested_conditions"]["traffic_density_vehicles_per_lane_km"]) == 0 for r in train), "nonzero": sum(float(r["requested_conditions"]["traffic_density_vehicles_per_lane_km"]) > 0 for r in train)},
            "val": {"zero": sum(float(r["requested_conditions"]["traffic_density_vehicles_per_lane_km"]) == 0 for r in val), "nonzero": sum(float(r["requested_conditions"]["traffic_density_vehicles_per_lane_km"]) > 0 for r in val)},
        },
        "label_rule": {
            "merge_accepted_samples": len(merge_accepted),
            "merge_sites": len({row["site_id"] for row in merge_accepted}),
            "default_path_accepted_samples": accepted - len(merge_accepted),
        },
        "bundle_rejections": bundle_rejections,
        "traffic": {"collision_or_overlap_failures": sum("collision" in str(row.get("error_message", "")).lower() or "overlap" in str(row.get("error_message", "")).lower() for row in failure_rows)},
        "official_labels": list(OFFICIAL_LABELS),
    }
    automatic_blockers = []
    if subset_counts["train"]["images"] != 12000 or subset_counts["val"]["images"] != 1584 or subset_counts["test_shared"]["images"] != 5040:
        automatic_blockers.append("accepted_subset_counts_differ_from_static")
    if qa_summary["development_traffic"]["train"]["zero"] != qa_summary["development_traffic"]["train"]["nonzero"] or qa_summary["development_traffic"]["val"]["zero"] != qa_summary["development_traffic"]["val"]["nonzero"]:
        automatic_blockers.append("development_traffic_not_50_50")
    if bundle_rejections:
        automatic_blockers.append("test_anchor_bundles_incomplete")
    if merge_accepted and len({row["site_id"] for row in merge_accepted}) != 24:
        automatic_blockers.append("merge_site_count_drifts_from_confirmed_scope")
    qa_summary["automatic_blockers"] = automatic_blockers
    qa_summary["automatic_gate_status"] = "pass" if not automatic_blockers else "fail_closed"
    write_json(run_dir / "qa" / "qa_summary.json", qa_summary)
    _write_jsonl(run_dir / "qa" / "bundle_rejections.jsonl", bundle_rejections)
    status_value = "awaiting_user_review" if not automatic_blockers else "failed_automatic_gate"
    write_json(run_dir / "run_status.json", {"run_id": context["run_id"], "status": status_value, "planned": planned, "accepted": accepted, "rejected": rejected, "retried": len(retry_rows), "subset_counts": subset_counts, "automatic_blockers": automatic_blockers})
    append_jsonl(run_dir / "execution_breadcrumbs.jsonl", {"event": "capture_finalized", "timestamp": utc_now(), "run_id": context["run_id"], "details": {"status": status_value, "accepted": accepted, "rejected": rejected}})
    return {"run_dir": str(run_dir), "status": status_value, "accepted": accepted, "rejected": rejected, "qa_summary": str(run_dir / "qa" / "qa_summary.json")}


def build_review_package(context: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(context["run_dir"])
    rows = context["rows"]
    test = [row for row in rows if row["physical_subset"] == "test_shared"]
    cards = []
    from PIL import Image
    width = int(context["config"]["review"]["thumbnail_width"])
    for index, row in enumerate(test, start=1):
        short_id = f"V3F3{index:05d}"
        sample_root = run_dir / "canonical" / row["sample_id"]
        metadata = load_json(sample_root / "metadata.json")
        cards.append({
            "short_id": short_id, "sample_id": row["sample_id"], "physical_subset": row["physical_subset"],
            "test_view": list(row.get("test_view") or []), "scene": row["scene"],
            "structural_cell": row["structural_cell"], "site_id": row["site_id"], "anchor_id": row["anchor_id"],
            "challenge_axis_id": row["challenge_axis_id"], "challenge_row_id": row["challenge_row_id"],
            "yaw_sign": row["yaw_sign"], "lane_change_actor_count": row.get("traffic_plan", {}).get("lane_change_actor_count", 0),
            "generation_status": row["generation_status"],
            "label_rule_version": row["label_rule_version"],
            "source_uri": metadata["source_uri"],
            "assets": {name: f"../canonical/{row['sample_id']}/{name}" for name in ("rgb.jpg", "road_mask_visible_overlay.jpg", "lane_polyline_projected_overlay.jpg")},
        })
    review_root = run_dir / "review"
    _write_jsonl(review_root / "cards.jsonl", cards)
    with (review_root / "short_id_map.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = ["short_id", "sample_id", "physical_subset", "test_view", "scene", "structural_cell", "site_id", "anchor_id", "challenge_axis_id", "challenge_row_id", "generation_status", "label_rule_version", "source_uri"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: card[key] for key in fields} for card in cards)

    def view_count(view: str) -> int:
        return sum(1 for c in cards if view in c["test_view"])
    result = {
        "schema_version": "benchmark_v3_review_package_1.0", "status": "awaiting_user_review",
        "card_count": len(cards),
        "view_memberships": {"core_scene": view_count("core_scene"), "core_observation": view_count("core_observation"), "core_axis": view_count("core_axis"), "challenge_l18": view_count("challenge_l18")},
        "manifest_coverage": {"core_scene": view_count("core_scene"), "core_observation": view_count("core_observation"), "core_axis": view_count("core_axis"), "challenge_l18": view_count("challenge_l18")},
        "full_regeneration": {"total": len(cards), "reviewed": 0, "ratio": 0.0},
        "filters": ["view", "scene", "label_rule", "yaw_sign"],
        "human_review": "not_claimed", "review_decisions_expected_empty": True,
    }
    import html as html_lib
    table = []
    for card in cards:
        links = " ".join(f'<a href="{html_lib.escape(path)}">{html_lib.escape(name)}</a>' for name, path in card["assets"].items())
        table.append(f"<tr><td>{card['short_id']}</td><td>{card['physical_subset']}</td><td>{card['scene']}</td><td>{card['structural_cell']}</td><td>{' '.join(card['test_view'])}</td><td>{card['generation_status']}</td><td>{card['label_rule_version']}</td><td>{links}</td></tr>")
    (review_root / "index.html").write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>Benchmark V3 Benchmark-V3 review</title>"
        "<style>body{font-family:system-ui;margin:20px}table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #bbb;padding:4px}</style></head><body>"
        f"<h1>Benchmark V3 Benchmark-V3 awaiting user review</h1>"
        f"<p>counts: {json.dumps({k: v for k, v in result.items() if k in ('card_count', 'view_memberships', 'full_regeneration')}, ensure_ascii=False)}</p>"
        "<table><tr><th>ID</th><th>subset</th><th>scene</th><th>cell</th><th>views</th><th>status</th><th>label rule</th><th>assets</th></tr>"
        + "".join(table) + "</table></body></html>",
        encoding="utf-8",
    )
    write_json(review_root / "review_manifest.json", result)
    return result


__all__ = ["build_review_package", "create_execution_context", "finalize_execution", "load_execution_bundle"]
