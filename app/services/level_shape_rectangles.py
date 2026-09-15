"""保留凹角的形状掩码分解；数量超限时只允许有界边界近似。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import cv2
import numpy as np

Rectangle = Tuple[int, int, int, int]
MAX_SHAPE_RECTANGLES = 32
BOUNDARY_TOLERANCE = 3
MAX_AXIS_ADJUSTMENT = 12


def _simplify_hand_drawn(mask: np.ndarray, tolerance: int) -> np.ndarray:
    """去除圆笔帽，并将近水平/竖直边按中线拉齐；保留凹角。"""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    simplified = np.zeros_like(mask)
    for contour in contours:
        epsilon = max(tolerance, min(8., cv2.arcLength(contour, True)*.015))
        polygon = cv2.approxPolyDP(contour, epsilon, True)[:, 0, :].astype(float)
        if len(polygon) < 3:
            continue
        for axis in (0, 1):
            groups = [{i} for i in range(len(polygon))]
            for i in range(len(polygon)):
                j = (i+1) % len(polygon)
                delta = np.abs(polygon[j]-polygon[i])
                # 少量倾斜最多修正 12px，不将斜多边形或 L 形拉成大框。
                if delta[axis] <= MAX_AXIS_ADJUSTMENT*2 and delta[axis] <= delta[1-axis]*.35:
                    a = next(g for g in groups if i in g)
                    b = next(g for g in groups if j in g)
                    if a is not b:
                        a.update(b)
                        groups.remove(b)
            for group in groups:
                indices = list(group)
                polygon[indices, axis] = np.mean(polygon[indices, axis])
        cv2.fillPoly(simplified, [np.rint(polygon).astype(np.int32)], 255)
    return simplified


@dataclass(frozen=True)
class ShapeEvidence:
    x: int
    y: int
    mask: np.ndarray
    source_ink: np.ndarray
    rectangles: Tuple[Rectangle, ...]

    def supports(self, rectangle: Rectangle) -> bool:
        """允许内部拆分矩形没有轮廓墨迹，但必须来自有墨迹的已验证主体。"""
        if rectangle not in self.rectangles or not self.source_ink.any():
            return False
        x, y, width, height = rectangle
        x -= self.x
        y -= self.y
        if x < 0 or y < 0 or x + width > self.mask.shape[1] or y + height > self.mask.shape[0]:
            return False
        roi = self.mask[y:y + height, x:x + width]
        distance = cv2.distanceTransform((self.mask == 0).astype(np.uint8), cv2.DIST_L2, 3)
        return (np.mean(roi > 0) >= .60 and
                float(distance[y:y+height, x:x+width].max()) <= MAX_AXIS_ADJUSTMENT)


def _scan_rows(mask: np.ndarray, step: int = 1) -> list[Rectangle]:
    active = {}
    rectangles = []
    for y in range(0, mask.shape[0], step):
        bottom = min(mask.shape[0], y + step)
        # 粗化时取交集，不能在台阶凹角之外增加实心面积。
        row = np.all(mask[y:bottom] > 0, axis=0).astype(np.int8)
        edges = np.diff(np.pad(row, (1, 1)))
        intervals = list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))
        current = {}
        for left, right in intervals:
            key = (int(left), int(right))
            top = active.pop(key, y)
            current[key] = top
        for (left, right), top in active.items():
            rectangles.append((left, top, right - left, y - top))
        active = current
    for (left, right), top in active.items():
        rectangles.append((left, top, right - left, mask.shape[0] - top))
    return sorted(rectangles, key=lambda r: (r[1], r[0], r[2], r[3]))


def shape_rectangles(mask: np.ndarray, max_rectangles: int = MAX_SHAPE_RECTANGLES,
                     boundary_tolerance: int = BOUNDARY_TOLERANCE,
                     hand_drawn: bool = False) -> list[Rectangle]:
    """输入单个可信填充形状；不允许外接矩形填满较大的凹空白。"""
    if mask.ndim != 2 or max_rectangles <= 0 or not mask.any():
        return []
    source = (mask > 0).astype(np.uint8) * 255
    x, y, width, height = cv2.boundingRect(source)
    crop = source[y:y + height, x:x + width]
    # 仅在每个外接框空白像素距离主体都很近时合并手绘长方形的小倾斜。
    if np.mean(crop > 0) >= .80:
        distance = cv2.distanceTransform((crop == 0).astype(np.uint8), cv2.DIST_L2, 3)
        if float(distance.max()) <= boundary_tolerance:
            return [(x, y, width, height)]
    raw = _scan_rows(source)
    if not hand_drawn and len(raw) <= max_rectangles:
        return raw
    simplified = _simplify_hand_drawn(source, boundary_tolerance)
    original_area = np.count_nonzero(source)
    if np.count_nonzero(simplified) < original_area*(.80 if hand_drawn else .85):
        simplified = source
    exact = _scan_rows(simplified)
    if len(exact) <= max_rectangles:
        return exact
    # 受限轮廓简化去除笔画锯齿，不改变凹形拓扑。
    # 检查简化增添的每个像素确实位于允许的边界误差内。
    outside_distance = cv2.distanceTransform((source == 0).astype(np.uint8), cv2.DIST_L2, 3)
    simplified[outside_distance > max(boundary_tolerance, MAX_AXIS_ADJUSTMENT)] = 0
    for step in range(1, boundary_tolerance * 2 + 2):
        rectangles = _scan_rows(simplified, step)
        area = sum(w * h for _, _, w, h in rectangles)
        if len(rectangles) <= max_rectangles and area >= original_area * .85:
            return rectangles
    return []
