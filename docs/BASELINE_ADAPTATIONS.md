# Baseline adaptations

This repository does not ship baseline model code, weights or result tables.
This document records what a reproduction of the six reference models needs to
know. All adaptations live on the benchmark side: **no third-party model source
was modified** except for the compatibility shims listed below.

## 1. Lane instance capacity

Lane instance capacity is unified at **10** per image and is applied **after**
ROI clipping. The original CondLaneNet pre-ROI slice was removed so that every
model sees the same instance budget.

## 2. ROI participates in evaluation

A candidate polyline whose support ratio inside `target_corridor_polygon` is
below **0.5** (measured on a 5 px raster) is ignored. The ROI is an evaluation
and post-processing boundary, not a loss term for CondLaneNet.

## 3. Road models are binary

SegFormer-B0, Mask2Former-R50 and YOLO26s-semantic are trained with two classes
(road / background). Mask2Former is built with `NUM_CLASSES = 2`.

## 4. Working resolutions

| Task | Working size | Metric resolution |
|---|---|---|
| road | 1024 × 576 | 1280 × 720 |
| lane | 800 × 448 | 1280 × 720 |

Logits are bilinearly resized back to 1280×720 before metrics are computed.
Predictions are saved at 1280×720.

## 5. LaneATT anchor frequency

LaneATT's anchor frequency prior is recomputed from this benchmark's **train**
split. The CULane prior is not used.

## 6. CondLaneNet variant

The reference model is the mmLaneDet CondLaneNet-R18 implementation. It requires
an out-of-place `AttentionLayer.forward` patch; the patch id is recorded in
`docs/PROVENANCE.md`. Forward equivalence against the unpatched layer was
verified numerically.

## 7. CLRNet compatibility

Two changes: an `np.bool` compatibility shim for newer NumPy, and training in
FP32 without gradient clipping.

## 8. Ground truth to native targets

| Model | Target encoding |
|---|---|
| LaneATT-R18 | 72 rows, x-offset regression, output stride 32 |
| CLRNet-R18 | 72 rows, x-offset regression, `cut_height=0`, `max_lanes=10`, 5 px auxiliary segmentation, output stride 8 |
| CondLaneNet-R18 | endpoint heatmap at stride 16 (50×28), location/offset/range masks at stride 8, endpoint Gaussian radius 2 cells |

The 72 rows linearly cover `y = 448 … 0` in the 800×448 working image.
The ROI does not enter the CondLaneNet loss; it is applied in post-processing
only.

## 9. Augmentation

Policy `V3-BASELINE-SYNC-AUG.1`, applied identically to road and lane inputs:

| Transform | Value |
|---|---|
| horizontal flip | probability 0.5 |
| brightness | uniform ±0.2 |
| contrast | uniform ±0.2 |
| saturation | uniform ±0.2 |
| random crop | none |
| perspective | none |

## 10. Training budget

| Group | Epochs | Steps/epoch | Batch | Accumulation | Effective batch |
|---|---:|---:|---:|---:|---:|
| SegFormer-B0 | 80 | 750 | 4 | 4 | 16 |
| Mask2Former-R50 | 80 | 750 | 2 | 8 | 16 |
| YOLO26s-semantic | 80 | 750 | 8 | 2 | 16 |
| LaneATT-R18 | 20 | 375 | 16 | 2 | 32 |
| CondLaneNet-R18 | 20 | 375 | 16 | 2 | 32 |
| CLRNet-R18 | 20 | 375 | 16 | 2 | 32 |

LR schedules differ per model (linear warmup + decay for SegFormer, cosine for
Mask2Former/CLRNet, staged decay for CondLaneNet, 3-epoch warmup + 80-epoch
cosine for YOLO).

## 11. Thresholds

- Road: a single `road_probability_threshold`.
- Lane: confidence threshold plus ROI support threshold.

Both are selected by a grid search on **val** only. The test split uses the
frozen values; no test-time tuning is permitted.
