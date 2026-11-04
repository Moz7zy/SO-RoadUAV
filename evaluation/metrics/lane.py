import math

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment

from .constants import ORIGINAL_SIZE
from .roi import rasterize_polygons


def lane_width_px(size=ORIGINAL_SIZE) -> int:
    return int(round(math.hypot(*size) * 0.01))


def rasterize_lane(points, roi: np.ndarray, thickness: int) -> np.ndarray:
    height, width = roi.shape
    mask = np.zeros((height, width), dtype=np.uint8)
    points = np.asarray([[round(float(x)), round(float(y))] for x, y in points], dtype=np.int32)
    if len(points) >= 2:
        cv2.polylines(mask, [points], False, 1, thickness, cv2.LINE_8)
    return np.logical_and(mask.astype(bool), roi)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    union = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / union) if union else 0.0


def evaluate_lane_sample(
    gt_lanes,
    pred_lanes,
    roi_polygons,
    *,
    size=ORIGINAL_SIZE,
    match_iou_threshold: float = 0.5,
) -> dict:
    roi = rasterize_polygons(roi_polygons, size)
    thickness = lane_width_px(size)
    gt_masks = [rasterize_lane(lane, roi, thickness) for lane in gt_lanes]
    pred_masks = [rasterize_lane(lane, roi, thickness) for lane in pred_lanes]
    gt_masks = [mask for mask in gt_masks if mask.any()]
    pred_masks = [mask for mask in pred_masks if mask.any()]
    matrix = np.zeros((len(gt_masks), len(pred_masks)), dtype=np.float64)
    for i, gt_mask in enumerate(gt_masks):
        for j, pred_mask in enumerate(pred_masks):
            matrix[i, j] = _iou(gt_mask, pred_mask)
    matched = []
    if matrix.size:
        rows, columns = linear_sum_assignment(1.0 - matrix)
        matched = [float(matrix[i, j]) for i, j in zip(rows, columns) if matrix[i, j] >= match_iou_threshold]
    tp = len(matched)
    fp = len(pred_masks) - tp
    fn = len(gt_masks) - tp
    denominator = 2 * tp + fp + fn
    f1 = 2 * tp / denominator if denominator else 1.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "projected_lane_f1": f1,
        "matched_projected_lane_iou": float(np.mean(matched)) if matched else 0.0,
        "matched_ious": matched,
        "valid_gt_instances": len(gt_masks),
        "valid_pred_instances": len(pred_masks),
        "raster_width_px": thickness,
        "raster_width_rule": "round(0.01 * original_image_diagonal)",
        "match_iou_threshold": match_iou_threshold,
        "roi_pixels": int(roi.sum()),
    }
