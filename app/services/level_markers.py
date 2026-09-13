"""按闭合圆形和三角旗面＋向下旗杆提取标记，不判断平台承载或可达性。"""
import cv2
import numpy as np


def _distinct(regions):
    result = []
    for region in sorted(regions, key=lambda r: r[2] * r[3], reverse=True):
        x, y, w, h = region
        if any(abs(x + w / 2 - a - c / 2) < (w + c) / 4
               and abs(y + h / 2 - b - d / 2) < (h + d) / 4
               for a, b, c, d in result):
            continue
        result.append(region)
    return sorted(result, key=lambda r: (r[1], r[0]))


def detect_markers(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 31, 7)
    # 闭运算仅连接很小的笔画缺口；RETR_LIST 同时保留空心标记内轮廓。
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    circles, flags = [], []
    for contour in contours:
        area = abs(cv2.contourArea(contour))
        perimeter = cv2.arcLength(contour, True)
        x, y, w, h = cv2.boundingRect(contour)
        if area < 35 or perimeter == 0 or min(w, h) < 8 or max(w, h) > min(image.shape[:2]) * .25:
            continue
        polygon = cv2.approxPolyDP(contour, .035 * perimeter, True)
        circularity = 4 * np.pi * area / perimeter ** 2
        if len(polygon) >= 6 and .7 <= w / h <= 1.4 and circularity >= .78:
            circles.append((x, y, w, h))
        triangle = cv2.approxPolyDP(cv2.convexHull(contour), .05 * cv2.arcLength(cv2.convexHull(contour), True), True)
        if len(triangle) != 3 or area / (w * h) < .25:
            continue
        points = triangle[:, 0, :]
        for i in range(3):
            a, b = points[i], points[(i + 1) % 3]
            if abs(int(a[0]) - int(b[0])) > max(6, h * .3) or abs(int(a[1]) - int(b[1])) < h * .55:
                continue
            lower = a if a[1] > b[1] else b
            # 旗杆应从旗面下端继续向下延伸，允许少量歪斜及 3px 断笔。
            bottom, gaps = int(lower[1]), 0
            for row in range(bottom + 1, min(mask.shape[0], bottom + max(30, 3 * h))):
                pole_x = int(round(lower[0] + (row - lower[1]) * (float(b[0] - a[0]) / float(b[1] - a[1]))))
                if pole_x < 0 or pole_x >= mask.shape[1]:
                    break
                if mask[row, max(0, pole_x - 4):min(mask.shape[1], pole_x + 5)].any():
                    bottom, gaps = row, 0
                else:
                    gaps += 1
                    if gaps > 3:
                        break
            if bottom - lower[1] >= max(9, h * .35):
                # 空心旗面通常取到内轮廓，补回边缘笔画且限制在画布内。
                left, top = max(0, x - 5), max(0, y - 5)
                right = min(mask.shape[1], x + w + 5)
                flags.append((left, top, right - left, bottom - top + 1))
                break
    return _distinct(circles), _distinct(flags)
