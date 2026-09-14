"""按闭合圆形和三角旗面＋向下旗杆提取标记，不判断平台承载或可达性。"""
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


def _looks_like_pen_ink(image, mask, region):
    """保留黑笔或深色彩笔，排除浅色高饱和度印刷 Logo/字母。"""
    x, y, w, h = region
    ink = mask[y:y + h, x:x + w] > 0
    if not ink.any():
        return False
    gray = cv2.cvtColor(image[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)[ink]
    saturation = cv2.cvtColor(image[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)[:, :, 1][ink]
    return np.percentile(saturation, 75) <= 80 or np.median(gray) < 85


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
        if _looks_circular(mask, region):
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
    if lines is None:
        return []
    result = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
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


def _overlap_ratio(first, second):
    """返回交集占较小候选框的比例，用于去掉圆圈触发的伪旗杆。"""
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    width = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    height = max(0, min(ay + ah, by + bh) - max(ay, by))
    return width * height / max(1, min(aw * ah, bw * bh))


def detect_markers(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 31, 7)
    # 闭运算仅连接很小的笔画缺口；RETR_LIST 同时保留空心标记内轮廓。
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    circles, flags = _hough_circles(gray, mask), _pole_flags(mask)
    for contour in contours:
        area = abs(cv2.contourArea(contour))
        perimeter = cv2.arcLength(contour, True)
        x, y, w, h = cv2.boundingRect(contour)
        if area < 35 or perimeter == 0 or min(w, h) < 8 or max(w, h) > min(image.shape[:2]) * .25:
            continue
        polygon = cv2.approxPolyDP(contour, .035 * perimeter, True)
        circularity = 4 * np.pi * area / perimeter ** 2
        region = (x, y, w, h)
        if (len(polygon) >= 6 and .7 <= w / h <= 1.4 and circularity >= .63
                and _looks_circular(mask, region)):
            circles.append(region)
    flags = [flag for flag in _distinct(flags)
             if min(mask.shape) * .07 <= flag[3] <= min(mask.shape) * .15]
    circles = [circle for circle in _distinct(circles)
               if not any(_overlap_ratio(circle, flag) >= .25 for flag in flags)
               and _looks_like_pen_ink(image, mask, circle)]
    return circles, flags
