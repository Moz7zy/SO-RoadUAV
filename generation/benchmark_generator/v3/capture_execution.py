"""CARLA-free execution contracts for the R4 corrective pilot.

The execution root is separate from the immutable R4 static artifact.  This
module owns only manifests, resume checks, staging publication, completeness
validation, and review evidence; the delayed CARLA import lives in the map
worker.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable

from benchmark_generator.common.io import append_jsonl, utc_now, write_json
from benchmark_generator.config import ConfigError, ensure_path_within, forbid_legacy_source_path, load_json

from .constants import (
    OFFICIAL_LABELS,
    R4_CONFLICT_SAFE_TOPOLOGY_RULE_VERSION,
    R4_EXECUTION_ARTIFACT_ROOT,
    R4_EXECUTION_MANIFEST_SCHEMA_VERSION,
    R4_EXECUTION_RULE_VERSION,
    R4_EXECUTION_STATUS_SCHEMA_VERSION,
    R4_PROTOCOL_VERSION,
    R4_SAMPLE_METADATA_SCHEMA_VERSION,
    WORKER_RESULT_SCHEMA_VERSION,
    V3_ARTIFACT_ROOT,
    V3_SCHEMA_ROOT,
)
from benchmark_generator.common.io import file_sha256
from benchmark_generator.v3.capture_plan import R4_ASSETS, R4_CONFIG_PATH, R4_MAPS, _uses_conflict_safe_layout, assert_formal_traffic_gate, build_r4_plan
from benchmark_generator.v3.capture_artifacts import validate_r4_static_artifact
from .schema_support import validate_schema_instance
from .replay import REPLAY_CANONICAL_SCHEMA_VERSION, canonical_replay, canonical_payload, payload_hash, replay_mismatch_diagnostics


EXECUTION_SCHEMA_NAMES = (
    "r4_execution_manifest.schema.json",
    "r4_execution_status.schema.json",
    "worker_result.schema.json",
    "r4_sample_metadata.schema.json",
)


def _schema(name: str) -> dict[str, Any]:
    return load_json(ensure_path_within(V3_SCHEMA_ROOT / name, [V3_SCHEMA_ROOT], f"R4 execution schema {name}"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ConfigError(f"R4 JSONL row is not an object: {path}")
        rows.append(value)
    return rows


def _copy_json(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def resolve_r4_static_run(static_run: Path | None) -> Path:
    if static_run is None:
        raise ConfigError("R4 corrective execution requires an explicit immutable --static-run")
    root = ensure_path_within(Path(static_run), [V3_ARTIFACT_ROOT / "p1_corrective_static"], "R4 corrective static run")
    if not root.is_dir() or not root.name.startswith("benchmark_v3_p1_corrective_static_"):
        raise ConfigError(f"R4 corrective static run is invalid: {root}")
    forbid_legacy_source_path(root, "R4 corrective static run")
    return root


def load_r4_execution_bundle(static_run: Path | None) -> dict[str, Any]:
    root = resolve_r4_static_run(static_run)
    manifest = validate_r4_static_artifact(root)
    bundle = build_r4_plan(Path(manifest["config_path"]))
    expected_files = {
        "r4_plan.json": bundle["plan"],
        "expected_r4_ledger.json": bundle["expected_bundle"],
        "expected_r4_traffic_ledger.json": bundle["traffic_bundle"],
        "recipe_table.json": bundle["recipe_table"],
    }
    for name, expected in expected_files.items():
        if load_json(root / name) != expected:
            raise ConfigError(f"R4 static input drifted before execution: {name}")
    return {
        "static_run": root,
        "static_manifest": manifest,
        "config": bundle["config"],
        "plan": bundle["plan"],
        "rows": bundle["rows"],
        "traffic_rows": bundle["traffic_rows"],
        "bundle": bundle,
    }


def partition_rows(rows: Iterable[dict[str, Any]], shard_count: int, shard_index: int) -> list[dict[str, Any]]:
    if int(shard_count) != 1 or int(shard_index) != 0:
        raise ConfigError("R4 corrective pilot currently freezes shard_count=1 and shard_index=0")
    ordered = sorted(rows, key=lambda row: row["sample_id"])
    return [row for index, row in enumerate(ordered) if index % int(shard_count) == int(shard_index)]


def build_execution_manifest(
    bundle: dict[str, Any],
    run_id: str,
    argv: Iterable[str],
    *,
    gpu_report: dict[str, Any] | None = None,
    sample_ids: Iterable[str] | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    config = bundle["config"]
    static_root = Path(bundle["static_run"])
    static_manifest = bundle["static_manifest"]
    static_files = {
        "run_id": static_manifest["run_id"],
        "artifact_root": str(static_root),
        "static_manifest_sha256": file_sha256(static_root / "static_manifest.json"),
        "r4_plan_sha256": file_sha256(static_root / "r4_plan.json"),
        "expected_r4_ledger_sha256": file_sha256(static_root / "expected_r4_ledger.json"),
        "expected_r4_traffic_ledger_sha256": file_sha256(static_root / "expected_r4_traffic_ledger.json"),
        "recipe_table_sha256": file_sha256(static_root / "recipe_table.json"),
        "source_checksums_sha256": file_sha256(static_root / "source_checksums.json"),
    }
    source_checksums = dict(static_manifest["source_checksums"])
    source_checksums.update({f"static_artifact/{name}": digest for name, digest in static_files.items() if name != "run_id" and name != "artifact_root"})
    source_checksums = dict(sorted(source_checksums.items()))
    selected_sample_ids = list(sample_ids) if sample_ids is not None else [row["sample_id"] for row in bundle["rows"]]
    selected_sample_ids = sorted(selected_sample_ids)
    if not selected_sample_ids:
        raise ConfigError("R4 execution manifest cannot have an empty sample selection")
    if scope is None:
        if len(selected_sample_ids) == 24:
            frozen_scope = "corrective_4_anchors_24_rows_r4"
        elif len(selected_sample_ids) == 6:
            frozen_scope = "corrective_conflict_safe_6_rows_r4"
        elif len(selected_sample_ids) == 14:
            frozen_scope = "corrective_conflict_safe_14_rows_r4"
        else:
            frozen_scope = "corrective_conflict_safe_7_rows_r4"
    else:
        frozen_scope = scope
    manifest = {
        "schema_version": R4_EXECUTION_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "protocol_version": R4_PROTOCOL_VERSION,
        "stage": "p1_corrective",
        "scope": frozen_scope,
        "config_path": config["_config_path"],
        "config_sha256": config["_config_sha256"],
        "static_artifact": static_files,
        "source_checksums": source_checksums,
        "sample_ids": selected_sample_ids,
        "map_ids": list(R4_MAPS),
        "shard_count": int(config["sharding"]["shard_count"]),
        "official_labels": list(OFFICIAL_LABELS),
        "gpu": dict(gpu_report or {"graphics_adapter": int(config["lifecycle"]["graphics_adapter"]), "preflight": "required_before_server_start"}),
        "runtime": {
            "python_executable": config["runtime"]["carla_python"],
            "preflight_before_server_start": True,
            "qa_policy_path": config["_qa_policy_path"],
            "qa_policy_sha256": config["_qa_policy_sha256"],
        },
        "lifecycle": {
            "mode": "one_new_owned_server_process_group_per_map",
            "host": config["lifecycle"]["host"],
            "rpc_port": int(config["lifecycle"]["rpc_port"]),
            "traffic_manager_port": int(config["lifecycle"]["traffic_manager_port"]),
            "graphics_adapter": int(config["lifecycle"]["graphics_adapter"]),
            "adopt_existing": False,
            "shutdown_policy": config["lifecycle"]["shutdown_policy"],
            "worker_map_load": "forbidden",
            "one_process_group_per_map": True,
            "server_ready_timeout_seconds": float(config["limits"]["server_ready_timeout_seconds"]),
            "map_load_timeout_seconds_by_map": dict(config["limits"]["map_load_timeout_seconds_by_map"]),
        },
        "qa_policy_version": config["qa"]["policy_version"],
        "command": list(argv),
        "pilot_evidence": True,
        "canonical_dataset": False,
        "registry_write": False,
        "formal_split": "not_run",
        "p0": "closed_not_mutated",
        "rc": "not_entered",
        "full": "not_entered",
        "rule_version": R4_EXECUTION_RULE_VERSION,
    }
    validate_schema_instance(manifest, _schema("r4_execution_manifest.schema.json"), "R4 corrective execution manifest")
    return manifest


def _new_run_id() -> str:
    return f"benchmark_v3_p1_corrective_r4_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def _status(run_id: str, status: str = "in_progress", planned: int = 24) -> dict[str, Any]:
    value = {
        "schema_version": R4_EXECUTION_STATUS_SCHEMA_VERSION,
        "run_id": run_id,
        "stage": "p1_corrective",
        "status": status,
        "planned": int(planned),
        "completed": 0,
        "reused": 0,
        "failed": 0,
        "pilot_evidence": True,
        "canonical_dataset": False,
        "registry_write": False,
        "split_write": False,
    }
    validate_schema_instance(value, _schema("r4_execution_status.schema.json"), "R4 corrective execution status")
    return value


def _validate_execution_ledgers(run_dir: Path, bundle: dict[str, Any]) -> None:
    expected = {
        "expected_r4_ledger.json": bundle["bundle"]["expected_bundle"],
        "expected_r4_traffic_ledger.json": bundle["bundle"]["traffic_bundle"],
    }
    for name, value in expected.items():
        path = ensure_path_within(run_dir / name, [run_dir], f"R4 execution ledger {name}")
        if not path.is_file() or load_json(path) != value:
            raise ConfigError(f"R4 execution ledger drifted or is missing: {name}")


def create_execution_context(
    bundle: dict[str, Any],
    argv: Iterable[str],
    *,
    resume_run: Path | None = None,
    gpu_report: dict[str, Any] | None = None,
    sample_ids: Iterable[str] | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    root = ensure_path_within(R4_EXECUTION_ARTIFACT_ROOT, [V3_ARTIFACT_ROOT], "R4 corrective execution root")
    root.mkdir(parents=True, exist_ok=True)
    if resume_run is None:
        run_id = _new_run_id()
        run_dir = ensure_path_within(root / run_id, [root], "R4 corrective execution run")
        if run_dir.exists():
            raise ConfigError(f"R4 corrective execution run already exists: {run_dir}")
        selected_ids = sorted(sample_ids) if sample_ids is not None else [row["sample_id"] for row in bundle["rows"]]
        selected_rows = [row for row in bundle["rows"] if row["sample_id"] in set(selected_ids)]
        if len(selected_rows) != len(selected_ids):
            raise ConfigError("R4 execution sample selection contains an unknown sample ID")
        manifest = build_execution_manifest(bundle, run_id, argv, gpu_report=gpu_report, sample_ids=selected_ids, scope=scope)
        run_dir.mkdir(parents=True, exist_ok=False)
        for name in ("samples", "_staging", "_workers", "carla_logs", "review"):
            (run_dir / name).mkdir(exist_ok=False)
        write_json(run_dir / "run_manifest.json", manifest)
        _copy_json(Path(bundle["static_run"]) / "expected_r4_ledger.json", run_dir / "expected_r4_ledger.json")
        _copy_json(Path(bundle["static_run"]) / "expected_r4_traffic_ledger.json", run_dir / "expected_r4_traffic_ledger.json")
        write_json(run_dir / "static_source_ref.json", {"static_run": str(bundle["static_run"]), "static_manifest_sha256": manifest["static_artifact"]["static_manifest_sha256"], "pilot_evidence": True, "canonical_dataset": False})
        for name in ("execution_breadcrumbs.jsonl", "results.jsonl", "integrity.jsonl", "failures.jsonl"):
            (run_dir / name).touch(exist_ok=False)
        append_jsonl(run_dir / "execution_breadcrumbs.jsonl", {"run_id": run_id, "event": "r4_execution_initialized", "timestamp": utc_now(), "details": {"map_ids": manifest["map_ids"], "expected_image_count": len(selected_ids), "pilot_evidence": True, "canonical_dataset": False}})
    else:
        run_dir = ensure_path_within(Path(resume_run), [root], "R4 corrective resume run")
        run_id = run_dir.name
        if not (run_dir / "run_manifest.json").is_file():
            raise ConfigError(f"R4 corrective resume run is incomplete: {run_dir}")
        existing = load_json(run_dir / "run_manifest.json")
        validate_schema_instance(existing, _schema("r4_execution_manifest.schema.json"), "R4 corrective resume manifest")
        candidate = build_execution_manifest(bundle, run_id, argv, gpu_report=gpu_report, sample_ids=existing["sample_ids"], scope=existing["scope"])
        candidate["gpu"] = existing["gpu"]
        # A resume command necessarily has a different argv.  All protocol,
        # source, scope, lifecycle, and sample identity fields remain exact.
        immutable = tuple(key for key in existing if key != "command" and key != "gpu")
        drifted = [key for key in immutable if existing.get(key) != candidate.get(key)]
        if drifted:
            raise ConfigError(f"R4 corrective resume manifest drifted in immutable fields: {drifted}")
        _validate_execution_ledgers(run_dir, bundle)
        append_jsonl(run_dir / "execution_breadcrumbs.jsonl", {"run_id": run_id, "event": "r4_execution_resumed", "timestamp": utc_now(), "details": {"expected_image_count": len(existing["sample_ids"])}})
        manifest = existing
    if resume_run is None:
        manifest = load_json(run_dir / "run_manifest.json")
    status = _status(run_id, planned=len(manifest["sample_ids"]))
    write_json(run_dir / "run_status.json", status)
    selected_ids = set(manifest["sample_ids"])
    selected_rows = [row for row in bundle["rows"] if row["sample_id"] in selected_ids]
    selected_traffic_rows = [row for row in bundle["traffic_rows"] if row["sample_id"] in selected_ids]
    return {"config": bundle["config"], "plan": bundle["plan"], "rows": selected_rows, "traffic_rows": selected_traffic_rows, "bundle": bundle, "manifest": manifest, "run_id": run_id, "run_dir": run_dir, "static_run": Path(bundle["static_run"]), "static_manifest": bundle["static_manifest"]}


def validate_r4_sample(run_dir: Path, row: dict[str, Any]) -> dict[str, Any] | None:
    samples_root = ensure_path_within(Path(run_dir) / "samples", [Path(run_dir)], "R4 sample root")
    sample_dir = ensure_path_within(samples_root / row["sample_id"], [samples_root], "R4 sample")
    if not sample_dir.exists():
        return None
    if not sample_dir.is_dir() or not (sample_dir / "metadata.json").is_file():
        raise ConfigError(f"partial immutable R4 sample cannot be overwritten: {sample_dir}")
    metadata = load_json(sample_dir / "metadata.json")
    validate_schema_instance(metadata, _schema("r4_sample_metadata.schema.json"), f"R4 sample metadata {row['sample_id']}")
    identity = {"sample_id": row["sample_id"], "map_id": row["map_id"], "site_id": row["site_id"], "anchor_id": row["anchor_id"], "leakage_group_id": row["leakage_group_id"], "structural_cell": row["structural_cell"], "observation_recipe_id": row["observation_recipe_id"], "recipe_family": row["recipe_family"], "pilot_kind": row["pilot_kind"]}
    for key, expected in identity.items():
        if metadata.get(key) != expected:
            raise ConfigError(f"R4 sample identity mismatch for {row['sample_id']}: {key}")
    if metadata.get("official_labels") != OFFICIAL_LABELS or set(metadata.get("labels", {})) != set(OFFICIAL_LABELS):
        raise ConfigError(f"R4 official label contract drifted for {row['sample_id']}")
    expected_traffic = row["traffic_plan"]
    actual_traffic = metadata["traffic"]
    for key in ("requested_density", "lane_km", "target_actor_count", "planned_actor_count", "master_layout_seed", "master_layout_hash", "planned_world_state_hash"):
        if actual_traffic.get(key) != expected_traffic.get(key):
            raise ConfigError(f"R4 traffic metadata mismatch for {row['sample_id']}: {key}")
    if int(actual_traffic.get("actual_actor_count", -1)) != int(expected_traffic["planned_actor_count"]):
        raise ConfigError(f"R4 actual actor count mismatch for {row['sample_id']}")
    replay = actual_traffic.get("replay", {})
    if replay.get("schema_version") == REPLAY_CANONICAL_SCHEMA_VERSION:
        replay_audit = canonical_replay(
            protocol_version=R4_PROTOCOL_VERSION,
            master_layout_hash=actual_traffic["master_layout_hash"],
            density=float(actual_traffic["requested_density"]),
            planned_actors=actual_traffic["planned_actors"],
            actual_actors=actual_traffic["actual_actors"],
        )
        if not replay_audit["match"] or replay.get("status") != "pass" or replay.get("planned_replay_hash") != replay_audit["planned_hash"] or replay.get("actual_replay_hash") != replay_audit["actual_hash"]:
            raise ConfigError(f"R4 canonical replay is not a verified pass for {row['sample_id']}")
    conflict_exclusion = actual_traffic.get("qa", {}).get("topology", {}).get("conflict_exclusion", {})
    if actual_traffic.get("layout_rule_version") == R4_CONFLICT_SAFE_TOPOLOGY_RULE_VERSION or str(actual_traffic.get("layout_rule_version", "")).endswith(("_0.3", "_0.4")):
        if conflict_exclusion.get("status") != "pass" or int(actual_traffic.get("qa", {}).get("topology", {}).get("junction_waypoint_count", 0)) != 0:
            raise ConfigError(f"R4 conflict-safe traffic gate is not a verified pass for {row['sample_id']}")
    assert_formal_traffic_gate(
        actual_traffic.get("qa", {}),
        traffic=actual_traffic,
        world_state=metadata.get("world_state"),
        sample_id=row["sample_id"],
    )
    if metadata.get("qa", {}).get("status") not in {"pass", "warn", "needs_manual_review"}:
        raise ConfigError(f"R4 sample has invalid QA disposition for {row['sample_id']}")
    for name in R4_ASSETS:
        path = sample_dir / name
        if not path.is_file():
            raise ConfigError(f"R4 sample asset is missing: {path}")
        asset = metadata.get("assets", {}).get(name, {})
        if asset.get("path") != f"samples/{row['sample_id']}/{name}":
            raise ConfigError(f"R4 asset path drifted for {row['sample_id']}: {name}")
        if name != "metadata.json" and asset.get("sha256") != file_sha256(path):
            raise ConfigError(f"R4 asset checksum mismatch for {row['sample_id']}: {name}")
    integrity = [item for item in _read_jsonl(Path(run_dir) / "integrity.jsonl") if item.get("sample_id") == row["sample_id"]]
    if len(integrity) != 1:
        raise ConfigError(f"R4 integrity record is missing or duplicated for {row['sample_id']}")
    if integrity[0].get("metadata_sha256") != file_sha256(sample_dir / "metadata.json"):
        raise ConfigError(f"R4 metadata checksum mismatch for {row['sample_id']}")
    for name in R4_ASSETS:
        if integrity[0].get("assets", {}).get(name) != file_sha256(sample_dir / name):
            raise ConfigError(f"R4 integrity asset checksum mismatch for {row['sample_id']}: {name}")
    return metadata


def publish_r4_sample(staging_dir: Path, final_dir: Path) -> dict[str, str]:
    staging = ensure_path_within(Path(staging_dir), [R4_EXECUTION_ARTIFACT_ROOT], "R4 staging sample")
    final = ensure_path_within(Path(final_dir), [R4_EXECUTION_ARTIFACT_ROOT], "R4 published sample")
    if final.exists():
        raise ConfigError(f"R4 sample already exists and will not be overwritten: {final}")
    missing = [name for name in R4_ASSETS if not (staging / name).is_file()]
    if missing:
        raise ConfigError(f"R4 staging sample is incomplete: {missing}")
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, final)
    return {name: file_sha256(final / name) for name in R4_ASSETS}


def validate_worker_result(context: dict[str, Any], map_id: str, result_path: Path) -> dict[str, Any]:
    path = ensure_path_within(Path(result_path), [Path(context["run_dir"]) / "_workers"], "R4 worker result")
    if not path.is_file():
        raise ConfigError(f"R4 worker result is missing for {map_id}")
    payload = load_json(path)
    validate_schema_instance(payload, _schema("worker_result.schema.json"), f"R4 worker result {map_id}")
    expected = sorted(row["sample_id"] for row in partition_rows((row for row in context["rows"] if row["map_id"] == map_id), int(context["manifest"]["shard_count"]), int(payload.get("shard_index", 0))))
    if payload.get("run_id") != context["run_id"] or payload.get("map_id") != map_id or payload.get("worker_map_load") != "not_performed":
        raise ConfigError(f"R4 worker identity or map-load contract drifted for {map_id}")
    if payload.get("cleanup_errors") or payload.get("failed_sample_ids"):
        raise ConfigError(f"R4 worker result is not reusable for {map_id}")
    if payload.get("expected_sample_ids") != expected:
        raise ConfigError(f"R4 worker expected IDs drifted for {map_id}")
    terminal = payload.get("completed_sample_ids", []) + payload.get("reused_sample_ids", [])
    if sorted(terminal) != expected or len(terminal) != len(set(terminal)):
        raise ConfigError(f"R4 worker terminal results are incomplete or duplicated for {map_id}")
    if {item.get("sample_id") for item in payload.get("rows", [])} != set(expected):
        raise ConfigError(f"R4 worker result rows do not cover {map_id}")
    return payload


def _render_execution_review(run_id: str, rows: list[dict[str, Any]], metadata_by_id: dict[str, dict[str, Any]]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["anchor_id"], []).append(row)
    sections: list[str] = []
    for anchor_id, group in sorted(grouped.items()):
        first = group[0]
        table_rows: list[str] = []
        for row in sorted(group, key=lambda item: item["sample_id"]):
            metadata = metadata_by_id.get(row["sample_id"], {})
            qa = metadata.get("qa", {})
            traffic = metadata.get("traffic", {})
            sid = html.escape(row["sample_id"])
            table_rows.append(
                "<tr>"
                f"<td><code>{sid}</code></td><td>{html.escape(row['pilot_kind'])}</td>"
                f"<td>{html.escape(str(row['requested_conditions'].get('height_m')))}m / density={html.escape(str(row['traffic_plan'].get('requested_density')))}</td>"
                f"<td>{html.escape(str(qa.get('status', 'missing')))}</td>"
                f"<td><a href=\"../samples/{sid}/rgb.jpg\">RGB</a> <a href=\"../samples/{sid}/road_mask_visible_overlay.jpg\">road</a> <a href=\"../samples/{sid}/lane_polyline_projected_overlay.jpg\">lane</a> <a href=\"../samples/{sid}/metadata.json\">metadata</a></td>"
                f"<td><details><summary>traffic</summary><pre>{html.escape(json.dumps(traffic, ensure_ascii=False, sort_keys=True, indent=2))}</pre></details></td>"
                "</tr>"
            )
        sections.append(
            f"<section><h2>{html.escape(first['map_id'])} / {html.escape(first['structural_cell'])} / {html.escape(anchor_id)}</h2>"
            f"<p>site={html.escape(first['site_id'])}; rows={len(group)}; corrective evidence only</p>"
            "<table><thead><tr><th>sample</th><th>kind</th><th>recipe</th><th>QA</th><th>assets</th><th>traffic</th></tr></thead>"
            f"<tbody>{''.join(table_rows)}</tbody></table></section>"
        )
    return "<!doctype html><html><head><meta charset=\"utf-8\"><title>V3 R4 corrective review</title><style>body{font-family:system-ui,sans-serif;margin:24px;color:#18212b}section{border-top:2px solid #74808c;margin-top:24px;padding-top:12px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #c9d0d6;padding:5px;vertical-align:top}th{background:#eef1f3}pre{white-space:pre-wrap;max-width:680px;max-height:360px;overflow:auto}a{margin-right:8px}</style></head><body><h1>Benchmark V3-P1 corrective / R4 CARLA pilot</h1><p>run_id=" + html.escape(run_id) + "; pilot_evidence=true; canonical_dataset=false; RC/Full not entered.</p>" + "".join(sections) + "</body></html>"


def _actual_replay_hash(metadata: dict[str, Any]) -> str:
    if metadata.get("traffic", {}).get("replay", {}).get("schema_version") == REPLAY_CANONICAL_SCHEMA_VERSION:
        actual_payload = metadata["traffic"]["replay"]["canonical_payload"]["actual"]
        return payload_hash(actual_payload)
    actors = [
        {
            "actor_id": item["actor_id"],
            "vehicle_blueprint_id": item["vehicle_blueprint_id"],
            "transform": item["resolved_transform"],
        }
        for item in metadata["traffic"]["actual_actors"]
    ]
    replay_input = {
        "master_layout_hash": metadata["traffic"]["master_layout_hash"],
        "density": metadata["traffic"]["requested_density"],
        "actors": actors,
    }
    return hashlib.sha256(json.dumps(replay_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _runtime_traffic_audit(context: dict[str, Any], metadata_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows_by_anchor: dict[str, dict[float, tuple[dict[str, Any], dict[str, Any]]]] = {}
    replay_checked = 0
    for row in context["rows"]:
        metadata = metadata_by_id[row["sample_id"]]
        traffic = metadata["traffic"]
        if "qa" in traffic:
            assert_formal_traffic_gate(
                traffic.get("qa", {}),
                traffic=traffic,
                world_state=metadata.get("world_state"),
                sample_id=row["sample_id"],
            )
        if int(traffic["actual_actor_count"]) != int(traffic["planned_actor_count"]):
            raise ConfigError(f"R4 runtime actor count drifted for {row['sample_id']}")
        replay = traffic.get("replay", {})
        canonical_status = replay.get("schema_version") == REPLAY_CANONICAL_SCHEMA_VERSION
        if canonical_status:
            audit = canonical_replay(
                protocol_version=R4_PROTOCOL_VERSION,
                master_layout_hash=traffic["master_layout_hash"],
                density=float(traffic["requested_density"]),
                planned_actors=traffic["planned_actors"],
                actual_actors=traffic["actual_actors"],
            )
            if not audit["match"]:
                raise ConfigError(f"R4 final canonical replay mismatch for {row['sample_id']}: {audit['mismatch_diagnostics']}")
            if audit["planned_hash"] != replay.get("planned_replay_hash") or audit["actual_hash"] != replay.get("actual_replay_hash"):
                raise ConfigError(f"R4 final canonical replay hash drifted for {row['sample_id']}")
            actual_hash = audit["actual_hash"]
        else:
            actual_hash = _actual_replay_hash(metadata)
        if actual_hash != traffic["replay"]["actual_replay_hash"] or actual_hash != metadata["world_state"]["traffic_replay_hash"]:
            raise ConfigError(f"R4 runtime replay hash drifted for {row['sample_id']}")
        replay_checked += 1
        if row["pilot_kind"] == "density_nested":
            rows_by_anchor.setdefault(row["anchor_id"], {})[float(traffic["requested_density"])] = (row, metadata)

    pair_audits: list[dict[str, Any]] = []
    for anchor_id, densities in sorted(rows_by_anchor.items()):
        if set(densities) != {20.0, 40.0}:
            raise ConfigError(f"R4 runtime density pair is incomplete for {anchor_id}")
        low_row, low_metadata = densities[20.0]
        high_row, high_metadata = densities[40.0]
        low_actors = low_metadata["traffic"]["actual_actors"]
        high_actors = high_metadata["traffic"]["actual_actors"]
        low_by_id = {item["actor_id"]: item for item in low_actors}
        high_by_id = {item["actor_id"]: item for item in high_actors}
        low_ids = set(low_by_id)
        high_ids = set(high_by_id)
        if not low_ids < high_ids:
            raise ConfigError(f"R4 runtime density20 is not a strict subset for {anchor_id}")
        if [item["actor_id"] for item in low_actors] == [item["actor_id"] for item in high_actors[: len(low_actors)]]:
            raise ConfigError(f"R4 runtime density20 unexpectedly uses a master list prefix for {anchor_id}")
        if any(high_by_id[item["actor_id"]]["resolved_transform"] != item["resolved_transform"] for item in low_actors):
            raise ConfigError(f"R4 runtime density20 transform inheritance drifted for {anchor_id}")
        pair_audits.append({
            "anchor_id": anchor_id,
            "low_sample_id": low_row["sample_id"],
            "high_sample_id": high_row["sample_id"],
            "low_actor_count": len(low_actors),
            "high_actor_count": len(high_actors),
            "strict_subset": True,
            "non_prefix": True,
            "transform_inheritance": True,
            "low_replay_hash": low_metadata["traffic"]["replay"]["actual_replay_hash"],
            "high_replay_hash": high_metadata["traffic"]["replay"]["actual_replay_hash"],
        })
    return {"status": "pass", "replay_hash_samples_checked": replay_checked, "canonical_replay_samples_checked": sum(metadata_by_id[row["sample_id"]].get("traffic", {}).get("replay", {}).get("schema_version") == REPLAY_CANONICAL_SCHEMA_VERSION for row in context["rows"]), "density_pair_count": len(pair_audits), "density_pairs": pair_audits}


def finalize_execution(context: dict[str, Any], worker_results: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [sid for result in worker_results for sid in result.get("completed_sample_ids", [])]
    reused = [sid for result in worker_results for sid in result.get("reused_sample_ids", [])]
    failed = [sid for result in worker_results for sid in result.get("failed_sample_ids", [])]
    expected_count = len(context["rows"])
    if failed or len(completed) + len(reused) != expected_count:
        raise ConfigError(f"R4 corrective execution is incomplete: completed={len(completed)} reused={len(reused)} failed={len(failed)}")
    metadata_by_id: dict[str, dict[str, Any]] = {}
    for row in context["rows"]:
        metadata = validate_r4_sample(context["run_dir"], row)
        if metadata is None:
            raise ConfigError(f"R4 sample is missing at finalization: {row['sample_id']}")
        metadata_by_id[row["sample_id"]] = metadata
    runtime_traffic_audit = _runtime_traffic_audit(context, metadata_by_id)
    qa_counts: dict[str, int] = {}
    geometry_counts: dict[str, int] = {}
    traffic_counts: dict[str, int] = {}
    for metadata in metadata_by_id.values():
        qa_status = str(metadata["qa"]["status"])
        qa_counts[qa_status] = qa_counts.get(qa_status, 0) + 1
        geometry_status = str(metadata["qa"].get("strict_geometry", {}).get("status", "unknown"))
        geometry_counts[geometry_status] = geometry_counts.get(geometry_status, 0) + 1
        traffic_status = str(metadata["traffic"].get("qa", {}).get("status", "unknown"))
        traffic_counts[traffic_status] = traffic_counts.get(traffic_status, 0) + 1
    if qa_counts.get("needs_manual_review", 0):
        status_name = "complete_with_manual_review"
    elif qa_counts.get("warn", 0):
        status_name = "complete_with_warnings"
    else:
        status_name = "complete"
    cards_dir = Path(context["run_dir"]) / "review" / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    for anchor_id in sorted({row["anchor_id"] for row in context["rows"]}):
        card_rows = []
        for row in sorted((item for item in context["rows"] if item["anchor_id"] == anchor_id), key=lambda item: item["sample_id"]):
            metadata = metadata_by_id[row["sample_id"]]
            card_rows.append({"sample_id": row["sample_id"], "recipe_id": row["observation_recipe_id"], "pilot_kind": row["pilot_kind"], "requested_conditions": row["requested_conditions"], "camera_geometry": {"planned": row["camera_geometry"], "actual": metadata["actual_camera_geometry"]}, "traffic": metadata["traffic"], "qa": metadata["qa"], "assets": metadata["assets"]})
        write_json(cards_dir / f"corrective_pilot_{anchor_id}.json", {"schema_version": "benchmark_v3_r4_execution_review_card_0.1", "run_id": context["run_id"], "anchor_id": anchor_id, "rows": card_rows, "pilot_evidence": True, "canonical_dataset": False})
    review_index = {"schema_version": "benchmark_v3_r4_execution_review_index_0.1", "run_id": context["run_id"], "bundle_count": len({row["anchor_id"] for row in context["rows"]}), "row_count": expected_count, "cards": [f"cards/corrective_pilot_{anchor_id}.json" for anchor_id in sorted({row["anchor_id"] for row in context["rows"]})], "official_labels": list(OFFICIAL_LABELS), "pilot_evidence": True, "canonical_dataset": False}
    write_json(Path(context["run_dir"]) / "review" / "r4_execution_review_index.json", review_index)
    (Path(context["run_dir"]) / "review" / "index.html").write_text(_render_execution_review(context["run_id"], context["rows"], metadata_by_id), encoding="utf-8")
    status = _status(context["run_id"], status_name, planned=expected_count)
    status.update({"completed": len(completed), "reused": len(reused), "failed": 0})
    validate_schema_instance(status, _schema("r4_execution_status.schema.json"), "R4 final execution status")
    write_json(Path(context["run_dir"]) / "run_status.json", status)
    report = {
        "schema_version": "benchmark_v3_r4_execution_report_0.1", **status,
        "pending_sample_ids": [], "pending_sample_count": 0, "warning_count": qa_counts.get("warn", 0),
        "manual_review_count": qa_counts.get("needs_manual_review", 0), "qa_counts": qa_counts,
        "geometry_status_counts": geometry_counts, "traffic_qa_status_counts": traffic_counts,
        "runtime_traffic_audit": runtime_traffic_audit,
        "review_index": str(Path(context["run_dir"]) / "review" / "r4_execution_review_index.json"),
        "formal_bundle_gate": "not_entered_corrective_targeted_pilot_only",
        "pilot_evidence": True, "canonical_dataset": False, "registry_write": False, "split_write": False,
        "rc": "not_entered", "full": "not_entered",
    }
    write_json(Path(context["run_dir"]) / "r4_execution_report.json", report)
    append_jsonl(Path(context["run_dir"]) / "execution_breadcrumbs.jsonl", {"run_id": context["run_id"], "event": "r4_execution_complete", "timestamp": utc_now(), "details": {"status": status_name, "qa_counts": qa_counts, "traffic_qa_status_counts": traffic_counts, "pilot_evidence": True, "canonical_dataset": False}})
    return {"run_id": context["run_id"], "run_dir": str(context["run_dir"]), "run_status": status, "report": str(Path(context["run_dir"]) / "r4_execution_report.json"), "review_index": str(Path(context["run_dir"]) / "review" / "r4_execution_review_index.json")}


def fail_execution(context: dict[str, Any], exc: BaseException, cleanup_errors: list[str] | None = None) -> None:
    complete: set[str] = set()
    for row in context["rows"]:
        try:
            if validate_r4_sample(context["run_dir"], row) is not None:
                complete.add(row["sample_id"])
        except (ConfigError, json.JSONDecodeError):
            continue
    expected = {row["sample_id"] for row in context["rows"]}
    failed_rows = {item.get("sample_id") for item in _read_jsonl(Path(context["run_dir"]) / "failures.jsonl") if item.get("sample_id")} & expected
    pending = expected - complete - failed_rows
    status = _status(context["run_id"], "failed", planned=len(context["rows"]))
    status.update({"completed": len(complete), "reused": 0, "failed": len(failed_rows)})
    write_json(Path(context["run_dir"]) / "run_status.json", status)
    write_json(Path(context["run_dir"]) / "r4_execution_report.json", {"schema_version": "benchmark_v3_r4_execution_report_0.1", **status, "pending_sample_ids": sorted(pending), "pending_sample_count": len(pending), "primary_error": {"type": type(exc).__name__, "message": str(exc)}, "cleanup_errors": list(cleanup_errors or []), "pilot_evidence": True, "canonical_dataset": False, "rc": "not_entered", "full": "not_entered"})
    append_jsonl(Path(context["run_dir"]) / "execution_breadcrumbs.jsonl", {"run_id": context["run_id"], "event": "r4_execution_failed", "timestamp": utc_now(), "details": {"error_type": type(exc).__name__, "error_message": str(exc), "completed": len(complete), "failed": len(failed_rows), "pending": len(pending), "cleanup_errors": list(cleanup_errors or [])}})


def worker_command(context: dict[str, Any], map_id: str, result_path: Path, host: str, port: int, shard_index: int = 0) -> list[str]:
    config = context["config"]
    return [
        config["runtime"]["carla_python"], "-B", "-m", "benchmark_generator.carla_client.capture",
        "--config", context["manifest"]["config_path"], "--static-run", str(context["static_run"]), "--map-id", map_id,
        "--run-dir", str(context["run_dir"]), "--run-id", context["run_id"], "--result-path", str(result_path),
        "--host", host, "--port", str(port), "--shard-index", str(shard_index), "--shard-count", str(context["manifest"]["shard_count"]),
        "--execute-worker", "--confirm-v3-p1-corrective-worker", "--confirm-v3-p1-corrective", "--confirm-v3-p1-server-lifecycle",
    ]


__all__ = [
    "build_execution_manifest", "create_execution_context", "fail_execution", "finalize_execution",
    "load_r4_execution_bundle", "partition_rows", "publish_r4_sample", "resolve_r4_static_run",
    "validate_r4_sample", "validate_worker_result", "worker_command",
]
