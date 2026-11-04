# Evaluation

```
evaluation/
├── score_predictions.py   command-line scorer
└── metrics/
    ├── constants.py       image sizes, capacity, tolerances
    ├── road.py            road IoU / boundary F1
    ├── lane.py            projected lane F1 / matched IoU
    ├── roi.py             polygon rasterisation and ROI helpers
    ├── aggregation.py     view-level site-macro aggregation
    └── statistics.py      bootstrap and paired tests
```

Dependencies: `numpy`, `opencv-python`, `scipy`. The evaluator intentionally has
no deep-learning dependency.

## Usage

Download the dataset package first (scoring only needs the test split):

```bash
python scripts/download_dataset.py --dest dataset --parts test-eval
```

Then run the scorer:

```bash
python evaluation/score_predictions.py \
    --dataset dataset \
    --predictions ./preds \
    --model my_model \
    --out ./scores
```

Options: `--task road|lane|both` (default `both`), `--limit N` for a smoke test.

### Prediction layout

```
preds/
├── road/<sample_id>.png     binary 0/255, 1280×720
└── lane/<sample_id>.json    {"instances": [{"points_image": [[x, y], ...]}, ...]}
```

At most 10 lane instances per image. All coordinates are in the original
1280×720 frame; the scorer never resizes.

### Output

```
scores/<model>/per_sample.jsonl   identity + all metric fields, one row per test sample
scores/<model>/report.json        per-view aggregates, 95% CIs, reference deltas
```

## Metric summary

| Task | Primary | Secondary |
|---|---|---|
| road | `road_miou_binary_class_macro` = mean of road IoU and background IoU | `boundary_f1` (3 px tolerance), foreground-only IoU |
| lane | `projected_lane_f1` = `2TP/(2TP+FP+FN)` with IoU ≥ 0.5 matching | `matched_projected_lane_iou`, empty rate |

Aggregation is anchor → site → site macro, with 10,000 site-bootstrap replicates
for the 95% interval. Deltas are `reference − condition`, so positive means
degradation.

Full definitions, edge cases and the self-check matrix are in
[`../docs/EVALUATION.md`](../docs/EVALUATION.md).

## Adding a model

1. Produce predictions in the layout above for all 5,040 test samples.
2. Run `score_predictions.py` once. It writes `per_sample.jsonl` and
   `report.json`.
3. Report the `challenge_l18.primary_non_rare` and per-view `site_macro` values
   with their `ci95`.

Do not touch ground truth, and do not tune thresholds on the test split.

## Smoke tests

```bash
# ground truth as prediction: every primary metric must be 1.0
python score_predictions.py --dataset dataset --predictions /path/to/gt_preds \
    --model gt_check --out /tmp/scores --limit 40
```
