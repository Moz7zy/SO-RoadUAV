import cv2
import numpy as np


def _iou(pred: np.ndarray, gt: np.ndarray, class_id: int) -> float:
    p = pred == class_id
    g = gt == class_id
    union = np.logical_or(p, g).sum()
    return float(np.logical_and(p, g).sum() / union) if union else 1.0


def _boundary(mask: np.ndarray) -> np.ndarray:
    mask = mask.astype(np.uint8)
    eroded = cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=1)
    return (mask != eroded).astype(np.uint8)


def boundary_f1(pred: np.ndarray, gt: np.ndarray, tolerance_px: int) -> float:
    if tolerance_px < 0:
        raise ValueError(tolerance_px)
    pred_b, gt_b = _boundary(pred), _boundary(gt)
    pred_count, gt_count = int(pred_b.sum()), int(gt_b.sum())
    if pred_count == 0 and gt_count == 0:
        return 1.0
    if pred_count == 0 or gt_count == 0:
        return 0.0
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * tolerance_px + 1, 2 * tolerance_px + 1))
    gt_dilated = cv2.dilate(gt_b, kernel)
    pred_dilated = cv2.dilate(pred_b, kernel)
    precision = float(np.logical_and(pred_b, gt_dilated).sum() / pred_count)
    recall = float(np.logical_and(gt_b, pred_dilated).sum() / gt_count)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def evaluate_road_sample(pred: np.ndarray, gt: np.ndarray, tolerance_px: int = 3) -> dict:
    if pred.shape != gt.shape:
        raise ValueError((pred.shape, gt.shape))
    if not set(np.unique(pred)).issubset({0, 1}) or not set(np.unique(gt)).issubset({0, 1}):
        raise ValueError("road evaluator requires binary masks")
    background_iou = _iou(pred, gt, 0)
    road_iou = _iou(pred, gt, 1)
    return {
        "road_miou_binary_class_macro": (background_iou + road_iou) / 2,
        "road_iou_foreground_only_candidate": road_iou,
        "background_iou": background_iou,
        "boundary_f1": boundary_f1(pred, gt, tolerance_px),
        "boundary_tolerance_px": tolerance_px,
    }
