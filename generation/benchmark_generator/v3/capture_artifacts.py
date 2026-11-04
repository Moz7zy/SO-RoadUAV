"""Immutable offline artifacts for the V3 R4 corrective pilot."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import shutil
from typing import Any

from benchmark_generator.common.io import append_jsonl, utc_now, write_json
from benchmark_generator.config import ConfigError, ensure_path_within, forbid_legacy_source_path, load_json

from .constants import (
    OFFICIAL_LABELS,
    R4_ARTIFACT_RULE_VERSION,
    R4_CORRECTIVE_ARTIFACT_ROOT,
    R4_PROTOCOL_VERSION,
    R4_RECIPE_TABLE_SCHEMA_VERSION,
    R4_SOURCE_CHECKSUMS_SCHEMA_VERSION,
    R4_STATIC_MANIFEST_SCHEMA_VERSION,
    R4_STATIC_STATUS_SCHEMA_VERSION,
    R4_TRAFFIC_LEDGER_SCHEMA_VERSION,
    R4_LEDGER_SCHEMA_VERSION,
    V3_CONFIG_ROOT,
    V3_SCHEMA_ROOT,
)
from benchmark_generator.common.io import file_sha256
from benchmark_generator.v3.capture_plan import (
    R4_ASSETS,
    R4_STATIC_SCHEMA_NAMES,
    build_r4_plan,
    build_r4_review_cards,
    build_r4_review_index,
)
from .schema_support import validate_schema_instance


STATIC_MANIFEST_NAME = "static_manifest.json"
STATIC_STATUS_NAME = "run_status.json"
BREADCRUMB_NAME = "execution_breadcrumbs.jsonl"
STATIC_EXCLUDED_FROM_FILE_MANIFEST = {STATIC_MANIFEST_NAME, STATIC_STATUS_NAME, BREADCRUMB_NAME}


def _schema(name: str) -> Path:
    path = ensure_path_within(V3_SCHEMA_ROOT / name, [V3_SCHEMA_ROOT], f"V3 R4 schema {name}")
    if not path.is_file():
        raise ConfigError(f"V3 R4 schema is missing: {path}")
    return path


def _immutable_file_checksums(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in STATIC_EXCLUDED_FROM_FILE_MANIFEST or relative.endswith(".tmp"):
            continue
        result[relative] = file_sha256(path)
    return result


def _render_review_html(index: dict[str, Any], cards: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for card in cards:
        rows: list[str] = []
        for row in card["rows"]:
            sample_id = html.escape(str(row["sample_id"]))
            recipe = html.escape(str(row["recipe_id"]))
            kind = html.escape(str(row["pilot_kind"]))
            condition = html.escape(json.dumps(row["requested_conditions"], ensure_ascii=False, sort_keys=True))
            geometry = html.escape(json.dumps(row["camera_geometry"], ensure_ascii=False, sort_keys=True))
            traffic = html.escape(json.dumps(row["traffic"], ensure_ascii=False, sort_keys=True))
            rows.append(
                "<tr>"
                f"<td><code>{sample_id}</code></td><td><code>{recipe}</code></td><td>{kind}</td>"
                f"<td><a href=\"../samples/{sample_id}/rgb.jpg\">RGB</a> "
                f"<a href=\"../samples/{sample_id}/road_mask_visible_overlay.jpg\">road</a> "
                f"<a href=\"../samples/{sample_id}/lane_polyline_projected_overlay.jpg\">lane</a></td>"
                f"<td><code>{condition}</code></td><td><code>{geometry}</code></td>"
                f"<td><details><summary>traffic QA/actors</summary><pre>{traffic}</pre></details></td>"
                "</tr>"
            )
        sections.append(
            f"<section data-bundle=\"{html.escape(card['bundle_key'])}\">"
            f"<h2>{html.escape(card['map_id'])} / {html.escape(card['structural_cell'])} / {html.escape(card['anchor_id'])}</h2>"
            f"<p>site={html.escape(card['site_id'])}; rows={len(card['rows'])}; "
            f"formal bundle status={html.escape(card['bundle_status'])}; incomplete disposition={html.escape(card['incomplete_formal_bundle_disposition'])}</p>"
            "<table><thead><tr><th>sample</th><th>recipe</th><th>pilot kind</th><th>future assets</th><th>recipe</th><th>camera geometry</th><th>traffic</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>"
        )
    options = "".join(
        f"<option value=\"{html.escape(item['bundle_key'])}\">{html.escape(item['bundle_key'])}</option>"
        for item in index["bundles"]
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>V3 R4 corrective review</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:24px;color:#18212b}section{border-top:2px solid #74808c;margin-top:24px;padding-top:12px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #c9d0d6;padding:5px;vertical-align:top}th{background:#eef1f3}pre{white-space:pre-wrap;max-width:720px;max-height:420px;overflow:auto}a{margin-right:8px}</style>"
        "</head><body><h1>Benchmark V3-P1 corrective / R4 static review</h1>"
        "<p>Static only: pilot_evidence=true; canonical_dataset=false; no RGB or labels generated. "
        "Links become reviewable after separately approved CARLA capture.</p>"
        f"<label>Anchor <select id=\"bundle-filter\" onchange=\"filterBundles()\"><option value=\"\">all</option>{options}</select></label>"
        f"{''.join(sections)}"
        "<script>function filterBundles(){const v=document.getElementById('bundle-filter').value;document.querySelectorAll('section[data-bundle]').forEach(e=>e.hidden=!!v&&e.dataset.bundle!==v)}</script>"
        "</body></html>"
    )


def _static_manifest(bundle: dict[str, Any], run_id: str, root: Path) -> dict[str, Any]:
    rows = bundle["rows"]
    return {
        "schema_version": R4_STATIC_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "protocol_version": R4_PROTOCOL_VERSION,
        "stage": "p1_corrective_static",
        "artifact_kind": "static",
        "config_path": bundle["config"]["_config_path"],
        "config_sha256": bundle["config"]["_config_sha256"],
        "source_checksums": dict(bundle["plan"]["source_checksums"]),
        "immutable_files": _immutable_file_checksums(root),
        "sample_ids": [row["sample_id"] for row in rows],
        "map_ids": ["Town04_Opt", "Town05_Opt", "Town10HD_Opt"],
        "expected_image_count": 24,
        "official_labels": list(OFFICIAL_LABELS),
        "status": "static_ready_waiting_explicit_r4_corrective_carla_approval",
        "pilot_evidence": True,
        "canonical_dataset": False,
        "carla_connection": "not_used",
        "image_generation": "not_run",
        "registry_write": False,
        "split_write": False,
        "old_p1": "immutable_not_mutated",
        "p0": "closed_not_mutated",
        "rc": "not_entered",
        "full": "not_entered",
    }


def materialize_r4_static_artifact(
    config_path: Path = None,
    output_root: Path = R4_CORRECTIVE_ARTIFACT_ROOT,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Write one new immutable R4 static artifact; never write images."""

    from capture_plan import R4_CONFIG_PATH

    config_path = R4_CONFIG_PATH if config_path is None else Path(config_path)
    root = ensure_path_within(Path(output_root), [R4_CORRECTIVE_ARTIFACT_ROOT], "V3 R4 static artifact root")
    forbid_legacy_source_path(root, "V3 R4 static artifact root")
    bundle = build_r4_plan(config_path)
    if run_id is None:
        run_id = f"benchmark_v3_p1_corrective_static_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_r4"
    if not run_id.startswith("benchmark_v3_p1_corrective_static_"):
        raise ConfigError("V3 R4 static run_id must use the independent corrective prefix")
    run_dir = ensure_path_within(root / run_id, [root], "V3 R4 static artifact run")
    if run_dir.exists():
        raise ConfigError(f"V3 R4 static artifact already exists and will not be overwritten: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "review" / "cards").mkdir(parents=True, exist_ok=False)
    (run_dir / "traffic_layouts").mkdir(parents=True, exist_ok=False)
    breadcrumbs = run_dir / BREADCRUMB_NAME
    append_jsonl(breadcrumbs, {"run_id": run_id, "event": "r4_static_artifact_started", "timestamp": utc_now(), "details": {"carla_connection": "not_used", "image_generation": "not_run", "expected_image_count": 24}})

    write_json(run_dir / "r4_plan.json", bundle["plan"])
    write_json(run_dir / "expected_r4_ledger.json", bundle["expected_bundle"])
    write_json(run_dir / "expected_r4_traffic_ledger.json", bundle["traffic_bundle"])
    write_json(run_dir / "recipe_table.json", bundle["recipe_table"])
    write_json(run_dir / "development_geometry_contract.json", bundle["development_contract"])
    write_json(run_dir / "rc_height_calibration.json", bundle["config"]["_rc_calibration"])
    if bundle["config"].get("_traffic_topology_path"):
        shutil.copy2(bundle["config"]["_traffic_topology_path"], run_dir / "traffic_topology_layout.json")
    for anchor_id, layout in sorted(bundle["masters"].items()):
        write_json(run_dir / "traffic_layouts" / f"{anchor_id}.json", layout)

    source_payload = {
        "schema_version": R4_SOURCE_CHECKSUMS_SCHEMA_VERSION,
        "protocol_version": R4_PROTOCOL_VERSION,
        "stage": "p1_corrective_static",
        "checksums": dict(bundle["plan"]["source_checksums"]),
    }
    validate_schema_instance(source_payload, load_json(_schema("r4_source_checksums.schema.json")), "V3 R4 source checksums")
    write_json(run_dir / "source_checksums.json", source_payload)
    write_json(run_dir / "protocol_decision.json", bundle["protocol_decision"])

    review_index = build_r4_review_index(bundle["rows"], run_id)
    cards = build_r4_review_cards(bundle["rows"])
    validate_schema_instance(review_index, load_json(_schema("r4_review_index.schema.json")), "V3 R4 review index")
    for card in cards:
        validate_schema_instance(card, load_json(_schema("r4_review_card.schema.json")), f"V3 R4 review card {card['bundle_key']}")
        write_json(run_dir / "review" / "cards" / f"corrective_pilot_{card['anchor_id']}.json", card)
    write_json(run_dir / "review" / "r4_review_index.json", review_index)
    (run_dir / "review" / "index.html").write_text(_render_review_html(review_index, cards), encoding="utf-8")

    status = {
        "schema_version": R4_STATIC_STATUS_SCHEMA_VERSION,
        "run_id": run_id,
        "stage": "p1_corrective_static",
        "status": "static_ready_waiting_explicit_r4_corrective_carla_approval",
        "planned": 24,
        "completed": 0,
        "reused": 0,
        "failed": 0,
        "carla_connection": "not_used",
        "image_generation": "not_run",
        "pilot_evidence": True,
        "canonical_dataset": False,
        "registry_write": False,
        "split_write": False,
        "old_p1": "immutable_not_mutated",
        "p0": "closed_not_mutated",
        "rc": "not_entered",
        "full": "not_entered",
    }
    # The status schema intentionally accepts only the stable status fields;
    # its extra audit fields are retained in the artifact but not schema-gated.
    status_schema = load_json(_schema("r4_static_status.schema.json"))
    validate_schema_instance({key: status[key] for key in status_schema["required"]}, status_schema, "V3 R4 static status")
    write_json(run_dir / STATIC_STATUS_NAME, status)
    manifest = _static_manifest(bundle, run_id, run_dir)
    validate_schema_instance(manifest, load_json(_schema("r4_static_manifest.schema.json")), "V3 R4 static manifest")
    write_json(run_dir / STATIC_MANIFEST_NAME, manifest)
    append_jsonl(breadcrumbs, {"run_id": run_id, "event": "r4_static_artifact_complete", "timestamp": utc_now(), "details": {"expected_image_count": 24, "bundle_count": 4, "carla_connection": "not_used", "image_generation": "not_run"}})
    return {"run_id": run_id, "run_dir": str(run_dir), "manifest": str(run_dir / STATIC_MANIFEST_NAME), "expected_image_count": 24}


def rebuild_r4_manifest(run_dir: Path) -> dict[str, Any]:
    root = ensure_path_within(Path(run_dir), [R4_CORRECTIVE_ARTIFACT_ROOT], "V3 R4 static manifest rebuild root")
    manifest_path = root / STATIC_MANIFEST_NAME
    if not manifest_path.is_file():
        raise ConfigError(f"V3 R4 static manifest is missing: {manifest_path}")
    manifest = load_json(manifest_path)
    manifest["immutable_files"] = _immutable_file_checksums(root)
    return manifest


def validate_r4_static_artifact(run_dir: Path) -> dict[str, Any]:
    root = ensure_path_within(Path(run_dir), [R4_CORRECTIVE_ARTIFACT_ROOT], "V3 R4 static artifact validation root")
    manifest = load_json(root / STATIC_MANIFEST_NAME)
    validate_schema_instance(manifest, load_json(_schema("r4_static_manifest.schema.json")), "V3 R4 static manifest")
    if root.name != manifest["run_id"]:
        raise ConfigError("V3 R4 static manifest run_id does not match its directory")
    if rebuild_r4_manifest(root)["immutable_files"] != manifest["immutable_files"]:
        raise ConfigError("V3 R4 static artifact immutable checksums drifted")
    status = load_json(root / STATIC_STATUS_NAME)
    status_schema = load_json(_schema("r4_static_status.schema.json"))
    validate_schema_instance({key: status[key] for key in status_schema["required"]}, status_schema, "V3 R4 static status")
    source_payload = load_json(root / "source_checksums.json")
    validate_schema_instance(source_payload, load_json(_schema("r4_source_checksums.schema.json")), "V3 R4 source checksums")

    bundle = build_r4_plan(Path(manifest["config_path"]))
    if manifest["config_sha256"] != bundle["config"]["_config_sha256"] or manifest["source_checksums"] != bundle["plan"]["source_checksums"]:
        raise ConfigError("V3 R4 static config/source checksum drifted")
    if source_payload["checksums"] != manifest["source_checksums"]:
        raise ConfigError("V3 R4 source checksum payload differs from manifest")

    expected_files = {
        "r4_plan.json": bundle["plan"],
        "expected_r4_ledger.json": bundle["expected_bundle"],
        "expected_r4_traffic_ledger.json": bundle["traffic_bundle"],
        "recipe_table.json": bundle["recipe_table"],
        "development_geometry_contract.json": bundle["development_contract"],
        "rc_height_calibration.json": bundle["config"]["_rc_calibration"],
        "protocol_decision.json": bundle["protocol_decision"],
    }
    if bundle["config"].get("_traffic_topology_path"):
        expected_files["traffic_topology_layout.json"] = load_json(Path(bundle["config"]["_traffic_topology_path"]))
    for name, expected in expected_files.items():
        actual = load_json(root / name)
        if actual != expected:
            raise ConfigError(f"V3 R4 static artifact content drifted: {name}")
    for anchor_id, layout in bundle["masters"].items():
        if load_json(root / "traffic_layouts" / f"{anchor_id}.json") != layout:
            raise ConfigError(f"V3 R4 traffic master layout drifted: {anchor_id}")
    actual_index = load_json(root / "review" / "r4_review_index.json")
    expected_index = build_r4_review_index(bundle["rows"], manifest["run_id"])
    if actual_index != expected_index:
        raise ConfigError("V3 R4 review index drifted")
    for card in build_r4_review_cards(bundle["rows"]):
        actual_card = load_json(root / "review" / "cards" / f"corrective_pilot_{card['anchor_id']}.json")
        if actual_card != card:
            raise ConfigError(f"V3 R4 review card drifted: {card['bundle_key']}")
    return manifest


__all__ = ["materialize_r4_static_artifact", "rebuild_r4_manifest", "validate_r4_static_artifact"]
