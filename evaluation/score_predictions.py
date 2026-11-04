#!/usr/bin/env python3
"""Score Benchmark-V3 predictions against the published test split.

Prediction layout (all resolutions are the published 1280x720):

    <predictions>/road/<sample_id>.png    binary 0/255 road mask
    <predictions>/lane/<sample_id>.json   {"instances": [{"points_image": [[x, y], ...]}, ...]}

The test manifest is the only source of membership. Nothing is resized: every
metric is computed in the original image coordinate system.

Usage:

    python evaluation/score_predictions.py \
        --dataset dataset --predictions ./preds --model my_model --out ./scores
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

EVALUATION_ROOT = Path(__file__).resolve().parent
if str(EVALUATION_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALUATION_ROOT))

from metrics import aggregation
from metrics.constants import (
    LANE_INSTANCE_CAPACITY,
    LANE_MATCH_IOU_THRESHOLD,
    ORIGINAL_SIZE,
    ROAD_BOUNDARY_TOLERANCE_PX,
)
from metrics.lane import evaluate_lane_sample
from metrics.road import evaluate_road_sample

TEST_COUNT = 5040
VIEW_COUNTS = {"core_scene": 5040, "core_observation": 1890, "core_axis": 882, "challenge_l18": 2268}
VIEW_NAMES = ("core_scene", "core_observation", "core_axis", "challenge_l18")
TASKS = ("road", "lane")


class PredictionError(RuntimeError):
    pass


def resolve(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    root = root.resolve()
    if candidate != root and root not in candidate.parents:
        raise PredictionError(f"path escapes the dataset root: {relative}")
    if not candidate.is_file():
        raise PredictionError(f"missing asset: {candidate}")
    return candidate


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_road_prediction(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise PredictionError(f"could not read road prediction: {path}")
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.shape != (ORIGINAL_SIZE[1], ORIGINAL_SIZE[0]):
        raise PredictionError(f"road prediction must be 1280x720, got {image.shape}: {path}")
    return image > 0


def load_lane_prediction(path: Path) -> list[list[list[float]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    instances = payload.get("instances")
    if not isinstance(instances, list):
        raise PredictionError(f"lane prediction needs an instances list: {path}")
    if len(instances) > LANE_INSTANCE_CAPACITY:
        raise PredictionError(f"lane prediction exceeds {LANE_INSTANCE_CAPACITY} instances: {path}")
    lanes = []
    for item in instances:
        points = item.get("points_image") if isinstance(item, dict) else None
        if not isinstance(points, list) or len(points) < 2:
            continue
        lanes.append([[float(x), float(y)] for x, y in points])
    return lanes


def ground_truth_lanes(label: dict[str, Any]) -> list[list[list[float]]]:
    lanes = []
    for item in label.get("instances", []):
        points = item.get("points_image") or []
        if len(points) >= 2:
            lanes.append([[float(x), float(y)] for x, y in points])
    return lanes


def score_road(dataset: Path, predictions: Path, row: dict[str, Any]) -> dict[str, Any]:
    sample_id = row["sample_id"]
    mask = cv2.imread(str(resolve(dataset, row["road_mask"])), cv2.IMREAD_GRAYSCALE)
    if mask is None:
        raise PredictionError(f"could not read ground-truth road mask: {sample_id}")
    prediction = load_road_prediction(predictions / "road" / f"{sample_id}.png")
    metrics = evaluate_road_sample(prediction, mask > 0, tolerance_px=ROAD_BOUNDARY_TOLERANCE_PX)
    metrics["prediction_foreground_pixels"] = int(prediction.sum())
    return metrics


def score_lane(dataset: Path, predictions: Path, row: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    sample_id = row["sample_id"]
    label = json.loads(resolve(dataset, row["lane"]).read_text(encoding="utf-8"))
    roi_polygons = metadata["labels"]["lane_polyline_projected"]["target_corridor_polygon"]
    prediction = load_lane_prediction(predictions / "lane" / f"{sample_id}.json")
    metrics = evaluate_lane_sample(
        ground_truth_lanes(label),
        prediction,
        roi_polygons,
        size=ORIGINAL_SIZE,
        match_iou_threshold=LANE_MATCH_IOU_THRESHOLD,
    )
    metrics["empty_prediction"] = len(prediction) == 0
    return metrics


def identity(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": metadata["sample_id"],
        "site_id": metadata["site_id"],
        "anchor_id": metadata["anchor_id"],
        "scene": metadata["scene"],
        "is_reference": bool(metadata.get("is_reference")),
        "reference_sample_id": metadata.get("reference_sample_id"),
    }


def check_predictions(root: Path, task: str, sample_ids: list[str]) -> None:
    directory = root / task
    if not directory.is_dir():
        raise PredictionError(f"prediction directory is missing: {directory}")
    suffix = ".png" if task == "road" else ".json"
    expected = {f"{sample_id}{suffix}" for sample_id in sample_ids}
    present = {path.name for path in directory.iterdir() if path.is_file()}
    missing = sorted(expected - present)
    extra = sorted(present - expected)
    if missing:
        raise PredictionError(f"{task}: {len(missing)} predictions missing, first {missing[:5]}")
    if extra:
        print(f"WARNING {task}: ignoring {len(extra)} unexpected files, first {extra[:5]}", file=sys.stderr)


def view_memberships(dataset: Path) -> dict[str, list[dict[str, Any]]]:
    memberships = {}
    for view in VIEW_NAMES:
        rows = load_jsonl(dataset / "manifests" / "views" / f"{view}.jsonl")
        if len(rows) != VIEW_COUNTS[view]:
            raise PredictionError(f"{view} manifest has {len(rows)} rows, expected {VIEW_COUNTS[view]}")
        memberships[view] = rows
    return memberships


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Benchmark-V3 predictions")
    parser.add_argument("--dataset", type=Path, required=True, help="dataset package root")
    parser.add_argument("--predictions", type=Path, required=True, help="prediction root")
    parser.add_argument("--model", required=True, help="model identifier")
    parser.add_argument("--out", type=Path, required=True, help="score output root")
    parser.add_argument("--task", choices=(*TASKS, "both"), default="both")
    parser.add_argument("--limit", type=int, help="score only the first N test samples")
    args = parser.parse_args(argv)

    dataset = args.dataset.resolve()
    rows = load_jsonl(dataset / "manifests" / "subsets" / "test.jsonl")
    if len(rows) != TEST_COUNT:
        raise PredictionError(f"test manifest has {len(rows)} rows, expected {TEST_COUNT}")
    if args.limit:
        rows = rows[: args.limit]
    sample_ids = [row["sample_id"] for row in rows]
    tasks = TASKS if args.task == "both" else (args.task,)

    for task in tasks:
        check_predictions(args.predictions, task, sample_ids)

    metrics_by_sample: dict[str, dict[str, Any]] = {}
    for row in rows:
        metadata = json.loads(resolve(dataset, row["metadata"]).read_text(encoding="utf-8"))
        record = identity(metadata)
        if "road" in tasks:
            record.update(score_road(dataset, args.predictions, row))
        if "lane" in tasks:
            record.update(score_lane(dataset, args.predictions, row, metadata))
        metrics_by_sample[record["sample_id"]] = record

    out_root = args.out / args.model
    out_root.mkdir(parents=True, exist_ok=True)
    with (out_root / "per_sample.jsonl").open("w", encoding="utf-8") as handle:
        for sample_id in sample_ids:
            handle.write(json.dumps(metrics_by_sample[sample_id], ensure_ascii=False, sort_keys=True) + "\n")

    report: dict[str, Any] = {
        "schema_version": "benchmark_v3_score_report_1.0",
        "model_id": args.model,
        "sample_count": len(sample_ids),
        "tasks": list(tasks),
        "limit": args.limit,
    }
    if args.limit is None:
        memberships = view_memberships(dataset)
        for task in tasks:
            report[task] = aggregation.view_results(args.model, task, metrics_by_sample, memberships)
    (out_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"model": args.model, "samples": len(sample_ids), "out": str(out_root)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
