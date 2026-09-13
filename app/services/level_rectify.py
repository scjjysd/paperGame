"""OpenCV 纸张检测、透视拉正与方向归一化。"""
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Sequence, Tuple

import cv2
import numpy as np


Point = Tuple[float, float]
_GRABCUT_LOCK = threading.Lock()


class RectifyIssue(RuntimeError):
    """纸张拉正无法安全完成时抛出的业务异常。"""

    def __init__(self, reason: str, candidates: Sequence[Any] = (), message: str = '') -> None:
        self.reason = reason
        self.candidates = list(candidates)
        super().__init__(message or reason)


@dataclass
class RectifyResult:
    rectified_path: Path
    width: int
    height: int
    corners: List[List[float]]
    matrix: List[List[float]]


def order_corners(points: np.ndarray) -> np.ndarray:
    """按左上、右上、右下、左下返回四个角点。"""
    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    sums = pts.sum(axis=1)
    diffs = np.diff(pts, axis=1).ravel()
    ordered = np.empty((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(sums)]
    ordered[2] = pts[np.argmax(sums)]
    ordered[1] = pts[np.argmin(diffs)]
    ordered[3] = pts[np.argmax(diffs)]
    return ordered


def _issue_candidates(contours: Sequence[np.ndarray]) -> List[dict]:
    result = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        x, y, w, h = cv2.boundingRect(contour)
        result.append({'area': round(area, 2), 'region': {'x': x, 'y': y, 'width': w, 'height': h}})
    return result


def _edge_based_candidates(image: np.ndarray, image_area: float, scale: float):
    """在单一尺度提取有闭合边界证据的四边形，返回原图坐标。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # 轻度模糊去噪，但保留纸张边缘的微弱梯度
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 10, 30)
    # 闭运算连接断裂的边缘，形成纸张轮廓
    kernel = np.ones((5, 5), np.uint8)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < image_area * 0.25:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if ((x <= 2 and x + w >= image.shape[1] - 2)
                or (y <= 2 and y + h >= image.shape[0] - 2)):
            continue
        hull = cv2.convexHull(contour)
        perimeter = cv2.arcLength(hull, True)
        if perimeter <= 0:
            continue
        polygon = cv2.approxPolyDP(hull, 0.02 * perimeter, True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        # 不把任意弯曲/不规则轮廓强行套成外接矩形，否则会把桌面当纸张。
        if area / max(cv2.contourArea(hull), 1.0) < 0.90:
            continue
        points = polygon.reshape(-1, 2).astype(np.float32)
        if scale < 1:
            points /= scale
        candidates.append(points)
    return candidates


def _multiscale_edge_candidates(image: np.ndarray) -> List[np.ndarray]:
    """优先在低分辨率连接弱边缘，失败后用较高分辨率补充细节。"""
    height, width = image.shape[:2]
    previous_size = None
    for max_side in (640, 1280):
        scale = min(1.0, max_side / max(height, width))
        size = (int(round(width * scale)), int(round(height * scale)))
        if size == previous_size:
            continue
        previous_size = size
        small = cv2.resize(image, size, interpolation=cv2.INTER_AREA) if scale < 1 else image
        # 显式使用实际宽高比，避免整数取整造成坐标映射误差。
        candidates = _edge_based_candidates(small, float(size[0] * size[1]), 1.0)
        if candidates:
            factors = np.array([width / size[0], height / size[1]], dtype=np.float32)
            return [points * factors for points in candidates]
    return []


def _foreground_candidates(image: np.ndarray) -> List[np.ndarray]:
    """用颜色/纹理前景分割恢复受不均匀光照影响的整张纸轮廓。"""
    height, width = image.shape[:2]
    scale = min(1.0, 960.0 / max(height, width))
    size = (int(round(width * scale)), int(round(height * scale)))
    small = cv2.resize(image, size, interpolation=cv2.INTER_AREA) if scale < 1 else image
    h, w = small.shape[:2]
    pad = max(12, int(min(h, w) * .04))
    mask = np.zeros((h, w), np.uint8)
    background = np.zeros((1, 65), np.float64)
    foreground = np.zeros((1, 65), np.float64)
    try:
        # OpenCV 随机种子是进程全局状态；锁住初始化与分割，保证并发任务结果稳定。
        with _GRABCUT_LOCK:
            cv2.setRNGSeed(0)
            cv2.grabCut(small, mask, (pad, pad, w - 2 * pad, h - 2 * pad),
                        background, foreground, 3, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return []
    binary = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8), iterations=2)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    result = []
    for contour in contours:
        if cv2.contourArea(contour) < h * w * .25:
            continue
        hull = cv2.convexHull(contour)
        perimeter = cv2.arcLength(hull, True)
        polygon = cv2.approxPolyDP(hull, .02 * perimeter, True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        factors = np.array([width / w, height / h], dtype=np.float32)
        result.append(polygon.reshape(4, 2).astype(np.float32) * factors)
    return result


def _aspect_ratio(points: np.ndarray) -> float:
    corners = order_corners(points)
    widths = (np.linalg.norm(corners[1] - corners[0]) + np.linalg.norm(corners[2] - corners[3])) / 2
    heights = (np.linalg.norm(corners[3] - corners[0]) + np.linalg.norm(corners[2] - corners[1])) / 2
    return max(widths, heights) / max(min(widths, heights), 1.0)


def _reasonable_foreground(candidate: np.ndarray, original: np.ndarray,
                           image_shape: Tuple[int, int]) -> bool:
    """限制 GrabCut 兜底候选，避免把整张桌面或其他大矩形替换成纸张。"""
    height, width = image_shape
    ordered = order_corners(candidate)
    area = abs(float(cv2.contourArea(ordered.reshape(-1, 1, 2))))
    if not 1.1 <= _aspect_ratio(ordered) <= 1.9:
        return False
    x, y, box_width, box_height = cv2.boundingRect(ordered.astype(np.float32))
    if ((x <= 2 and x + box_width >= width - 2)
            or (y <= 2 and y + box_height >= height - 2)):
        return False
    if not .25 <= area / float(width * height) <= .92:
        return False
    overlap, _ = cv2.intersectConvexConvex(order_corners(original), ordered)
    original_area = abs(float(cv2.contourArea(order_corners(original).reshape(-1, 1, 2))))
    return float(overlap) / max(original_area, 1.0) >= .65


def _paper_candidates(image: np.ndarray) -> Tuple[List[np.ndarray], np.ndarray]:
    height, width = image.shape[:2]
    scale = min(1.0, 1600.0 / max(height, width))
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    # CLAHE 均衡局部光照：真实拍照常一侧亮一侧暗，直接阈值会丢失暗区纸张。
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    # Otsu 自动找纸张/背景最佳分割点，替代固定阈值 200；
    # 均匀暗图（无纸张）Otsu 会退回 0，此时用固定阈值 200 兜底确保空图被拒绝。
    otsu_thresh, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if otsu_thresh < 150:
        _, mask = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8), iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    image_area = float(small.shape[0] * small.shape[1])
    candidates = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < image_area * 0.25:
            continue
        # 真实纸张在透视下不会紧贴图像左右边界；
        # 候选水平跨度覆盖整图宽度说明包含了背景（Otsu 过分割），拒绝。
        x, _, w, _ = cv2.boundingRect(contour)
        if x <= 2 and x + w >= small.shape[1] - 2:
            continue
        hull = cv2.convexHull(contour)
        perimeter = cv2.arcLength(hull, True)
        if perimeter <= 0:
            continue
        polygon = cv2.approxPolyDP(hull, 0.02 * perimeter, True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        points = polygon.reshape(4, 2).astype(np.float32)
        if scale < 1:
            points /= scale
        candidates.append(points)
    # 暗图回退可能得到空 mask；边缘路线不依赖亮度分割的覆盖率。
    if not candidates:
        candidates = _multiscale_edge_candidates(image)
    # A4 纸被亮度梯度横向截断时会形成异常扁长四边形；GrabCut 用整体颜色和纹理恢复边界。
    if candidates and _aspect_ratio(max(candidates, key=lambda c: cv2.contourArea(c.reshape(-1, 1, 2)))) > 1.9:
        foreground = _foreground_candidates(image)
        if foreground:
            current = max(candidates, key=lambda c: cv2.contourArea(c.reshape(-1, 1, 2)))
            foreground = [candidate for candidate in foreground
                          if _reasonable_foreground(candidate, current, image.shape[:2])]
            current_area = cv2.contourArea(current.reshape(-1, 1, 2))
            foreground_area = max((cv2.contourArea(c.reshape(-1, 1, 2)) for c in foreground), default=0)
            if foreground_area > current_area * 1.15:
                candidates = foreground
    return candidates, mask


def _atomic_imwrite(path: Path, image: np.ndarray) -> None:
    temporary = path.with_name(path.stem + '.tmp' + path.suffix)
    try:
        if not cv2.imwrite(str(temporary), image):
            raise OSError('failed to write {}'.format(path.name))
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.stem + '.tmp' + path.suffix)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_mask(job_dir: Path, image_shape: Tuple[int, int], corners: np.ndarray) -> None:
    mask = np.zeros(image_shape, dtype=np.uint8)
    cv2.fillConvexPoly(mask, np.round(corners).astype(np.int32), 255)
    _atomic_imwrite(job_dir / 'paper-mask.png', mask)


def _is_occluded(corners: np.ndarray, image_shape: Tuple[int, int]) -> bool:
    """保守拒绝明显残片；v1 不把正常透视造成的边长差误判为遮挡。"""
    ordered = order_corners(corners)
    area = abs(float(cv2.contourArea(ordered.reshape(-1, 1, 2))))
    image_area = float(image_shape[0] * image_shape[1])
    return area < image_area * 0.25


def rectify(input_path: Path, job_dir: Path) -> RectifyResult:
    """检测单张近矩形纸并生成 rectified.png、paper-mask.png、transform.json。"""
    image = cv2.imread(str(input_path), cv2.IMREAD_COLOR)
    if image is None:
        raise RectifyIssue('PAPER_NOT_FOUND', message='无法读取输入图片。')
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    candidates, paper_mask = _paper_candidates(image)
    if not candidates:
        bright_ratio = float(np.count_nonzero(paper_mask)) / float(paper_mask.size)
        if bright_ratio >= 0.10:
            raise RectifyIssue('PAPER_OCCLUDED', message='纸张疑似被遮挡或截断。')
        raise RectifyIssue('PAPER_NOT_FOUND', message='未检测到纸张。')
    if len(candidates) > 1:
        areas = sorted(((abs(float(cv2.contourArea(c.reshape(-1, 1, 2)))), index, c) for index, c in enumerate(candidates)), reverse=True)
        areas = [(area, c) for area, _, c in areas]
        if areas[1][0] / max(areas[0][0], 1.0) >= 0.75:
            raise RectifyIssue('PAPER_AMBIGUOUS', _issue_candidates([c.reshape(-1, 1, 2) for c in candidates]))
        candidates = [areas[0][1]]
    corners = order_corners(candidates[0])
    if _is_occluded(corners, image.shape[:2]):
        raise RectifyIssue('PAPER_OCCLUDED', _issue_candidates([corners.reshape(-1, 1, 2)]), '纸张疑似被遮挡或截断。')

    measured_width = max(np.linalg.norm(corners[1] - corners[0]), np.linalg.norm(corners[2] - corners[3]))
    measured_height = max(np.linalg.norm(corners[3] - corners[0]), np.linalg.norm(corners[2] - corners[1]))
    # 合成/黄金样本使用 900x560 画布；拉正输出统一为该 v1 坐标画布，避免拍摄尺度影响关卡坐标。
    aspect = 900.0 / 560.0
    long_side = 900
    width, height = (long_side, int(round(long_side / aspect))) if measured_width >= measured_height else (int(round(long_side / aspect)), long_side)
    if min(width, height) < 2:
        raise RectifyIssue('PAPER_NOT_FOUND')
    if abs(float(measured_width) - float(measured_height)) / max(float(measured_width), float(measured_height)) < 0.08:
        raise RectifyIssue('ORIENTATION_AMBIGUOUS', _issue_candidates([corners.reshape(-1, 1, 2)]), '纸张接近正方形，方向证据不足。')

    # 长边归一为横向。竖拍时直接改变源角点到目标角点的对应关系，
    # 避免先拉正再旋转导致矩阵和最终产物坐标系不一致。
    target_w, target_h = 900, 560
    if measured_width >= measured_height:
        destination = np.array([
            [0, 0], [target_w - 1, 0],
            [target_w - 1, target_h - 1], [0, target_h - 1],
        ], dtype=np.float32)
    else:
        destination = np.array([
            [target_w - 1, 0], [target_w - 1, target_h - 1],
            [0, target_h - 1], [0, 0],
        ], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(corners, destination)
    warped = cv2.warpPerspective(image, matrix, (target_w, target_h), flags=cv2.INTER_LINEAR)
    rectified_path = job_dir / 'rectified.png'
    _atomic_imwrite(rectified_path, warped)
    _write_mask(job_dir, image.shape[:2], corners)
    transform = {'matrix': matrix.tolist(), 'inputSize': {'width': int(image.shape[1]), 'height': int(image.shape[0])}, 'outputSize': {'width': target_w, 'height': target_h}}
    _atomic_json(job_dir / 'transform.json', transform)
    return RectifyResult(rectified_path, target_w, target_h, corners.tolist(), matrix.tolist())
