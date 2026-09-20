"""按闭合圆形和三角旗面＋向下旗杆提取标记，不判断平台承载或可达性。

起点圆的判定分两条路：形状类判据（`_looks_circular` / `_is_quadrilateral_outline` /
`_inside_elongated_slot`）负责否掉伪圆，`_inner_dot`（方案 A：圈里点一个点）负责
**正向**认定起点并绕过上述形状判据——单色笔下后者是唯一不会误杀合法起点的判据。
"""
import cv2
import numpy as np


def _distinct(regions):
    result = []
    for region in sorted(regions, key=lambda r: r[2] * r[3], reverse=True):
        x, y, w, h = region
        if any((abs(x + w / 2 - a - c / 2) < (w + c) / 4
                and abs(y + h / 2 - b - d / 2) < (h + d) / 4)
               or _overlap_ratio(region, (a, b, c, d)) >= .45
               for a, b, c, d in result):
            continue
        result.append(region)
    return sorted(result, key=lambda r: (r[1], r[0]))


def _looks_circular(mask, region):
    """复核霍夫候选的轮廓半径一致性，排除方框等规则噪声。"""
    x, y, w, h = region
    center_x, center_y = x + w / 2, y + h / 2
    radius = min(w, h) / 2
    distances = []
    for angle in np.linspace(0, 2 * np.pi, 72, endpoint=False):
        hits = []
        for distance in np.linspace(.4 * radius, 1.5 * radius, 33):
            sample_x = int(round(center_x + distance * np.cos(angle)))
            sample_y = int(round(center_y + distance * np.sin(angle)))
            if (0 <= sample_y < mask.shape[0] and 0 <= sample_x < mask.shape[1]
                    and mask[sample_y, sample_x]):
                hits.append(distance)
        if hits:
            distances.append(min(hits, key=lambda value: abs(value - radius)))
    coverage = len(distances) / 72
    radial_error = np.median(np.abs(np.asarray(distances) - radius)) / radius if distances else 1.0
    radial_spread = np.std(distances) / radius if distances else 1.0
    return coverage >= .78 and radial_error <= .055 and radial_spread <= .105


