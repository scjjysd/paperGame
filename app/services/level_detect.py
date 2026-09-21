"""OpenCV platform/goal candidate detection for v1 level parsing.

Thresholds were calibrated against the fixed synthetic set and testdata/levels/golden/level1-background.png;
recalibrate after adding real photos. ponytail: v1 handles straight platforms at any slope;
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
from app.services.level_shape_rectangles import ShapeEvidence, shape_rectangles

logger = logging.getLogger(__name__)

# 坡与墙的倾角分界：|θ| <= 60° 一律识别为「坡」（哪怕陡到爬不上去，客户端会让人物滑下来），
# 更陡的交给 _walls 当竖障碍。两个分支必须严格互补，否则同一段墨迹会被同时判成坡和墙。
MAX_PLATFORM_SLOPE_DEGREES = 60.0
MAX_WALL_TILT_DEGREES = 30.0

# 细长比门限：中心线长度 / 法向厚度。外接框宽高比会随倾角天然变小，不能用作判据。
MIN_LINE_ELONGATION = 4.5

# 折线/曲线切分（_polyline_platforms）：形态学路把每个墨连通域拟合成一条直线，
# 折线与弧线会被 bbox 高度门限当「厚重墨块」剔除、被水平开运算剪碎，这里补一条多段路。
POLYLINE_EPSILON = 2.0        # RDP 容差（px），只用于找拐点
POLYLINE_MIN_SEG = 15.0       # 段最短中心线；拐角连接段也放行
POLYLINE_LONG_SEG = 40.0      # ≥它独立成立；更短的段必须与长段首尾相连才保留
POLYLINE_MAX_COL_SPAN = 40    # 单列 y 跨度超过它视为近竖直，该列不参与中心线
POLYLINE_MIN_COVERAGE = .55   # 发布前自查墨覆盖率（与 parse 几何门禁同式）
POLYLINE_MIN_RMS = 3.0        # 整线 fitLine 法向 RMS ≥ 它才算曲线/折线；直线交给形态学路

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
    evidence: ShapeEvidence | None = field(default=None, compare=False, repr=False)

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
    """霍夫候选路。当前 detect() 未调用（平台只走形态学路），保留并在改判据时保持同步。"""
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
        if (length < max(45, short * .09) or abs(angle) > MAX_PLATFORM_SLOPE_DEGREES
                or min(y1, y2) <= max(25, int(h * .15)) or max(y1, y2) >= h - 7):
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


def _centerline_segment(points, origin, unit):
    """从带圆笔帽的墨迹条带估算用户绘制的中心线端点。"""
    projections = (points - origin) @ unit
    normal = np.array([-unit[1], unit[0]])
    thickness = float(np.ptp((points - origin) @ normal))
    # OpenCV 光栅线的坐标跨度比像素宽度少约 2px；其余法向跨度对应两端圆笔帽。
    cap_extension = max(0., thickness - 2.)
    lo = float(projections.min()) + cap_extension / 2
    hi = float(projections.max()) - cap_extension / 2
    return origin + lo * unit, origin + hi * unit, max(0., hi - lo)


def _has_substantial_hole(mask):
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP,
                                           cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return False
    minimum_hole_area = mask.shape[0] * mask.shape[1] * .10
    return any(parent >= 0 and abs(cv2.contourArea(contour)) >= minimum_hole_area
               for contour, (_, _, _, parent) in zip(contours, hierarchy[0]))


def _reliable_polygon_outline(ink, gray, region):
    x, y, width, height = region
    if min(width, height) < 18:
        return False
    dark = (gray[y:y + height, x:x + width] < 60).astype(np.uint8) * 255
    if not _has_substantial_hole(dark):
        return False
    contours, _ = cv2.findContours(ink[y:y + height, x:x + width],
                                   cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return False
    contour = max(contours, key=cv2.contourArea)
    polygon = cv2.approxPolyDP(contour, .015 * cv2.arcLength(contour, True), True)[:, 0, :]
    distance = cv2.distanceTransform((dark == 0).astype(np.uint8), cv2.DIST_L2, 3)
    for start, end in zip(polygon, np.roll(polygon, -1, axis=0)):
        samples = np.rint(np.linspace(start, end, max(2, int(np.linalg.norm(end-start))))).astype(int)
        if np.mean(distance[samples[:, 1], samples[:, 0]] <= 3) < .90:
            return False
    return len(polygon) >= 3


def _has_triangular_hole(mask):
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return False
    for contour, (_, _, _, parent) in zip(contours, hierarchy[0]):
        if parent < 0 or abs(cv2.contourArea(contour)) < 35:
            continue
        polygon = cv2.approxPolyDP(contour, .04 * cv2.arcLength(contour, True), True)
        if len(polygon) == 3:
            return True
    return False


def _blocks(ink, marker_regions, image):
    """用局部对比度复核闭合/实心主体；分解凹形而不是填满外接框。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = ink.shape
    contours, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if (min(w, h) < 8 or w*h > width*height*.35 or
                x <= 5 or y <= 5 or x+w >= width-5 or y+h >= height-5):
            continue
        left, top = max(0, x-8), max(0, y-8)
        right, bottom = min(width, x+w+8), min(height, y+h+8)
        roi = gray[top:bottom, left:right]
        threshold, _ = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        if float(np.percentile(roi, 90)) - threshold < 12:
            continue
        dark = (roi <= threshold).astype(np.uint8)*255
        dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        shape = np.zeros_like(dark)
        contrast = (roi < float(np.percentile(roi, 90))-20).astype(np.uint8)*255
        distance_to_contrast = cv2.distanceTransform((contrast == 0).astype(np.uint8), cv2.DIST_L2, 3)
        masks = [cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8)) for k in (3, 7)]
        masks.append(ink[top:bottom, left:right])
        for mask_index, closed in enumerate(masks):
            cs, hierarchy = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
            if hierarchy is None:
                continue
            for c, (_, _, _, parent) in zip(cs, hierarchy[0]):
                cx, cy, cw, ch = cv2.boundingRect(c)
                region = (left+cx, top+cy, cw, ch)
                if parent < 0 or cv2.contourArea(c) < 60 or max(cw, ch) < 40:
                    continue
                if mask_index == 2:
                    polygon = cv2.approxPolyDP(c, 2, True)[:, 0, :]
                    reliable = True
                    for a, b in zip(polygon, np.roll(polygon, -1, axis=0)):
                        samples = np.rint(np.linspace(a, b, max(2, int(np.linalg.norm(b-a))))).astype(int)
                        if np.mean(distance_to_contrast[samples[:, 1], samples[:, 0]] <= 3) < .90:
                            reliable = False
                            break
                    if not reliable:
                        continue
                if any(_region_overlap(region, marker) >= .50 and
                       region[1]+region[3]*.5 < marker[1]+marker[3]*.65
                       for marker in marker_regions):
                    continue
                # 多尺度小缺口补全取并集，避免大核吞掉真实细框内部。
                cv2.drawContours(shape, [c], -1, 255, -1)
        component = np.zeros_like(dark)
        cv2.drawContours(component, [contour-np.array([[[left, top]]])], -1, 255, -1)
        solid = cv2.bitwise_and(dark, component)
        for mx, my, mw, mh in marker_regions:
            a, b = max(0, mx-left), max(0, my-top)
            c, d = min(solid.shape[1], mx+mw-left), min(solid.shape[0], my+mh-top)
            if c > a and d > b:
                solid[b:d, a:c] = 0
        depth = cv2.distanceTransform(np.pad(solid, 1), cv2.DIST_L2, 3)
        is_solid = (np.count_nonzero(solid) >= cv2.contourArea(contour)*.80 and
                    np.count_nonzero(depth >= 6) >= 35)
        if is_solid:
            shape = solid
        if shape.any():
            # 只纳入内孔附近的真实边界；旗杆/小字等附着笔画不扩张实体。
            if not is_solid:
                near = cv2.dilate(shape, np.ones((7, 7), np.uint8))
                shape = cv2.bitwise_or(shape, cv2.bitwise_and(dark, near))
        else:
            continue
        rectangles = shape_rectangles(shape, hand_drawn=True)
        if not rectangles:
            continue
        regions = tuple((left+a, top+b, rw, rh) for a, b, rw, rh in rectangles)
        evidence = ShapeEvidence(left, top, shape, dark, regions)
        for region in regions:
            found.append((region, evidence))
    found.sort(key=lambda item: (item[0][1], item[0][0], item[0][2:]))
    return [BlockCandidate(f'block_{i:03d}', RegionCandidate(*region, .85), .85, evidence)
            for i, (region, evidence) in enumerate(found, 1)]


