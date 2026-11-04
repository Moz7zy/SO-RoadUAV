"""Frozen hierarchical E3 view aggregation from canonical metric rows only."""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from .statistics import bootstrap_sites, holm, sign_flip


def _anchor_metrics(rows: list[dict], task: str) -> dict[tuple[str, str], dict]:
    grouped = defaultdict(list)
    for row in rows: grouped[(row["site_id"], row["anchor_id"])].append(row)
    result = {}
    for key, values in grouped.items():
        if task == "road":
            result[key] = {metric: sum(float(row[metric]) for row in values) / len(values) for metric in ("road_miou_binary_class_macro", "boundary_f1", "road_iou_foreground_only_candidate")}
        else:
            tp = sum(int(row["tp"]) for row in values); fp = sum(int(row["fp"]) for row in values); fn = sum(int(row["fn"]) for row in values); matched = [value for row in values for value in row["matched_ious"]]; den = 2 * tp + fp + fn
            result[key] = {"projected_lane_f1": 2 * tp / den if den else 1.0, "matched_projected_lane_iou": sum(matched) / len(matched) if matched else 0.0, "tp": tp, "fp": fp, "fn": fn}
    return result


def site_values(rows: list[dict], task: str, metric: str) -> dict[str, float]:
    sites = defaultdict(list)
    for (site, _), values in _anchor_metrics(rows, task).items(): sites[site].append(float(values[metric]))
    return {site: sum(values) / len(values) for site, values in sites.items()}


def aggregate_cell(rows: list[dict], task: str, key: str) -> dict:
    primary = "road_miou_binary_class_macro" if task == "road" else "projected_lane_f1"
    values = site_values(rows, task, primary); boot = bootstrap_sites(values, key, 10000)
    anchors = _anchor_metrics(rows, task)
    result = {"primary_metric": primary, "site_macro": boot["estimate"], "ci95": boot["ci95"], "bootstrap_seed": boot["seed"], "bootstrap_replicates": 10000, "counts": {"images": len(rows), "anchors": len(anchors), "sites": len(values)}, "site_values": values}
    if task == "road":
        for metric in ("boundary_f1", "road_iou_foreground_only_candidate"): result[metric] = sum(site_values(rows, task, metric).values()) / len(values)
        result["degenerate"] = {"all_background": sum(row.get("prediction_foreground_pixels", 1) == 0 for row in rows), "all_foreground": sum(row.get("prediction_foreground_pixels", 0) == 1280 * 720 for row in rows)}
    else:
        matched_values = site_values(rows, task, "matched_projected_lane_iou"); result["matched_projected_lane_iou"] = sum(matched_values.values()) / len(matched_values)
        tp = sum(row["tp"] for row in rows); fp = sum(row["fp"] for row in rows); fn = sum(row["fn"] for row in rows); den = 2 * tp + fp + fn
        result["pooled_secondary"] = {"tp": tp, "fp": fp, "fn": fn, "f1": 2 * tp / den if den else 1.0}; result["empty_rate"] = sum(bool(row["empty_prediction"]) for row in rows) / len(rows)
    return result


def reference_degradation(rows: list[dict], all_metrics: dict[str, dict], task: str, key: str) -> dict:
    primary = "road_miou_binary_class_macro" if task == "road" else "projected_lane_f1"
    refs = {row["anchor_id"]: all_metrics[row["reference_sample_id"]] for row in rows if row.get("reference_sample_id") in all_metrics}
    condition = _anchor_metrics(rows, task); by_site = defaultdict(list)
    for (site, anchor), values in condition.items():
        if anchor not in refs: continue
        reference = float(refs[anchor][primary]); by_site[site].append(reference - float(values[primary]))
    site_delta = {site: sum(values) / len(values) for site, values in by_site.items()}; boot = bootstrap_sites(site_delta, key, 10000)
    return {"degradation_reference_minus_condition": boot["estimate"], "condition_minus_reference": -boot["estimate"], "ci95": boot["ci95"], "sites": len(site_delta), "site_values": site_delta, "seed": boot["seed"]}


def view_results(model_id: str, task: str, all_metrics: dict[str, dict], memberships: dict[str, list[dict]]) -> dict:
    output = {"schema_version": "benchmark_v3_e3_view_results_1.0", "model_id": model_id, "task": task, "model_forward_events": 0}
    scene_rows = [all_metrics[row["sample_id"]] for row in memberships["core_scene"]]
    output["core_scene"] = {scene: aggregate_cell([row for row in scene_rows if row["scene"] == scene], task, f"{model_id}:scene:{scene}") for scene in sorted({row["scene"] for row in scene_rows})}
    obs = {}
    for view in memberships["core_observation"]:
        metric = all_metrics[view["sample_id"]]; metric = {**metric, "reference_sample_id": view["reference_sample_id"]}
        obs.setdefault((view["factor"], str(view["level"])), []).append(metric)
    output["core_observation"] = {f"{factor}:{level}": {"absolute": aggregate_cell(rows, task, f"{model_id}:obs:{factor}:{level}"), "relative": reference_degradation(rows, all_metrics, task, f"{model_id}:obs-delta:{factor}:{level}")} for (factor, level), rows in sorted(obs.items())}
    axes = {}
    for view in memberships["core_axis"]: axes.setdefault(view["axis"], []).append(all_metrics[view["sample_id"]])
    output["core_axis"] = {axis: {"absolute": aggregate_cell(rows, task, f"{model_id}:axis:{axis}"), "relative": reference_degradation(rows, all_metrics, task, f"{model_id}:axis-delta:{axis}")} for axis, rows in sorted(axes.items())}
    challenge_rows = [all_metrics[row["sample_id"]] for row in memberships["challenge_l18"]]
    non_ramp = [row for row in challenge_rows if row["scene"] != "ramp"]; ramp = [row for row in challenge_rows if row["scene"] == "ramp"]
    output["challenge_l18"] = {"primary_non_rare": aggregate_cell(non_ramp, task, f"{model_id}:challenge:primary"), "all_53_sensitivity": aggregate_cell(challenge_rows, task, f"{model_id}:challenge:all"), "ramp_descriptive": aggregate_cell(ramp, task, f"{model_id}:challenge:ramp"), "relative_non_rare": reference_degradation([row for row in non_ramp if not row.get("is_reference")], all_metrics, task, f"{model_id}:challenge:delta")}
    output["challenge_l18"]["by_row"] = {row_id: aggregate_cell([all_metrics[row["sample_id"]] for row in memberships["challenge_l18"] if row["row_id"] == row_id], task, f"{model_id}:l18:{row_id}") for row_id in sorted({row["row_id"] for row in memberships["challenge_l18"]})}
    return output


def method_significance(results: dict[str, dict], task: str) -> list[dict]:
    rows = []
    for left, right in combinations(sorted(results), 2):
        a = results[left]["challenge_l18"]["primary_non_rare"]["site_values"]; b = results[right]["challenge_l18"]["primary_non_rare"]["site_values"]
        rows.append({"methods": [left, right], **sign_flip(a, b, f"challenge:{task}:{left}:{right}", 10000)})
    return holm(rows)