def _is_quadrilateral_outline(mask, region):
    """排除被霍夫圆误检的近正方形小平台或方框。"""
    x, y, width, height = region
    padding = 5
    left, top = max(0, x - padding), max(0, y - padding)
    right = min(mask.shape[1], x + width + padding)
    bottom = min(mask.shape[0], y + height + padding)
    contours, _ = cv2.findContours(mask[top:bottom, left:right], cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return False
    contour = max(contours, key=cv2.contourArea)
    perimeter = cv2.arcLength(contour, True)
    if perimeter == 0:
        return False
    polygon = cv2.approxPolyDP(contour, .035 * perimeter, True)
    _, _, contour_width, contour_height = cv2.boundingRect(contour)
    return (len(polygon) == 4 and .65 <= contour_width / max(1, contour_height) <= 1.55)


def _inside_elongated_slot(mask, region, expand=1.5, aspect=1.6, stride_limit=1.25):
    """候选中心是否落在一条「比候选本身长出很多、截面很窄」的空隙走廊里。

    平台被画成长方框时，每个端头/拐角在局部看都很像一段圆角弧：两条长边恰好落在
    候选半径附近，端弧补上其余方向，霍夫圆与轮廓复核都会接受它（实测一张手绘图上
    6 个方框端头全部被当成起点圆圈，导致 AMBIGUOUS_START）。区分依据是候选中心所在的
    **背景空隙**形状：圆环内部是近似方形的空腔，方框内部则是一条细长走廊。
    两个条件必须同时成立：只看细长会把「圆环被一条线切成两半」的半圆空腔误判成走廊。
    """
    x, y, width, height = region
    center_x, center_y = x + width // 2, y + height // 2
    left = max(0, int(round(x - expand * width)))
    top = max(0, int(round(y - expand * height)))
    right = min(mask.shape[1], int(round(x + (1 + expand) * width)))
    bottom = min(mask.shape[0], int(round(y + (1 + expand) * height)))
    window = (mask[top:bottom, left:right] == 0).astype(np.uint8)
    row, column = center_y - top, center_x - left
    if not (0 <= row < window.shape[0] and 0 <= column < window.shape[1]):
        return False
    _, labels, stats, _ = cv2.connectedComponentsWithStats(window, 4)
    label = labels[row, column]
    if label == 0:
        # 中心落在笔画上（实心标记），空隙证据不可用，交给轮廓判据。
        return False
    _, _, slot_width, slot_height, _ = (int(value) for value in stats[label])
    long_side, short_side = max(slot_width, slot_height), min(slot_width, slot_height)
    if long_side < short_side * aspect:
        return False
    stride = slot_width / max(1, width) if slot_width >= slot_height else slot_height / max(1, height)
    return stride >= stride_limit


def _minimum_marker_size(shape):
    """标记的下限尺寸（像素）：合成起点圆直径 0.07 倍画布宽，手绘圆同量级。

    自适应阈值会在空白纸纹上偶发闭合小噪点（实测 8×10 像素），轮廓复核会把它当成
    第二个圆圈，因此小于该下限的候选一律不作为标记。
    """
    return max(12, int(round(min(shape[:2]) * .022)))


def _inner_dot(mask, region, pad=3, min_ratio=.004, max_ratio=.10,
               min_cavity_ratio=.15, max_offset=.45, limit=2):
    """环内是否存在一个「与环不相连的孤立小墨点」——单色笔下的起点画法（方案 A）。

    孩子画起点时改成「画一个圈，圈里点一个点」。这样起点就有了**正向**判据：
    真圆带点时环内是「近似方形的空腔 + 中央一个小墨点」；而全部已知的伪圆家族
    （方框端头、细长槽端头、方框直角、纸纹噪点）环内都是**空**的——实测内点存在率
    0.14~0.27 vs 0.00，无一例外。因此它可以用作白名单：命中即无条件保留，不再受
    `_looks_circular` / `_is_quadrilateral_outline` / `_inside_elongated_slot` 影响
    （后两者在「细圆 + 很粗的平台线」画法下会误杀合法起点，见回归用例）。

    实现：从候选窗口四边泛洪填充背景，淹不到的背景就是**环内空腔**——这样不必先
    判断哪块墨迹是环。空腔的填洞轮廓就是环的内边界，落在它里面的墨迹只可能是内点。
    中心被切、环不闭合（如细长槽端头）时取不到空腔，直接返回 False，不擅自定义。
    """
    x, y, width, height = region
    left, top = max(0, x - pad), max(0, y - pad)
    right = min(mask.shape[1], x + width + pad)
    bottom = min(mask.shape[0], y + height + pad)
    window = mask[top:bottom, left:right]
    if window.size == 0:
        return False
    flood = (window == 0).astype(np.uint8)
    if not flood.any():
        return False
    for row, column in ((0, 0), (0, flood.shape[1] - 1),
                        (flood.shape[0] - 1, 0), (flood.shape[0] - 1, flood.shape[1] - 1)):
        if flood[row, column]:
            cv2.floodFill(flood, np.zeros((flood.shape[0] + 2, flood.shape[1] + 2), np.uint8),
                          (column, row), 2)
    cavity = ((window == 0) & (flood != 2)).astype(np.uint8)
    if not cavity.any():
        return False
    count, labels, _, centroids = cv2.connectedComponentsWithStats(cavity, 8)
    center_x, center_y = x + width // 2 - left, y + height // 2 - top
    best, best_distance = 0, None
    for label in range(1, count):
        cx, cy = centroids[label]
        distance = (cx - center_x) ** 2 + (cy - center_y) ** 2
        if best_distance is None or distance < best_distance:
            best, best_distance = label, distance
    if not best:
        return False
    contours, _ = cv2.findContours((labels == best).astype(np.uint8), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return False
    interior = np.zeros_like(cavity)
    cv2.drawContours(interior, [max(contours, key=cv2.contourArea)], -1, 1, cv2.FILLED)
    radius = min(width, height) / 2
    interior_area = int(interior.sum())
    if interior_area < np.pi * radius ** 2 * min_cavity_ratio:
        return False
    # 收 1 像素，避免把环的内边缘算成内点。
    interior = cv2.erode(interior, np.ones((3, 3), np.uint8))
    if not interior.any():
        return False
    dots = ((window > 0) & (interior > 0)).astype(np.uint8)
    found, _, dot_stats, dot_centroids = cv2.connectedComponentsWithStats(dots, 8)
    qualified = 0
    for label in range(1, found):
        if not min_ratio <= dot_stats[label, cv2.CC_STAT_AREA] / interior_area <= max_ratio:
            continue
        cx, cy = dot_centroids[label]
        if np.hypot(cx - center_x, cy - center_y) / radius > max_offset:
            continue
        qualified += 1
    return 1 <= qualified <= limit


# 笔迹相对纸面的最小局部对比度（灰阶）。实测手绘笔迹 61~168，纸纹/折痕/光照阴影
# 只有 0~18，两者相差数倍，阈值取在空档中间。
MIN_PEN_CONTRAST = 45

# 测量圆度前用来接上笔画断口的闭合核边长（像素）。只作用于圆度这一项，不动全局遮罩。
CIRCLE_BRIDGE_SIZE = 5


def _contrast_map(image):
    """每个像素相对周围背景的暗度（blackhat），用作「是不是笔迹」的判据。

    纸张纹理、折痕和照片光照阴影与纸面只差几个灰阶，手绘笔迹差几十个灰阶。
    用局部对比度而不是绝对灰度，才能不受纸面明暗和光照梯度影响——实测同一张
    带网格且光照不均的照片里，真起点圆的绝对灰度中位数会随位置漂到 200 以上，
    而局部对比度稳定在 80 左右。
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31)))


def _looks_like_pen_ink(image, mask, region, contrast=None):
    """保留黑笔或深色彩笔，排除浅色高饱和度印刷 Logo/字母与纸纹阴影。

    局部对比度是必要条件：自适应阈值会把纸纹和阴影一起抠进墨迹遮罩，这些噪声
    在饱和度上同样是低饱和（灰色），只靠下面的饱和度/灰度判据拦不住——实测
    带网格纸的一次拍摄里，纸纹噪声凑出的伪起点圆对比度仅 16.5，而真圆为 80。
    """
    x, y, w, h = region
    ink = mask[y:y + h, x:x + w] > 0
    if not ink.any():
        return False
    if contrast is None:
        contrast = _contrast_map(image)
    if np.median(contrast[y:y + h, x:x + w][ink]) < MIN_PEN_CONTRAST:
        return False
    gray = cv2.cvtColor(image[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)[ink]
    saturation = cv2.cvtColor(image[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)[:, :, 1][ink]
    return np.percentile(saturation, 75) <= 80 or np.median(gray) < 85


def _drop_border_background(mask, ratio=.01):
    """剔除贴着画布边缘的大块深色区域——那是纸外背景，不是孩子的画。

    拉正把纸张铺满画布，画布边缘朝外就是桌面；阈值化后它沿边缘形成一条面积可观
    的连通带（实测 900×560 画布上 17972 像素、占 3.6%）。纸边这条黑带本身是竖直
    长线，会被概率霍夫当成旗杆、再配上一侧墨迹凑出三角旗面（实测一次拍摄里 6 个
    终点候选有 5 个来自纸边），也是伪圆的来源。真笔迹不会与它连通，除非画到纸的
    最边缘——那本来也不可信。用「接触画布边缘＋面积超过画布 1%」双重条件，避免
    误删恰好贴边的正常笔画。
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), 8)
    edges = np.concatenate((labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]))
    touching = {int(value) for value in np.unique(edges) if value}
    keep = np.ones(count, bool)
    for label in range(1, count):
        if label in touching and stats[label, cv2.CC_STAT_AREA] >= ratio * mask.size:
            keep[label] = False
    return np.where(keep[labels], mask, 0).astype(np.uint8)


