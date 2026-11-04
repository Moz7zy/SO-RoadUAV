# Evaluation protocol

The reference implementation is `evaluation/score_predictions.py` plus
`evaluation/metrics/`. It depends only on `numpy`, `opencv-python` and `scipy`.

## 1. Submission format

```
<predictions>/
├── road/<sample_id>.png    binary 0/255, exactly 1280×720
└── lane/<sample_id>.json   {"instances": [{"points_image": [[x, y], ...]}, ...]}
```

Rules:

- The manifest `dataset/manifests/subsets/test.jsonl` is the only membership
  source. Every listed `sample_id` must have a prediction; missing files are a
  hard error.
- Lane predictions have at most **10** instances per image.
- Polylines use original image coordinates (1280×720). Do not resize inputs and
  do not emit normalised coordinates.
- Extra files are ignored with a warning.

## 2. Road metrics

Let `TP/FP/FN/TN` be pixel counts of a binary prediction against
`road_mask_visible`.

| Metric | Definition |
|---|---|
| `road_iou_foreground_only_candidate` | `TP / (TP + FP + FN)` |
| `background_iou` | `TN / (TN + FP + FN)` |
| `road_miou_binary_class_macro` (**primary**) | `(road_iou + background_iou) / 2` |
| `boundary_f1` | boundary precision/recall within 3 px, harmonic mean |

Details:

- Any IoU whose union is empty returns `1.0` (empty-vs-empty is correct).
- Boundaries are extracted with a 3×3 erosion; the matching tolerance uses an
  elliptical kernel of diameter `2 * 3 + 1`; if both boundaries are empty the
  score is `1.0`, if exactly one is empty it is `0.0`.
- `prediction_foreground_pixels` is recorded per sample so degenerate
  all-background / all-foreground predictions stay visible.

## 3. Lane metrics

Let `roi = target_corridor_polygon` from the sample metadata, and let the raster
width be

```
w = round(hypot(1280, 720) * 0.01) = 15 px
```

For each GT and predicted polyline:

1. rasterise with `cv2.polylines(..., thickness=w)`;
2. intersect with the ROI;
3. drop the instance if the mask is empty.

Matching uses the Hungarian algorithm on `1 - IoU` over the GT×pred IoU matrix.
A pair counts as a true positive when `IoU >= 0.5`. Then:

```
fp = len(pred) - tp
fn = len(gt)   - tp
projected_lane_f1 = 2*tp / (2*tp + fp + fn)      # primary; 1.0 if denominator is 0
matched_projected_lane_iou = mean IoU of matched pairs
```

`empty_prediction` is true when the image produced no instances.
`valid_gt_instances` / `valid_pred_instances` record the post-ROI counts.

## 4. Aggregation

Per view, metrics are aggregated hierarchically:

1. **Anchor** — road metrics are averaged over the anchor's images; lane
   metrics pool `tp/fp/fn` across the anchor's images before computing F1.
2. **Site** — average the anchor values within each site.
3. **Site macro** — average over sites. This is the reported number.

Uncertainty: 10,000 site-level bootstrap replicates with a fixed seed, reported
as a 95% percentile interval.

Reference-relative deltas: for each anchored condition,

```
delta = reference_value - condition_value
```

so a **positive** delta means degradation and a **negative** delta means the
condition scored higher than the reference. Deltas are aggregated by site and
bootstrapped the same way.

Views:

| View | Report content |
|---|---|
| `core_scene` | one cell per scene |
| `core_observation` | one cell per `(factor, level)`, absolute and reference-relative |
| `core_axis` | one cell per axis, absolute and reference-relative |
| `challenge_l18` | `primary_non_rare` (ramp excluded), `all_53_sensitivity`, `ramp_descriptive`, `relative_non_rare`, per-row cells |

Method comparison (used when scoring several models): paired site-level
sign-flip test with 10,000 draws and Holm correction within a task
(3 road pairs, 3 lane pairs). `score_predictions.py` scores one model at a time
and does not run this test.

## 5. Command

```bash
python evaluation/score_predictions.py \
    --dataset dataset \
    --predictions ./preds \
    --model my_model \
    --out ./scores \
    [--task road|lane|both] [--limit N]
```

Outputs:

```
scores/<model>/per_sample.jsonl   identity + all metric fields
scores/<model>/report.json        view-level aggregates, CIs and deltas
```

`--limit N` scores only the first N test rows and omits the view report; use it
for a smoke test.

## 6. Self-checks

| Check | Expected |
|---|---|
| GT used as prediction | road mIoU 1.0, boundary F1 1.0, lane F1 1.0, matched IoU 1.0 |
| all-background prediction | road foreground IoU 0.0, boundary F1 0.0 |
| empty lane prediction | `tp=0`, `fp=0`, `projected_lane_f1=0.0`, `empty_prediction=true` |
| full test split | report counts 5,040 / 1,890 / 882 / 2,268 |

## 7. Known limitations

- Empty-vs-empty road IoU is defined as 1.0, so a model that predicts nothing
  still scores 1.0 on background-only images.
- `projected_lane_f1` is instance-count sensitive: one merged prediction costs
  both a false positive and a false negative.
- The ramp scene is part of `core_scene` but excluded from the
  `challenge_l18` primary aggregate because ramps are rare in the corpus.
- Lane evaluation is ROI-scoped; predictions outside the corridor are ignored
  rather than penalised.
