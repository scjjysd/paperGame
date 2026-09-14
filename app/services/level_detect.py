"""OpenCV platform/goal candidate detection for v1 level parsing.

Thresholds were calibrated against the fixed synthetic set and testdata/levels/golden/level1-background.png;
recalibrate after adding real photos. ponytail: v1 handles near-horizontal straight platforms;
curves should move to a contour-polyline model.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Sequence, Tuple
import cv2
import numpy as np
from app.services.level_markers import detect_markers

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class PointCandidate:
    x: int
    y: int
    confidence: float = 0.0

@dataclass(frozen=True)
class RegionCandidate:
    x: int
    y: int
    width: int
    height: int
    confidence: float

@dataclass(frozen=True)
class PlatformCandidate:
    id: str
    start: PointCandidate
    end: PointCandidate
    confidence: float
    angle_degrees: float = 0.0
    @property
    def length(self) -> float:
        return float(np.hypot(self.end.x - self.start.x, self.end.y - self.start.y))

@dataclass(frozen=True)
class GoalCandidate:
    id: str
    region: RegionCandidate
    confidence: float

@dataclass(frozen=True)
class BlockCandidate:
    id: str
    region: RegionCandidate
    confidence: float

@dataclass(frozen=True)
class DetectionResult:
    platform_candidates: List[PlatformCandidate]
    goal_candidates: List[GoalCandidate]
    ink_mask_path: Path
    start_candidates: List[PointCandidate] = field(default_factory=list)
    wall_candidates: List[PlatformCandidate] = field(default_factory=list)
    block_candidates: List[BlockCandidate] = field(default_factory=list)


def _ink_mask(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    black_hat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT,
        cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31)))
    black = (black_hat >= 10).astype(np.uint8) * 255
    adaptive = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                      cv2.THRESH_BINARY_INV, 31, 7)
    return cv2.morphologyEx(cv2.bitwise_or(black, adaptive), cv2.MORPH_OPEN,
                            np.ones((2, 2), np.uint8))


def _red_mask(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 120, 70]), np.array([12, 255, 255]))
    mask = cv2.bitwise_or(mask, cv2.inRange(hsv, np.array([170, 120, 70]),
                                             np.array([180, 255, 255])))
    blue, green, red_channel = cv2.split(image)
    mask[(red_channel.astype(np.int16) < green.astype(np.int16) * 3 // 2) |
         (red_channel.astype(np.int16) < blue.astype(np.int16) * 3 // 2)] = 0
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))


def _raw_lines(mask: np.ndarray):
    h, w = mask.shape
    short = min(h, w)
    edges = cv2.Canny(mask, 40, 120)
    raw = cv2.HoughLinesP(edges, 1, np.pi / 1800,
        threshold=max(18, short // 18), minLineLength=max(45, int(short * .09)),
        maxLineGap=max(8, int(short * .025)))
    if raw is None:
        return []
    result = []
    for x1, y1, x2, y2 in raw[:, 0, :]:
        dx, dy = float(x2 - x1), float(y2 - y1)
        length = float(np.hypot(dx, dy))
        angle = float(np.degrees(np.arctan2(dy, dx)))
        if dx < 0:
            x1, y1, x2, y2, angle = x2, y2, x1, y1, -angle
        if length < max(45, short * .09) or abs(angle) > 6 or min(y1, y2) <= max(25, int(h * .15)) or max(y1, y2) >= h - 7:
            continue
        xs = np.linspace(x1, x2, max(2, int(length))).round().astype(int)
        ys = np.linspace(y1, y2, max(2, int(length))).round().astype(int)
        covered = []
        for x, y in zip(xs, ys):
            covered.append(mask[max(0, y-3):min(h, y+4), max(0, x-3):min(w, x+4)].any())
        if np.mean(covered) >= .55:
            result.append((float(x1), float(y1), float(x2), float(y2), length, angle))
    return result


def _merge_lines(lines):
    groups = []
    for line in sorted(lines, key=lambda z: (z[1] + z[3], z[0])):
        x1, y1, x2, y2, length, angle = line
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        for group in groups:
            ref = group[0]
            rx1, ry1, rx2, ry2 = ref[:4]
            if abs(angle - ref[5]) <= 4:
                # 端点相距超过一个窄缺口时不要把同一高度的平行平台合并。
                gap = max(0.0, max(rx1, x1) - min(rx2, x2))
                if gap > 20:
                    continue
                ref_y = ry1 + (mx-rx1) * (ry2-ry1) / max(1., rx2-rx1)
                if abs(my-ref_y) <= 14:
                    group.append(line)
                    break
        else:
            groups.append([line])
    merged = []
    for group in groups:
        angle = float(np.average([z[5] for z in group], weights=[z[4] for z in group]))
        unit = np.array([np.cos(np.radians(angle)), np.sin(np.radians(angle))])
        origin = np.array(group[0][:2])
        points = [np.array(p) for z in group for p in ((z[0], z[1]), (z[2], z[3]))]
        projections = [(float((p-origin) @ unit), p) for p in points]
        start, end = min(projections, key=lambda z: z[0])[1], max(projections, key=lambda z: z[0])[1]
        merged.append((start[0], start[1], end[0], end[1], angle))
    return merged


def _refine(line, mask):
    x1, y1, x2, y2, angle = line
    h, w = mask.shape
    unit = np.array([np.cos(np.radians(angle)), np.sin(np.radians(angle))])
    normal = np.array([-unit[1], unit[0]])
    center = np.array([(x1+x2)/2, (y1+y2)/2])
    half = max(10., np.hypot(x2-x1, y2-y1)/2 + 50)
    ys, xs = np.nonzero(mask)
    rel = np.column_stack((xs, ys)) - center
    across = rel @ normal
    along = rel @ unit
    chosen = (np.abs(across) <= 6) & (np.abs(along) <= half + 12)
    if chosen.sum() < 8:
        return line
    center = center + float(np.median(across[chosen])) * normal
    rel = np.column_stack((xs, ys)) - center
    along = rel @ unit
    chosen = (np.abs(rel @ normal) <= 6) & (np.abs(along) <= half + 12)
    cross_values = (rel @ normal)[chosen]
    half_width = min(7.0, max(1.0, (float(np.percentile(cross_values, 95)) - float(np.percentile(cross_values, 5))) / 2.0))
    lo, hi = float(along[chosen].min()) + half_width, float(along[chosen].max()) - half_width
    a, b = center + lo*unit, center + hi*unit
    return (float(a[0]), float(a[1]), float(b[0]), float(b[1]), angle)


def _refine_endpoint_x(candidate, origin, unit, normal, projections, gray):
    """用原图深色笔画收回形态学条带被阴影扩大的端点。"""
    lo, hi = int(np.floor(projections.min())), int(np.ceil(projections.max()))
    support = []
    for projection in range(lo, hi + 1):
        point = origin + projection * unit
        xs = np.clip(np.rint(point[0] + np.arange(-4, 5) * normal[0]).astype(int),
                     0, gray.shape[1] - 1)
        ys = np.clip(np.rint(point[1] + np.arange(-4, 5) * normal[1]).astype(int),
                     0, gray.shape[0] - 1)
        # 60 在固定合成集和 golden 实测可排除纸张阴影，同时保留铅笔平台；
        # 真实拍摄样本加入后须重标定。ponytail: v1 只按灰度判定，升级时结合光照归一化。
        if (gray[ys, xs] < 60).any():
            support.append(projection)
    if not support:
        return candidate
    left, right = origin + min(support) * unit, origin + max(support) * unit
    start, end = candidate.start, candidate.end
    # 仅接受明显但有限的内缩：小量是像素锯齿，大量通常是平台内的断笔。
    if 5 <= left[0] - start.x <= 30:
        start = PointCandidate(round(float(left[0])), start.y, start.confidence)
    if 5 <= end.x - right[0] <= 30:
        end = PointCandidate(round(float(right[0])), end.y, end.confidence)
    return PlatformCandidate(candidate.id, start, end, candidate.confidence,
                             candidate.angle_degrees)


def _region_overlap(first, second):
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    width = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    height = max(0, min(ay + ah, by + bh) - max(ay, by))
    return width * height / max(1, aw * ah)


def _line_region_overlap(start, end, region):
    length = max(2, int(np.hypot(end[0] - start[0], end[1] - start[1])))
    xs = np.linspace(start[0], end[0], length)
    ys = np.linspace(start[1], end[1], length)
    x, y, width, height = region
    return float(np.mean((xs >= x) & (xs <= x + width) &
                         (ys >= y) & (ys <= y + height)))


def _blocks(ink, marker_regions):
    h, w = ink.shape
    contours, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    min_dimension = max(18, int(min(h, w) * .03))
    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        area = abs(cv2.contourArea(contour))
        if perimeter <= 0 or area < min_dimension ** 2:
            continue
        x, y, width, height = cv2.boundingRect(contour)
        region = (x, y, width, height)
        if min(width, height) < min_dimension or width * height > w * h * .35:
            continue
        if x <= 5 or y <= 5 or x + width >= w - 5 or y + height >= h - 5:
            continue
        polygon = cv2.approxPolyDP(contour, .025 * perimeter, True)
        if len(polygon) < 4 or not cv2.isContourConvex(cv2.convexHull(polygon)):
            continue
        fill_ratio = min(1., area / max(1, width * height))
        # 外轮廓面积会包含空心图形内部，因此闭合方框接近 1；旗杆与平台相交形成的
        # 开放细线轮廓则很低，不能把整组线条误当成一个实体。
        if fill_ratio < .35:
            continue
        if any(_region_overlap(region, marker) >= .25 for marker in marker_regions):
            continue
        confidence = min(.97, .72 + .20 * fill_ratio)
        found.append((region, confidence))
    found.sort(key=lambda item: (item[0][1], item[0][0]))
    return [BlockCandidate(f'block_{index:03d}',
                           RegionCandidate(*region, confidence), confidence)
            for index, (region, confidence) in enumerate(found, 1)]


def _is_line_allowed(start, end, marker_regions, block_regions, threshold=.45):
    if any(_line_region_overlap(start, end, region) >= threshold
           for region in marker_regions):
        return False
    if any(_line_region_overlap(start, end, region) >= .50
           for region in block_regions):
        return False
    return True


def _platforms(ink, red, image, marker_regions=(), block_regions=()):
    mask = ink.copy(); mask[red > 0] = 0
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # 水平形态学开运算将局部纹理压成连通平台条带；15px 闭运算只连接小断裂，
    # 不跨越黄金样本中约 40px 的同高独立平台。阈值依据合成与黄金样本标定。
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (15, 1)))
    horizontal = cv2.morphologyEx(horizontal, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (9, 1)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(horizontal, 8)
    h, w = mask.shape
    found = []
    for label in range(1, n):
        x, y, width, height, area = map(int, stats[label])
        minimum = max(55, int(min(h, w) * .10))
        if width < minimum or height > max(35, int(h * .045)):
            continue
        if y < max(25, int(h * .15)) or x <= 5 or (x + width >= w - 5 and y < int(h * .20)):
            continue
        ys, xs = np.nonzero(labels == label)
        points = np.column_stack((xs.astype(np.float32), ys.astype(np.float32)))
        vx, vy, x0, y0 = cv2.fitLine(points, cv2.DIST_L2, 0, .01, .01).reshape(-1)
        angle = float(np.degrees(np.arctan2(vy, vx)))
        if abs(angle) > 6:
            continue
        unit = np.array([float(vx), float(vy)])
        origin = np.array([float(x0), float(y0)])
        projections = (points - origin) @ unit
        # 形态学条带的端点带有半个笔画宽度，内缩后得到中心线端点。
        half_width = 1.0
        lo, hi = float(projections.min()) + half_width, float(projections.max()) - half_width
        a, b = origin + lo * unit, origin + hi * unit
        # 与 Hough 路线一致：底边 7px 属于纸边/阴影，拟合端点也不得越界。
        if max(a[1], b[1]) >= h - 7 or min(a[1], b[1]) < 0:
            continue
        length = float(np.hypot(*(b - a)))
        if length < max(55., min(h, w) * .10):
            continue
        if length < 110:
            safe_margin = max(25, int(min(h, w) * .05))
            if (min(a[0], b[0]) < safe_margin or max(a[0], b[0]) >= w - safe_margin
                    or min(a[1], b[1]) < safe_margin or max(a[1], b[1]) >= h - safe_margin):
                continue
            aspect_ratio = width / max(1, height)
            samples = max(2, int(length))
            sample_x = np.clip(np.rint(np.linspace(a[0], b[0], samples)).astype(int), 0, w - 1)
            sample_y = np.clip(np.rint(np.linspace(a[1], b[1], samples)).astype(int), 0, h - 1)
            continuity = np.mean([
                (gray[max(0, y_value - 4):min(h, y_value + 5), x_value] < 100).any()
                for x_value, y_value in zip(sample_x, sample_y)
            ])
            if aspect_ratio < 4.5 or continuity < .70:
                continue
        if not _is_line_allowed(a, b, marker_regions, block_regions):
            continue
        confidence = min(.99, .68 + min(.29, length / max(w, 1) * .25))
        candidate = PlatformCandidate('', PointCandidate(round(float(a[0])), round(float(a[1]))),
                                      PointCandidate(round(float(b[0])), round(float(b[1]))),
                                      confidence, angle)
        candidate = _refine_endpoint_x(candidate, origin, unit, np.array([-unit[1], unit[0]]),
                                       projections, gray)
        found.append((float(a[1] + b[1]), float(a[0]), float(b[0]), candidate))
    # 合并同一平台的重叠/小缺口碎片；gap > 20px 保留为独立平行平台。
    merged = []
    for item in sorted(found, key=lambda z: (z[0], z[1])):
        if merged:
            previous = merged[-1]
            py, px1, px2, pp = previous
            _, x1, x2, p = item
            gap = max(0.0, max(px1, x1) - min(px2, x2))
            if abs(item[0] - py) <= 10 and gap <= 20 and abs(p.angle_degrees - pp.angle_degrees) <= 4:
                start = pp.start if pp.start.x <= p.start.x else p.start
                end = pp.end if pp.end.x >= p.end.x else p.end
                merged[-1] = ((py + item[0]) / 2, min(px1, x1), max(px2, x2),
                              PlatformCandidate('', start, end, max(pp.confidence, p.confidence),
                                                (pp.angle_degrees + p.angle_degrees) / 2))
                continue
        merged.append(item)
    merged.sort(key=lambda z: (z[0], z[1], z[2]))
    return [PlatformCandidate(f'line_{i:03d}', p.start, p.end, p.confidence, p.angle_degrees)
            for i, (_, _, _, p) in enumerate(merged, 1)]


def _walls(ink, image, marker_regions=(), block_regions=()):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    vertical = cv2.morphologyEx(ink, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, 15)))
    vertical = cv2.morphologyEx(vertical, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, 9)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(vertical, 8)
    h, w = ink.shape
    minimum = max(55, int(min(h, w) * .10))
    found = []
    for label in range(1, n):
        x, y, width, height, _ = map(int, stats[label])
        if height < minimum or width > max(35, int(w * .045)):
            continue
        if y < max(25, int(h * .15)) or x <= 5 or x + width >= w - 5 or y + height >= h - 7:
            continue
        ys, xs = np.nonzero(labels == label)
        points = np.column_stack((xs.astype(np.float32), ys.astype(np.float32)))
        vx, vy, x0, y0 = cv2.fitLine(points, cv2.DIST_L2, 0, .01, .01).reshape(-1)
        angle = float(np.degrees(np.arctan2(vy, vx)))
        if abs(abs(angle) - 90) > 6:
            continue
        unit = np.array([float(vx), float(vy)])
        origin = np.array([float(x0), float(y0)])
        projections = (points - origin) @ unit
        lo, hi = float(projections.min()) + 1., float(projections.max()) - 1.
        a, b = origin + lo * unit, origin + hi * unit
        length = float(np.hypot(*(b - a)))
        if length < max(55., min(h, w) * .10):
            continue
        if length < 110:
            aspect_ratio = height / max(1, width)
            samples = max(2, int(length))
            sample_x = np.clip(np.rint(np.linspace(a[0], b[0], samples)).astype(int), 0, w - 1)
            sample_y = np.clip(np.rint(np.linspace(a[1], b[1], samples)).astype(int), 0, h - 1)
            continuity = np.mean([
                (gray[y_value, max(0, x_value - 4):min(w, x_value + 5)] < 100).any()
                for x_value, y_value in zip(sample_x, sample_y)
            ])
            if aspect_ratio < 4.5 or continuity < .70:
                continue
        if not _is_line_allowed(a, b, marker_regions, block_regions):
            continue
        if a[1] > b[1]:
            a, b = b, a
        confidence = min(.99, .68 + min(.29, length / max(h, 1) * .25))
        found.append((float(a[0] + b[0]), float(a[1]), float(b[1]),
                      PlatformCandidate('',
                                        PointCandidate(round(float(a[0])), round(float(a[1]))),
                                        PointCandidate(round(float(b[0])), round(float(b[1]))),
                                        confidence, angle)))
    found.sort(key=lambda item: (item[0], item[1], item[2]))
    return [PlatformCandidate(f'wall_{index:03d}', candidate.start, candidate.end,
                              candidate.confidence, candidate.angle_degrees)
            for index, (_, _, _, candidate) in enumerate(found, 1)]


def detect(rectified_path: Path, job_dir: Path) -> DetectionResult:
    image = cv2.imread(str(rectified_path), cv2.IMREAD_COLOR)
    if image is None: raise ValueError(f'无法读取拉正图：{rectified_path}')
    job_dir = Path(job_dir); job_dir.mkdir(parents=True, exist_ok=True)
    ink = _ink_mask(image); red = _red_mask(image)
    path = job_dir / 'ink-mask.png'; tmp = path.with_suffix('.tmp.png')
    if not cv2.imwrite(str(tmp), ink): raise OSError(f'无法写入墨迹遮罩：{tmp}')
    tmp.replace(path)
    circles, flags = detect_markers(image)
    marker_regions = [*circles, *flags]
    blocks = _blocks(ink, marker_regions)
    block_regions = [(block.region.x, block.region.y, block.region.width, block.region.height)
                     for block in blocks]
    platforms = _platforms(ink, red, image, marker_regions, block_regions)
    walls = _walls(ink, image, marker_regions, block_regions)
    goals = [GoalCandidate(f'goal_{i:03d}', RegionCandidate(x, y, w, h, .9), .9)
             for i, (x, y, w, h) in enumerate(flags, 1)]
    starts = [PointCandidate(x + w // 2, y + h - 1, .9) for x, y, w, h in circles]
    logger.info('关卡候选检测完成：平台 %d 条、墙 %d 条、实体 %d 个、终点 %d 个（图片 %s）',
                len(platforms), len(walls), len(blocks), len(goals), rectified_path)
    return DetectionResult(platforms, goals, path, starts, walls, blocks)