def _is_line_allowed(start, end, marker_regions, block_regions, threshold=.45):
    if any(_line_region_overlap(start, end, region) >= threshold
           for region in marker_regions):
        return False
    if any(_line_region_overlap(start, end, region) >= .50
           for region in block_regions):
        return False
    return True


def _crosses_both_side_edges(x, width, w, margin=2):
    """连通域同时贴住左右两边。翻拍场景里只有纸张/桌面边界会这样，玩家画的线不会。"""
    return x <= margin and x + width >= w - margin


def _platforms(ink, red, image, marker_regions=(), block_regions=(),
               frame_canvas=False):
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
        if width < minimum:
            continue
        if frame_canvas:
            # 降级画布就是前端取景框：边缘是玩家可见、可画的边界，只有横跨整幅的背景
            # 边界（纸卷上沿、桌面分界）才该剔除。照搬纸张拉正那套贴边阈值会把
            # 用户画到框边的合法平台整条吃掉（job level_3f7bc3c380b8：10 条只剩 6 条）。
            if _crosses_both_side_edges(x, width, w):
                continue
        elif (y < max(25, int(h * .15)) or x <= 5
              or (x + width >= w - 5 and y < int(h * .20))):
            # 纸张拉正画布：边缘朝外是桌面与纸边阴影，贴边墨迹一律不算平台。
            continue
        ys, xs = np.nonzero(labels == label)
        points = np.column_stack((xs.astype(np.float32), ys.astype(np.float32)))
        vx, vy, x0, y0 = cv2.fitLine(points, cv2.DIST_L2, 0, .01, .01).reshape(-1)
        angle = float(np.degrees(np.arctan2(vy, vx)))
        if abs(angle) > MAX_PLATFORM_SLOPE_DEGREES:
            continue
        unit = np.array([float(vx), float(vy)])
        origin = np.array([float(x0), float(y0)])
        projections = (points - origin) @ unit
        center_a, center_b, centerline_length = _centerline_segment(points, origin, unit)
        # 笔宽估计 = 面积 / 中心线长度：任意倾角的直线都等于笔画粗细。
        # 不能用「法向跨度」——直线时它等于厚度，折线时却等于整条折线的高度，门限会失效。
        pen_width = area / max(1.0, centerline_length)
        # 外接框高度的门限必须按实测倾角折算：斜线的 bbox 高度本来就该接近 width*tan(θ)，
        # 只有超出「倾角本身 + 笔画粗细」的部分才是真正的厚重墨块（折线、阴影、纸纹）。
        maximum_height = max(35, int(h * .045),
                             int(width * abs(np.tan(np.radians(angle))))
                             + int(3 * pen_width) + 12)
        if height > maximum_height:
            continue
        # 形态学条带的端点带有半个笔画宽度，内缩后得到中心线端点。
        half_width = 1.0
        lo, hi = float(projections.min()) + half_width, float(projections.max()) - half_width
        a, b = origin + lo * unit, origin + hi * unit
        # 与 Hough 路线一致：底边 7px 属于纸边/阴影，拟合端点也不得越界。
        if max(a[1], b[1]) >= h - 7 or min(a[1], b[1]) < 0:
            continue
        length = float(np.hypot(*(b - a)))
        if centerline_length + .01 < max(55., min(h, w) * .10):
            continue
        if centerline_length < 110:
            # 短贴边墨迹在纸张拉正画布上基本都是纸边/阴影碎片（边缘朝外是桌面），
            # 所以要求离边 28px。但降级画布就是前端取景框，边缘是玩家可见、可画的
            # 边界，照搬会把画到框边的合法短平台整条吃掉（job level_671a051473cf：
            # 左下 90×41 与右下 112×15 两条真实横线被剔，而它们细长比 13.7/18.2、
            # 连续性 1.00/0.99 全部合格）。取景框下只要求不越出画布。
            safe_margin = 2 if frame_canvas else max(25, int(min(h, w) * .05))
            if (min(center_a[0], center_b[0]) < safe_margin
                    or max(center_a[0], center_b[0]) >= w - safe_margin
                    or min(center_a[1], center_b[1]) < safe_margin
                    or max(center_a[1], center_b[1]) >= h - safe_margin):
                continue
            elongation = centerline_length / max(1.0, pen_width)
            samples = max(2, int(centerline_length))
            sample_x = np.clip(np.rint(np.linspace(center_a[0], center_b[0], samples)).astype(int), 0, w - 1)
            sample_y = np.clip(np.rint(np.linspace(center_a[1], center_b[1], samples)).astype(int), 0, h - 1)
            continuity = np.mean([
                (gray[max(0, y_value - 4):min(h, y_value + 5), x_value] < 100).any()
                for x_value, y_value in zip(sample_x, sample_y)
            ])
            if elongation < MIN_LINE_ELONGATION or continuity < .70:
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


