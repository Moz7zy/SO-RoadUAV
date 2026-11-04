from __future__ import annotations

from collections.abc import Iterable

import cv2
import numpy as np


def rasterize_polygons(polygons: Iterable, size: tuple[int, int]) -> np.ndarray:
    width, height = size
    mask = np.zeros((height, width), dtype=np.uint8)
    valid = []
    for polygon in polygons:
        points = np.asarray([[round(float(x)), round(float(y))] for x, y in polygon], dtype=np.int32)
        if len(points) >= 3 and np.isfinite(points).all():
            valid.append(points)
    if valid:
        cv2.fillPoly(mask, valid, 1)
    return mask.astype(bool)


def candidate_support_ratio(points, roi: np.ndarray, thickness: int = 5) -> float:
    support = np.zeros_like(roi, dtype=np.uint8)
    points = np.asarray([[round(float(x)), round(float(y))] for x, y in points], dtype=np.int32)
    if len(points) < 2:
        return 0.0
    cv2.polylines(support, [points], False, 1, thickness, cv2.LINE_8)
    total = int(support.sum())
    return float(np.logical_and(support.astype(bool), roi).sum() / total) if total else 0.0


def candidate_ignore_mask(candidates, roi: np.ndarray, threshold: float, thickness: int = 5) -> np.ndarray:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(threshold)
    return np.asarray(
        [candidate_support_ratio(points, roi, thickness) < threshold for points in candidates], dtype=bool
    )