def _hough_circles(gray, mask):
    short = min(gray.shape)
    blurred = cv2.medianBlur(gray, 5)
    found = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT, 1,
                             minDist=max(25, int(short * .06)), param1=50, param2=16,
                             minRadius=max(6, int(short * .01)),
                             maxRadius=max(16, int(short * .12)))
    if found is None:
        return []
    result = []
    for x, y, radius in found[0]:
        region = (max(0, int(round(x - radius))), max(0, int(round(y - radius))),
                  int(round(2 * radius)), int(round(2 * radius)))
        if (_inner_dot(mask, region)
                or (_looks_circular(mask, region) and not _is_quadrilateral_outline(mask, region))):
            # 手绘圆紧邻平台时，底边缘会被直线干扰；留少量余量作为角色落脚点。
            region = (region[0], region[1], region[2], region[3] + max(2, int(round(radius * .15))))
            result.append(region)
    return result


def _has_triangular_face(mask, pole_x, xs, ys, pole_length):
    """验证旗杆一侧墨迹的凸包是否为有面积的三角旗面。"""
    if len(xs) < 3:
        return False
    points = np.column_stack((xs, ys)).astype(np.int32)
    points = np.vstack((points, (pole_x, int(ys.min())), (pole_x, int(ys.max()))))
    hull = cv2.convexHull(points.reshape(-1, 1, 2))
    perimeter = cv2.arcLength(hull, True)
    if perimeter <= 0:
        return False
    polygon = cv2.approxPolyDP(hull, .06 * perimeter, True)
    _, _, width, height = cv2.boundingRect(hull)
    fill_ratio = cv2.contourArea(hull) / max(1, width * height)
    if len(polygon) != 3 or height < max(8, pole_length * .15) or not .25 <= fill_ratio <= .85:
        return False
    # 用真实墨迹的闭合面积复核第三条边，避免旗杆＋单斜线被补点后的凸包伪装成三角形。
    left, right = max(0, min(int(xs.min()), pole_x) - 2), min(mask.shape[1], max(int(xs.max()), pole_x) + 3)
    top, bottom = max(0, int(ys.min()) - 2), min(mask.shape[0], int(ys.max()) + 3)
    face = mask[top:bottom, left:right]
    contours, _ = cv2.findContours(face, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return False
    contour = max(contours, key=cv2.contourArea)
    contour_hull = cv2.convexHull(contour)
    closure = cv2.contourArea(contour) / max(1.0, cv2.contourArea(contour_hull))
    return closure >= .55


def _pole_flags(mask):
    """从近竖直旗杆及其顶部单侧旗面识别实心或空心旗帜。"""
    short = min(mask.shape)
    lines = cv2.HoughLinesP(mask, 1, np.pi / 360,
                            threshold=max(12, short // 45),
                            minLineLength=max(18, int(short * .035)), maxLineGap=6)
    # 实心旗面可能让概率霍夫优先消耗横向墨迹，仅留下过短的下伸旗杆。
    # 额外竖向视图去除横向干扰；候选仍通过原角度、长度、三角面与下伸验证。
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, 9)))
    vertical_lines = cv2.HoughLinesP(vertical, 1, np.pi / 360,
                                     threshold=max(12, short // 45),
                                     minLineLength=max(18, int(short * .035)), maxLineGap=6)
    line_sets = [raw.reshape(-1, 4) for raw in (lines, vertical_lines) if raw is not None]
    if not line_sets:
        return []
    result = []
    for x1, y1, x2, y2 in np.concatenate(line_sets):
        length = float(np.hypot(x2 - x1, y2 - y1))
        angle = abs(float(np.degrees(np.arctan2(y2 - y1, x2 - x1))))
        if abs(angle - 90) > 12 or length < short * .055 or length > short * .35:
            continue
        top, bottom = min(y1, y2), max(y1, y2)
        pole_x = int(round((x1 + x2) / 2))
        reach = max(15, int(length * .8))
        y0, y1_window = max(0, int(top - length * .15)), min(mask.shape[0], int(top + length * .65))
        sides = []
        for sign in (-1, 1):
            xa, xb = sorted((pole_x + sign * 5, pole_x + sign * reach))
            xa, xb = max(0, xa), min(mask.shape[1], xb)
            ys, xs = np.nonzero(mask[y0:y1_window, xa:xb])
            absolute_x = xs + xa
            lateral = np.abs(absolute_x - pole_x)
            sides.append((len(xs), int(lateral.max()) if len(lateral) else 0,
                          absolute_x, ys + y0))
        best_index = 0 if sides[0][0] > sides[1][0] else 1
        count, extent, xs, ys = sides[best_index]
        other_count = sides[1 - best_index][0]
        if count < max(15, length * .4) or extent < max(8, length * .18):
            continue
        if count < other_count * 1.35:
            continue
        if not _has_triangular_face(mask, pole_x, xs, ys, length):
            continue
        # 旗杆必须明显伸到旗面下方；仅有三角形、方框或圆圈侧边不能成立。
        if int(bottom) - int(ys.max()) < max(8, length * .15):
            continue
        left = max(0, min(int(xs.min()), pole_x) - 4)
        right = min(mask.shape[1], max(int(xs.max()), pole_x) + 5)
        flag_top = max(0, min(int(ys.min()), int(top)) - 4)
        flag_bottom = min(mask.shape[0] - 1, int(bottom) + 3)
        result.append((int(left), int(flag_top), int(right - left), int(flag_bottom - flag_top + 1)))
    return _distinct(result)


def _contains(outer, inner, slack=8):
    """inner 是否整体落在 outer 内（允许 slack 像素溢出）。

    圆圈侧弧会被概率霍夫当成旗杆、再凑出一块伪三角旗面，这类伪旗**整体位于圆圈框内**；
    真旗帜只可能与圆圈局部相交。用它区分「伪旗」与「真旗压在圆圈上」两种情况。
    """
    x, y, w, h = inner
    a, b, c, d = outer
    return (x >= a - slack and y >= b - slack
            and x + w <= a + c + slack and y + h <= b + d + slack)


def _overlap_ratio(first, second):
    """返回交集占较小候选框的比例，用于去掉圆圈触发的伪旗杆。"""
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    width = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    height = max(0, min(ay + ah, by + bh) - max(ay, by))
    return width * height / max(1, min(aw * ah, bw * bh))


def _same_marker_shape(circle, flag, area_ratio=.5):
    """圆候选与旗候选是否在描述**同一个物体**。

    真旗帜会同时触发霍夫圆：旗面是实心三角形，而 `_looks_circular` 只考察各方向
    到候选中心的距离一致性，实心区域每个方向都能命中半径附近，因此实心旗面必然
    满足它。此时两个候选的外接框几乎重合（实测 0.87）。而圆圈侧弧凑出来的伪旗
    只取到圆的一小段弧，框明显更小（面积比远低于 .5）。用面积比把「同一个物体」
    与「物体的一部分」分开，前者判给旗帜——三角旗面验证（凸包三点＋下伸旗杆＋
    单侧墨迹占优）比圆形判据严格得多，实心三角只能靠圆形判据蒙混过关。
    """
    if not _contains(circle, flag):
        return False
    return (flag[2] * flag[3]) / max(1, circle[2] * circle[3]) >= area_ratio


def _ring_circularity(mask, region):
    """「断口接上后」的圆度：先按小核把笔画断口接上，再量外轮廓。

    手绘圆环的起止笔画端头常常没接上，圆环自带 1~3px 断口。断口留在遮罩里时，轮廓链会
    从断口扎进环内，`contourArea` 从「整圆面积」崩成「环壁薄片」，圆度随之失效——实测
    同一张图仅把降级缩放从 0.7031 换成 0.7000：轮廓面积 636→163、圆度 0.775→0.061，
    真起点直接丢失。只在这一项上接断口，避免改动全局遮罩影响旗杆/圈中点等其它判据。
    """
    x, y, w, h = region
    sub = mask[max(0, y):y + h, max(0, x):x + w]
    if sub.size == 0 or not sub.any():
        return 0.0
    bridged = cv2.morphologyEx(sub, cv2.MORPH_CLOSE,
                               np.ones((CIRCLE_BRIDGE_SIZE, CIRCLE_BRIDGE_SIZE), np.uint8))
    contours, _ = cv2.findContours(bridged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 0.0
    outer = max(contours, key=cv2.contourArea)
    perimeter = float(cv2.arcLength(outer, True))
    if perimeter <= 0:
        return 0.0
    return 4 * np.pi * abs(float(cv2.contourArea(outer))) / perimeter ** 2


def detect_markers(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 31, 7)
    # 闭运算仅连接很小的笔画缺口；RETR_LIST 同时保留空心标记内轮廓。
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    # 纸外背景必须先剔除：它沿画布边缘成带状贴在纸边，是伪旗与伪圆的主要来源。
    mask = _drop_border_background(mask)
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    circles = _hough_circles(gray, mask)
    # 霍夫圆只是待消歧候选，旗面也可能局部呈圆形；过早遮掉圆候选会连真实旗杆一起删除。
    # 两类标记先独立收集，再由下方的包含关系和重叠率统一消歧。
    flags = _pole_flags(mask)
    marker_floor = _minimum_marker_size(mask.shape)
    for contour in contours:
        area = abs(cv2.contourArea(contour))
        perimeter = cv2.arcLength(contour, True)
        x, y, w, h = cv2.boundingRect(contour)
        if (area < 35 or perimeter == 0 or min(w, h) < marker_floor
                or max(w, h) > min(image.shape[:2]) * .25):
            continue
        polygon = cv2.approxPolyDP(contour, .035 * perimeter, True)
        region = (x, y, w, h)
        if (len(polygon) >= 6 and .7 <= w / h <= 1.4
                and _ring_circularity(mask, region) >= .63
                and (_inner_dot(mask, region) or _looks_circular(mask, region))):
            circles.append(region)
    flags = [flag for flag in _distinct(flags) if flag[3] >= min(mask.shape) * .07]
    # 局部对比度只跟候选所在区域有关，整图算一次供所有候选复用。
    contrast = _contrast_map(image)
    # 旗帜同样要过笔迹门禁：纸边暗影与折痕也能凑出「旗杆＋三角面」（实测一次拍摄里
    # 6 个终点候选有 5 个是阴影，局部对比度 0~12，而真旗帜为 167）。
    flags = [flag for flag in flags if _looks_like_pen_ink(image, mask, flag, contrast)]
    # 「圈中点」（方案 A）是唯一的正向判据，命中即无条件保留：它证明孩子明确标注了
    # 起点位置，因而不再受形状类判据约束——下方两个判据在「细圆 + 粗平台线」画法下
    # 都会误杀合法起点（实测 r=12/线粗14 与 r=14/线粗14 直接判成没有起点）。
    distinct = _distinct(circles)
    dotted = {circle for circle in distinct if _inner_dot(mask, circle)}
    # 长方框的端头/拐角必须先按「中心位于细长空隙」判废：它局部看就是一段圆角弧，
    # 轮廓复核也拦不住，只有看空隙的整体形状才能区分（实测 AMBIGUOUS_START 根因）。
    verified = [circle for circle in distinct
                if circle in dotted or not _inside_elongated_slot(mask, circle)]
    # 与旗帜同框的圆必须是伪圆：实心三角旗面靠 `_looks_circular` 的半径一致性蒙混
    # 过关，两个候选描述的是同一个物体，判给证据更强的旗帜（实测 phone.jpg 的起点
    # 候选就是真旗面，导致 AMBIGUOUS_START）。
    verified = [circle for circle in verified
                if not any(_same_marker_shape(circle, flag) for flag in flags)]
    # 伪旗必须先按「整体落在圆圈框内」判废：否则下面的互斥规则会把真圆圈当成
    # 「与旗帜重叠的圆形噪声」删掉，只留下圆圈自己凑出来的假旗帜（实测 START_NOT_FOUND）。
    flags = [flag for flag in flags
             if not any(_contains(circle, flag) for circle in verified)]
    circles = [circle for circle in verified
               if (circle in dotted
                   or not any(_overlap_ratio(circle, flag) >= .25 for flag in flags))
               and _looks_like_pen_ink(image, mask, circle, contrast)]
    return circles, flags