def _polyline_platforms(mask, image, marker_regions=(), frame_canvas=False, existing=()):
    """折线/曲线按拐点切分成直线段，补形态学路的漏检。

    形态学路把每个墨连通域整体拟合成一条直线：一笔折线被 bbox 高度门限
    当「厚重墨块」剔除（job level_671a051473cf 的 W 折线甚至先被水平开运算
    剪碎成 <56px 的碎片），一笔弧线同样被剔。这里在原始墨掩膜上取中心线点串，
    RDP 找拐点后逐段拟合直线，每段走与 _platforms 相同的门禁后作为独立平台发布。
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = mask.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    found = []
    for label in range(1, n):
        x, y, width, height, area = map(int, stats[label])
        if width < 80 or area < 250:
            continue
        ys, xs = np.nonzero(labels == label)
        columns = {}
        for xx, yy in zip(xs, ys):
            columns.setdefault(int(xx), []).append(int(yy))
        points = []
        for xx in sorted(columns):
            column = columns[xx]
            if max(column) - min(column) > POLYLINE_MAX_COL_SPAN:
                continue
            points.append((float(xx), float(np.median(column))))
        if len(points) < 12:
            continue
        curve = np.array(points, np.float32)
        # 整线直线度复核：纸边、装订线、桌面分界这类直线 RMS≈0，留给形态学路
        gx, gy, g0x, g0y = cv2.fitLine(curve, cv2.DIST_L2, 0, .01, .01).reshape(-1)
        normal = np.array([-float(gy), float(gx)])
        rms = float(np.sqrt(np.mean(((curve - np.array([g0x, g0y])) @ normal) ** 2)))
        if rms < POLYLINE_MIN_RMS:
            continue
        poly = cv2.approxPolyDP(curve.reshape(-1, 1, 2), POLYLINE_EPSILON, True).reshape(-1, 2)
        if len(poly) < 3:
            continue
        # RDP 只负责找拐点；每段用段内真实中心线点重新 fitLine，端点取投影极值
        vertex_index = [int(np.argmin(np.linalg.norm(curve - v, axis=1))) for v in poly[:-1]]
        bounds = vertex_index + [len(points) - 1]
        segments = []
        for si in range(len(bounds) - 1):
            i0, i1 = bounds[si], bounds[si + 1]
            if i1 - i0 < 3:
                continue
            sub = curve[i0:i1 + 1]
            vx, vy, x0, y0 = cv2.fitLine(sub, cv2.DIST_L2, 0, .01, .01).reshape(-1)
            if abs(float(vy)) > abs(float(vx)) * 2.0:
                continue  # 段内近竖直，交给 _walls
            unit = np.array([float(vx), float(vy)])
            origin = np.array([float(x0), float(y0)])
            projections = (sub - origin) @ unit
            a = origin + float(projections.min()) * unit
            b = origin + float(projections.max()) * unit
            length = float(np.hypot(b[0] - a[0], b[1] - a[1]))
            if length < POLYLINE_MIN_SEG:
                continue
            angle = float(np.degrees(np.arctan2(b[1] - a[1], b[0] - a[0])))
            if abs(angle) > MAX_PLATFORM_SLOPE_DEGREES:
                continue
            # 与 _platforms 一致的边界门禁
            if frame_canvas:
                if _crosses_both_side_edges(min(a[0], b[0]), abs(b[0] - a[0]), w):
                    continue
            elif (min(a[1], b[1]) < max(25, int(h * .15)) or min(a[0], b[0]) <= 5
                    or (max(a[0], b[0]) >= w - 5 and min(a[1], b[1]) < int(h * .20))):
                continue
            if max(a[1], b[1]) >= h - 7 or min(a[1], b[1]) < 0:
                continue
            if not _is_line_allowed(a, b, marker_regions, ()):
                continue
            # 与形态学路已发布的候选重复（段中点落在既有线上 <10px）则跳过
            mid = np.array([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2])
            duplicate = False
            for pc in existing:
                sx, sy = float(pc.start.x), float(pc.start.y)
                ex_, ey_ = float(pc.end.x) - sx, float(pc.end.y) - sy
                denom = ex_ * ex_ + ey_ * ey_
                t = 0.0 if denom == 0 else max(0.0, min(
                    1.0, ((mid[0] - sx) * ex_ + (mid[1] - sy) * ey_) / denom))
                if float(np.hypot(mid[0] - (sx + t * ex_), mid[1] - (sy + t * ey_))) < 10:
                    duplicate = True
                    break
            if duplicate:
                continue
            # 发布前自查墨覆盖率（与 parse 的 _coverage 同式 ±1px），不达标不发布，
            # 否则整图会被 parse 的 PLATFORM_GEOMETRY_AMBIGUOUS 门禁拦成复核
            samples = max(2, int(length))
            sample_x = np.clip(np.rint(np.linspace(a[0], b[0], samples)).astype(int), 0, w - 1)
            sample_y = np.clip(np.rint(np.linspace(a[1], b[1], samples)).astype(int), 0, h - 1)
            hits = [mask[max(0, yy - 1):min(h, yy + 2), max(0, xx - 1):min(w, xx + 2)].any()
                    for xx, yy in zip(sample_x, sample_y)]
            if float(np.mean(hits)) < POLYLINE_MIN_COVERAGE:
                continue
            segments.append((a, b, angle, length))

        def _linked_short(seg, others):
            for other in others:
                if min(np.hypot(seg[0][0] - other[0][0], seg[0][1] - other[0][1]),
                       np.hypot(seg[0][0] - other[1][0], seg[0][1] - other[1][1]),
                       np.hypot(seg[1][0] - other[0][0], seg[1][1] - other[0][1]),
                       np.hypot(seg[1][0] - other[1][0], seg[1][1] - other[1][1])) < 8:
                    return True
            return False

        # 孤立短段剔除：短段只有与长段首尾相连（拐角连接段）才保留
        long_segments = [seg for seg in segments if seg[3] >= POLYLINE_LONG_SEG]
        segments = [seg for seg in segments
                    if seg[3] >= POLYLINE_LONG_SEG or _linked_short(seg, long_segments)]
        for a, b, angle, length in segments:
            confidence = min(.99, .68 + min(.29, length / max(w, 1) * .25))
            found.append(PlatformCandidate('', PointCandidate(round(float(a[0])), round(float(a[1]))),
                                           PointCandidate(round(float(b[0])), round(float(b[1]))),
                                           confidence, angle))
    return found


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
        x, y, width, height, area = map(int, stats[label])
        if height < minimum:
            continue
        if y < max(25, int(h * .15)) or x <= 5 or x + width >= w - 5 or y + height >= h - 7:
            continue
        ys, xs = np.nonzero(labels == label)
        points = np.column_stack((xs.astype(np.float32), ys.astype(np.float32)))
        vx, vy, x0, y0 = cv2.fitLine(points, cv2.DIST_L2, 0, .01, .01).reshape(-1)
        angle = float(np.degrees(np.arctan2(vy, vx)))
        # 与 _platforms 严格互补：坡取 |θ| <= 60°，墙取 |θ| > 60°，同一段墨迹只会进一个分支。
        if abs(abs(angle) - 90) >= MAX_WALL_TILT_DEGREES:
            continue
        unit = np.array([float(vx), float(vy)])
        origin = np.array([float(x0), float(y0)])
        projections = (points - origin) @ unit
        center_a, center_b, centerline_length = _centerline_segment(points, origin, unit)
        pen_width = area / max(1.0, centerline_length)
        # 与外接框高度同理：竖线的 bbox 宽度本来就该接近 height/tan(θ)，倾斜不能算厚重。
        sine = max(1e-6, abs(np.sin(np.radians(angle))))
        expected_width = int(height * abs(np.cos(np.radians(angle))) / sine) + int(3 * pen_width) + 12
        maximum_width = max(35, int(w * .045), expected_width)
        if width > maximum_width:
            continue
        lo, hi = float(projections.min()) + 1., float(projections.max()) - 1.
        a, b = origin + lo * unit, origin + hi * unit
        length = float(np.hypot(*(b - a)))
        if centerline_length + .01 < max(55., min(h, w) * .10):
            continue
        if centerline_length < 110:
            safe_margin = max(25, int(min(h, w) * .05))
            if (min(center_a[0], center_b[0]) < safe_margin
                    or max(center_a[0], center_b[0]) >= w - safe_margin
                    or min(center_a[1], center_b[1]) < safe_margin
                    or max(center_a[1], center_b[1]) >= h - safe_margin):
                continue
            elongation = centerline_length / max(1.0, pen_width)
            samples = max(2, int(centerline_length))
            sample_x = np.clip(np.rint(np.linspace(center_a[0], center_b[0], samples)).astype(int), 0, w - 1)
            sample_y = np.clip(np.rint(np.linspace(center_a[1], center_b[1], samples)).astype(int), 0, h - 1)
            continuity = np.mean([
                (gray[y_value, max(0, x_value - 4):min(w, x_value + 5)] < 100).any()
                for x_value, y_value in zip(sample_x, sample_y)
            ])
            if elongation < MIN_LINE_ELONGATION or continuity < .70:
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


def detect(rectified_path: Path, job_dir: Path, *,
           frame_canvas: bool = False) -> DetectionResult:
    """frame_canvas=True 表示画布来自 normalize_visible_canvas 的取景框降级，
    边缘是玩家可见范围而非纸外背景，平台侧改用「横跨整幅」判据剔除背景边界。"""
    image = cv2.imread(str(rectified_path), cv2.IMREAD_COLOR)
    if image is None: raise ValueError(f'无法读取拉正图：{rectified_path}')
    job_dir = Path(job_dir); job_dir.mkdir(parents=True, exist_ok=True)
    ink = _ink_mask(image); red = _red_mask(image)
    path = job_dir / 'ink-mask.png'; tmp = path.with_suffix('.tmp.png')
    if not cv2.imwrite(str(tmp), ink): raise OSError(f'无法写入墨迹遮罩：{tmp}')
    tmp.replace(path)
    circles, flags = detect_markers(image)
    contours, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    closed_polygons = []
    for contour in contours:
        region = cv2.boundingRect(contour)
        x, y, width, height = region
        # 三角内孔可能属于带下伸旗杆的真实旗帜，不能用闭合证据否决 marker。
        if (_reliable_polygon_outline(ink, gray, region)
                and not _has_triangular_hole(ink[y:y + height, x:x + width])):
            closed_polygons.append(region)
    # 闭合多边形的短边可能触发伪旗杆；可靠闭合边界不是三角旗。
    flags = [flag for flag in flags
             if not any(_region_overlap(flag, region) >= .80
                        for region in closed_polygons)]
    marker_regions = [*circles, *flags]
    blocks = _blocks(ink, marker_regions, image)
    line_ink = ink.copy()
    for block in blocks:
        evidence = block.evidence
        if evidence is not None:
            eh, ew = evidence.mask.shape
            roi = line_ink[evidence.y:evidence.y+eh, evidence.x:evidence.x+ew]
            roi[cv2.dilate(evidence.mask, np.ones((17, 17), np.uint8)) > 0] = 0
    platforms = _platforms(line_ink, red, image, marker_regions,
                           frame_canvas=frame_canvas)
    # 折线/曲线多段路：形态学路只认「一笔一条直线」，这里按拐点切分补漏
    polyline = _polyline_platforms(line_ink, image, marker_regions,
                                   frame_canvas=frame_canvas, existing=platforms)
    if polyline:
        platforms = platforms + polyline
        platforms = [PlatformCandidate(f'line_{i:03d}', p.start, p.end,
                                       p.confidence, p.angle_degrees)
                     for i, p in enumerate(platforms, 1)]
    walls = _walls(line_ink, image, marker_regions)
    goals = [GoalCandidate(f'goal_{i:03d}', RegionCandidate(x, y, w, h, .9), .9)
             for i, (x, y, w, h) in enumerate(flags, 1)]
    starts = [PointCandidate(x + w // 2, y + h - 1, .9) for x, y, w, h in circles]
    logger.info('关卡候选检测完成：平台 %d 条、墙 %d 条、实体 %d 个、终点 %d 个（图片 %s）',
                len(platforms), len(walls), len(blocks), len(goals), rectified_path)
    return DetectionResult(platforms, goals, path, starts, walls, blocks)
