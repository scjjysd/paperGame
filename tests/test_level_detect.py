from itertools import permutations
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.services.level_detect import detect


def _write_image(path: Path, size=(420, 300), lines=(), flags=(), circles=(),
                 rectangles=(), polygons=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = np.full((size[1], size[0], 3), 245, dtype=np.uint8)
    for x1, y1, x2, y2, width in lines:
        cv2.line(image, (x1, y1), (x2, y2), (25, 25, 25), width)
    for x, y, w, h in flags:
        cv2.line(image, (x, y), (x, y + h), (30, 30, 30), 3)
        cv2.polylines(image, [np.array([(x, y), (x + w, y + h // 5), (x, y + h // 2)], dtype=np.int32)], True, (0, 0, 255), 3)
    for x, y, radius, width in circles:
        cv2.circle(image, (x, y), radius, (25, 25, 25), width)
    for x1, y1, x2, y2, width in rectangles:
        cv2.rectangle(image, (x1, y1), (x2, y2), (25, 25, 25), width)
    for polygon in polygons:
        points, filled = polygon[:2]
        pen_width = polygon[2] if len(polygon) > 2 else 5
        contour = np.asarray(points, dtype=np.int32)
        if filled:
            cv2.fillPoly(image, [contour], (25, 25, 25))
        else:
            cv2.polylines(image, [contour], True, (25, 25, 25), pen_width)
    cv2.imwrite(str(path), image)


def _endpoint_error(candidate, truth):
    direct = np.hypot(candidate.start.x - truth['x1'], candidate.start.y - truth['y1'])
    direct += np.hypot(candidate.end.x - truth['x2'], candidate.end.y - truth['y2'])
    reverse = np.hypot(candidate.start.x - truth['x2'], candidate.start.y - truth['y2'])
    reverse += np.hypot(candidate.end.x - truth['x1'], candidate.end.y - truth['y1'])
    return min(direct, reverse) / 2


def _assert_polygon_coverage(result, points, size=(900, 560)):
    truth = np.zeros((size[1], size[0]), np.uint8)
    cv2.fillPoly(truth, [np.asarray(points, np.int32)], 255)
    actual = np.zeros_like(truth)
    for block in result.block_candidates:
        r = block.region
        actual[r.y:r.y+r.height, r.x:r.x+r.width] = 255
    assert 1 <= len(result.block_candidates) <= 32
    assert np.count_nonzero((actual > 0) & (truth > 0)) >= np.count_nonzero(truth)*.80
    allowed = cv2.dilate(truth, np.ones((25, 25), np.uint8))
    assert not np.any((actual > 0) & (allowed == 0))


@pytest.mark.synthetic
def test_golden_c1levels_detects_seven_platforms_with_endpoint_accuracy(tmp_path):
    root = Path(__file__).resolve().parents[1]
    image = root / 'testdata' / 'levels' / 'golden' / 'level1-background.png'
    truth = json.loads((root / 'testdata' / 'levels' / 'golden' / 'level1.json').read_text(encoding='utf-8'))

    result = detect(image, tmp_path)
    candidates = result.platform_candidates
    assert len(candidates) == 7

    assignment = min(permutations(candidates), key=lambda ordered: sum(
        _endpoint_error(candidate, expected)
        for candidate, expected in zip(ordered, truth['platforms'])))
    errors = [_endpoint_error(candidate, expected)
              for candidate, expected in zip(assignment, truth['platforms'])]
    short_side = min(truth['canvas']['width'], truth['canvas']['height'])
    assert np.median(errors) <= max(3, short_side * .004)
    assert np.percentile(errors, 95) <= max(8, short_side * .01)


def test_detects_constructed_platform_endpoints(tmp_path):
    image = tmp_path / 'rectified.png'
    expected = [(40, 70, 170, 70), (230, 140, 370, 142), (60, 230, 350, 227)]
    _write_image(image, lines=[(*line, 7) for line in expected])

    result = detect(image, tmp_path)

    assert len(result.platform_candidates) == 3
    actual = sorted(result.platform_candidates, key=lambda p: p.start.y)
    for candidate, truth in zip(actual, expected):
        assert abs(candidate.start.x - truth[0]) <= 3
        assert abs(candidate.start.y - truth[1]) <= 3
        assert abs(candidate.end.x - truth[2]) <= 3
        assert abs(candidate.end.y - truth[3]) <= 3
    assert (tmp_path / 'ink-mask.png').exists()


def test_merges_collinear_fragments_but_not_parallel_lines(tmp_path):
    image = tmp_path / 'fragments.png'
    _write_image(image, lines=[(30, 80, 130, 80, 7), (137, 81, 260, 80, 7), (40, 120, 240, 126, 7)])

    result = detect(image, tmp_path)

    assert len(result.platform_candidates) == 2
    assert max(p.end.x for p in result.platform_candidates) >= 255


def test_preserves_small_slope_instead_of_flattening(tmp_path):
    image = tmp_path / 'slope.png'
    _write_image(image, lines=[(40, 100, 350, 108, 7)])

    result = detect(image, tmp_path)

    candidate = max(result.platform_candidates, key=lambda p: p.end.x - p.start.x)
    assert candidate.end.y - candidate.start.y >= 5


def test_rejects_page_edge_shadow_and_short_text_strokes(tmp_path):
    image = tmp_path / 'noise.png'
    _write_image(image, size=(420, 300), lines=[(2, 2, 418, 2, 20), (100, 180, 130, 180, 3), (50, 220, 330, 220, 7)])

    result = detect(image, tmp_path)

    assert len(result.platform_candidates) == 1
    assert result.platform_candidates[0].start.y > 210


def test_detects_single_red_flag_candidate(tmp_path):
    image = tmp_path / 'flag.png'
    _write_image(image, lines=[(40, 250, 360, 250, 7)], flags=[(300, 70, 55, 80)])

    result = detect(image, tmp_path)

    assert len(result.goal_candidates) == 1
    goal = result.goal_candidates[0]
    assert goal.id == 'goal_001'
    assert abs(goal.region.x - 300) <= 6 and abs(goal.region.y - 70) <= 6
    assert goal.region.x + goal.region.width >= 345
    assert goal.region.y + goal.region.height >= 150
    assert all(abs(p.start.x - 327) > 4 or abs(p.start.y - 70) > 4 for p in result.platform_candidates)


def test_multiple_flags_remain_separate_candidates(tmp_path):
    image = tmp_path / 'flags.png'
    _write_image(image, lines=[(30, 260, 390, 260, 7)], flags=[(55, 70, 35, 60), (300, 100, 45, 70)])

    first = detect(image, tmp_path / 'first')
    second = detect(image, tmp_path / 'second')

    assert [g.id for g in first.goal_candidates] == ['goal_001', 'goal_002']
    assert [(g.region.x, g.region.y) for g in first.goal_candidates] == [(g.region.x, g.region.y) for g in second.goal_candidates]


def test_detects_seventy_pixel_short_horizontal_platform(tmp_path):
    image = tmp_path / 'short-platform.png'
    _write_image(image, size=(900, 560), lines=[(120, 180, 190, 180, 7)])

    result = detect(image, tmp_path)

    assert len(result.platform_candidates) == 1
    assert result.platform_candidates[0].length >= 65


def test_detects_eighty_pixel_short_vertical_wall(tmp_path):
    image = tmp_path / 'short-wall.png'
    _write_image(image, size=(900, 560), lines=[(220, 150, 220, 230, 7)])

    result = detect(image, tmp_path)

    assert len(result.wall_candidates) == 1
    assert result.wall_candidates[0].length >= 75


def test_detects_hollow_rectangle_as_block(tmp_path):
    image = tmp_path / 'hollow-block.png'
    _write_image(image, size=(900, 560), rectangles=[(260, 160, 380, 260, 7)])

    result = detect(image, tmp_path)

    assert len(result.block_candidates) == 1
    region = result.block_candidates[0].region
    assert abs(region.x - 260) <= 5 and abs(region.y - 160) <= 5
    assert abs(region.width - 121) <= 8 and abs(region.height - 101) <= 8


def test_detects_filled_non_triangle_polygon_as_block(tmp_path):
    image = tmp_path / 'solid-block.png'
    points = [(470, 170), (540, 145), (610, 190), (585, 270), (495, 260)]
    _write_image(image, size=(900, 560), polygons=[(points, True)])

    result = detect(image, tmp_path)

    _assert_polygon_coverage(result, points)


def test_circle_marker_is_not_geometry(tmp_path):
    image = tmp_path / 'circle.png'
    _write_image(image, size=(900, 560), circles=[(240, 190, 30, 6)])

    result = detect(image, tmp_path)

    assert len(result.start_candidates) == 1
    assert result.block_candidates == []
    assert result.platform_candidates == []
    assert result.wall_candidates == []


def test_flag_pole_is_not_wall(tmp_path):
    image = tmp_path / 'flag-pole.png'
    _write_image(image, size=(900, 560), flags=[(610, 120, 45, 80)])

    result = detect(image, tmp_path)

    assert len(result.goal_candidates) == 1
    assert result.block_candidates == []
    assert result.wall_candidates == []


def test_long_platform_crossing_flag_pole_is_preserved(tmp_path):
    image = tmp_path / 'platform-through-flag.png'
    _write_image(image, size=(900, 560),
                 lines=[(420, 225, 780, 225, 7)],
                 flags=[(620, 120, 55, 110)])

    result = detect(image, tmp_path)

    assert len(result.goal_candidates) == 1
    crossing = [candidate for candidate in result.platform_candidates
                if candidate.start.x < 620 < candidate.end.x]
    assert len(crossing) == 1
    assert crossing[0].length >= 340


def test_block_edges_are_not_repeated_as_platforms_or_walls(tmp_path):
    image = tmp_path / 'block-without-duplicate-edges.png'
    _write_image(image, size=(900, 560), rectangles=[(250, 170, 410, 290, 7)])

    result = detect(image, tmp_path)

    assert len(result.block_candidates) == 1
    assert result.platform_candidates == []
    assert result.wall_candidates == []


def test_detects_filled_triangle_as_block_when_it_is_not_a_marker(tmp_path):
    image = tmp_path / 'solid-triangle.png'
    _write_image(image, size=(900, 560),
                 polygons=[([(300, 150), (390, 270), (210, 270)], True)])

    result = detect(image, tmp_path)

    assert result.goal_candidates == []
    _assert_polygon_coverage(result, [(300, 150), (390, 270), (210, 270)])


@pytest.mark.parametrize(('length', 'expected_count'), [(55, 0), (56, 1)])
def test_horizontal_minimum_length_uses_centerline_endpoints(
        tmp_path, length, expected_count):
    image = tmp_path / f'horizontal-{length}.png'
    _write_image(image, size=(900, 560), lines=[(200, 180, 200 + length, 180, 7)])

    result = detect(image, tmp_path)

    assert len(result.platform_candidates) == expected_count


@pytest.mark.parametrize(('length', 'expected_count'), [(55, 0), (56, 1)])
def test_vertical_minimum_length_uses_centerline_endpoints(
        tmp_path, length, expected_count):
    image = tmp_path / f'vertical-{length}.png'
    _write_image(image, size=(900, 560), lines=[(240, 180, 240, 180 + length, 7)])

    result = detect(image, tmp_path)

    assert len(result.wall_candidates) == expected_count


def _sloped_line(angle_degrees, length=330, center=(450, 280)):
    """以画布中心为轴、按给定倾角（度，y 向下为正）生成一条直线。"""
    radians = np.radians(angle_degrees)
    half = length / 2
    return (round(center[0] - half * np.cos(radians)), round(center[1] - half * np.sin(radians)),
            round(center[0] + half * np.cos(radians)), round(center[1] + half * np.sin(radians)))


@pytest.mark.parametrize('angle', [-59, -45, -30, -15, -6, 0, 6, 15, 30, 45, 59])
def test_line_up_to_max_platform_slope_is_a_platform(tmp_path, angle):
    """任意倾角到 60° 的直线都该识别成坡；孩子画歪、拍照倾斜都不影响。"""
    image = tmp_path / f'slope-{angle}.png'
    _write_image(image, size=(900, 560), lines=[(*_sloped_line(angle), 7)])

    result = detect(image, tmp_path)

    assert len(result.platform_candidates) == 1
    assert result.wall_candidates == []
    assert result.platform_candidates[0].angle_degrees == pytest.approx(angle, abs=1.5)
    # 端点必须落在原始直线上（斜率正确比端点绝对误差更关键）。
    candidate = result.platform_candidates[0]
    radians = np.radians(angle)
    for endpoint in (candidate.start, candidate.end):
        offset = (endpoint.x - 450) * np.sin(radians) - (endpoint.y - 280) * np.cos(radians)
        assert abs(offset) <= 3


@pytest.mark.parametrize('angle', [-89, -75, -62, 62, 75, 89])
def test_line_beyond_max_platform_slope_is_a_wall(tmp_path, angle):
    """超过 60° 的线是竖障碍，不是坡。"""
    image = tmp_path / f'wall-{angle}.png'
    _write_image(image, size=(900, 560), lines=[(*_sloped_line(angle), 7)])

    result = detect(image, tmp_path)

    assert result.platform_candidates == []
    assert len(result.wall_candidates) == 1


@pytest.mark.parametrize('angle', [0, 12, 30, 45, 55, 59, 61, 70, 84, 90])
def test_every_sloped_stroke_lands_in_exactly_one_bucket(tmp_path, angle):
    """坡与墙的分支必须严格互补：同一段墨迹不能既是坡又是墙，也不能两边都不要。"""
    image = tmp_path / f'bucket-{angle}.png'
    _write_image(image, size=(900, 560), lines=[(*_sloped_line(angle), 7)])

    result = detect(image, tmp_path)

    total = len(result.platform_candidates) + len(result.wall_candidates)
    assert total == 1, f'{angle}° 落进 {total} 个分支'
    expected_platform = abs(angle) <= 60
    assert bool(result.platform_candidates) is expected_platform


def test_shallow_vee_is_not_a_platform(tmp_path):
    """折线不是直线：外接框高度门限按倾角折算后仍须拦住浅 V 形。"""
    image = tmp_path / 'shallow-vee.png'
    _write_image(image, size=(900, 560), lines=[
        (280, 250, 450, 300, 7),
        (450, 300, 620, 250, 7),
    ])

    result = detect(image, tmp_path)

    assert result.platform_candidates == []


def test_sloped_platform_height_budget_scales_with_angle(tmp_path):
    """斜线的外接框本来就高，不能按水平线的高度预算把它剔除。"""
    image = tmp_path / 'sloped-tall-box.png'
    _write_image(image, size=(900, 560), lines=[(*_sloped_line(45, length=300), 7)])

    result = detect(image, tmp_path)

    assert len(result.platform_candidates) == 1
    assert result.platform_candidates[0].length >= 280


def test_short_line_with_low_aspect_ratio_is_not_a_platform(tmp_path):
    image = tmp_path / 'wide-short-stroke.png'
    _write_image(image, size=(900, 560), lines=[(200, 180, 270, 180, 21)])

    result = detect(image, tmp_path)

    assert result.platform_candidates == []


def test_short_line_with_less_than_seventy_percent_continuity_is_not_a_platform(tmp_path):
    image = tmp_path / 'broken-short-stroke.png'
    _write_image(image, size=(900, 560), lines=[
        (200, 180, 203, 180, 5),
        (219, 180, 222, 180, 5),
        (238, 180, 241, 180, 5),
        (257, 180, 270, 180, 5),
    ])

    result = detect(image, tmp_path)

    assert result.platform_candidates == []


def test_short_platform_outside_safe_area_is_ignored(tmp_path):
    image = tmp_path / 'unsafe-short-platform.png'
    _write_image(image, size=(900, 560), lines=[(10, 180, 80, 180, 7)])

    result = detect(image, tmp_path)

    assert result.platform_candidates == []


def test_short_wall_outside_safe_area_is_ignored(tmp_path):
    image = tmp_path / 'unsafe-short-wall.png'
    _write_image(image, size=(900, 560), lines=[(10, 180, 10, 260, 7)])

    result = detect(image, tmp_path)

    assert result.wall_candidates == []


def test_block_touching_page_edge_is_ignored(tmp_path):
    image = tmp_path / 'page-edge-block.png'
    _write_image(image, size=(900, 560), rectangles=[(0, 160, 140, 280, 7)])

    result = detect(image, tmp_path)

    assert result.block_candidates == []


def test_too_small_closed_shape_is_not_a_block(tmp_path):
    image = tmp_path / 'small-block.png'
    _write_image(image, size=(900, 560), rectangles=[(300, 180, 310, 190, 3)])

    result = detect(image, tmp_path)

    assert result.block_candidates == []


def test_shape_covering_most_of_canvas_is_not_a_block(tmp_path):
    image = tmp_path / 'canvas-covering-block.png'
    _write_image(image, size=(900, 560), rectangles=[(40, 90, 860, 520, 7)])

    result = detect(image, tmp_path)

    assert result.block_candidates == []


def test_cropped_real_photo_keeps_lines_and_actual_rectangle_geometry(tmp_path):
    from app.services.level_rectify import normalize_visible_canvas

    root = Path(__file__).resolve().parents[1]
    source = root / 'testdata' / 'levels' / 'real' / 'cropped-paper-markers.jpg'
    rectified = normalize_visible_canvas(source, tmp_path / 'rectify')

    result = detect(rectified.rectified_path, tmp_path / 'detect')

    assert len(result.platform_candidates) == 9
    assert len(result.block_candidates) == 3
    false_regions = {(252, 447, 199, 39), (40, 492, 207, 41)}
    actual_regions = {(block.region.x, block.region.y,
                       block.region.width, block.region.height)
                      for block in result.block_candidates}
    assert actual_regions.isdisjoint(false_regions)


def test_short_wall_with_low_aspect_ratio_is_ignored(tmp_path):
    image = tmp_path / 'wide-short-wall.png'
    _write_image(image, size=(900, 560), lines=[(240, 180, 240, 250, 21)])

    result = detect(image, tmp_path)

    assert result.wall_candidates == []


def test_short_wall_with_less_than_seventy_percent_continuity_is_ignored(tmp_path):
    image = tmp_path / 'broken-short-wall.png'
    _write_image(image, size=(900, 560), lines=[
        (240, 180, 240, 183, 5),
        (240, 199, 240, 202, 5),
        (240, 218, 240, 221, 5),
        (240, 237, 240, 250, 5),
    ])

    result = detect(image, tmp_path)

    assert result.wall_candidates == []


def test_short_horizontal_line_overlapping_circle_marker_is_ignored(tmp_path):
    image = tmp_path / 'short-line-through-circle.png'
    _write_image(image, size=(900, 560),
                 lines=[(210, 180, 290, 180, 5)],
                 circles=[(250, 180, 30, 5)])

    result = detect(image, tmp_path)

    assert len(result.start_candidates) == 1
    assert result.platform_candidates == []


def test_detects_long_thin_hollow_rectangle_as_block(tmp_path):
    image = tmp_path / 'long-thin-hollow-block.png'
    _write_image(image, size=(900, 560), rectangles=[(250, 180, 470, 220, 7)])

    result = detect(image, tmp_path)

    assert result.goal_candidates == []
    assert len(result.block_candidates) == 1
    assert result.platform_candidates == []
    assert result.wall_candidates == []


def test_detects_solid_concave_l_shape_as_block(tmp_path):
    image = tmp_path / 'solid-concave-block.png'
    points = [(250, 150), (282, 150), (282, 258),
              (410, 258), (410, 290), (250, 290)]
    _write_image(image, size=(900, 560), polygons=[(points, True)])

    result = detect(image, tmp_path)

    assert len(result.block_candidates) == 2
    _assert_polygon_coverage(result, points)
    assert not any(b.region.x <= 350 < b.region.x+b.region.width and
                   b.region.y <= 200 < b.region.y+b.region.height for b in result.block_candidates)


def test_detects_exact_low_density_solid_concave_polygon_as_block(tmp_path):
    image = tmp_path / 'exact-low-density-concave-block.png'
    points = [(300, 150), (420, 150), (420, 175),
              (325, 175), (325, 270), (300, 270)]
    _write_image(image, size=(900, 560), polygons=[(points, True)])

    result = detect(image, tmp_path)

    assert len(result.block_candidates) == 2
    _assert_polygon_coverage(result, points)
    assert result.platform_candidates == []
    assert result.wall_candidates == []


def test_detects_solid_cross_with_twenty_pixel_arms_as_block(tmp_path):
    image = tmp_path / 'solid-cross.png'
    points = [(350, 150), (370, 150), (370, 200), (420, 200),
              (420, 220), (370, 220), (370, 270), (350, 270),
              (350, 220), (300, 220), (300, 200), (350, 200)]
    _write_image(image, size=(900, 560), polygons=[(points, True)])

    result = detect(image, tmp_path)

    assert 3 <= len(result.block_candidates) <= 4
    _assert_polygon_coverage(result, points)


def test_detects_long_thin_hollow_parallelogram_without_false_flag(tmp_path):
    image = tmp_path / 'hollow-parallelogram.png'
    points = [(250, 180), (550, 180), (570, 230), (270, 230)]
    _write_image(image, size=(900, 560), polygons=[(points, False, 7)])

    result = detect(image, tmp_path)

    _assert_polygon_coverage(result, points)
    assert result.goal_candidates == []
    assert result.platform_candidates == []
    assert result.wall_candidates == []


def test_black_hollow_triangle_flag_remains_marker_with_downward_pole(tmp_path):
    image = tmp_path / 'black-hollow-flag.png'
    _write_image(image, size=(900, 560),
                 lines=[(610, 120, 610, 200, 5)],
                 polygons=[([(610, 120), (655, 136), (610, 160)], False, 5)])

    result = detect(image, tmp_path)

    assert len(result.goal_candidates) == 1
    assert result.block_candidates == []
    assert result.wall_candidates == []
    assert result.platform_candidates == []


@pytest.mark.parametrize('color', [(25, 25, 25), (0, 0, 255)])
@pytest.mark.parametrize('filled', [False, True])
def test_triangle_flag_does_not_swallow_connected_long_platform(tmp_path, color, filled):
    image_path = tmp_path / 'flag-on-platform.png'
    _write_image(image_path, size=(900, 560), lines=[
        (40, 400, 280, 400, 4), (600, 220, 840, 220, 4)])
    image = cv2.imread(str(image_path))
    cv2.line(image, (740, 150), (742, 220), color, 3)
    triangle = np.asarray([(740, 150), (780, 161), (741, 180)], np.int32)
    if filled:
        cv2.fillPoly(image, [triangle], color)
    else:
        cv2.polylines(image, [triangle], True, color, 3)
    cv2.imwrite(str(image_path), image)

    result = detect(image_path, tmp_path)

    assert len(result.goal_candidates) == 1
    assert result.block_candidates == []
    assert len(result.platform_candidates) == 2
    assert any(candidate.start.x < 740 < candidate.end.x
               and candidate.length >= 230 for candidate in result.platform_candidates)


# --- 取景框降级画布（frame_canvas） -------------------------------------------------
# 纸张拉正成功时画布就是纸本身，边缘朝外是桌面与纸边阴影，贴边墨迹一律不算平台。
# 但 normalize_visible_canvas 降级画布 = 前端取景框内的可见画面，用户看到的、能画到的
# 就是这个范围，画到框边合法。此时只有「横跨整幅」的背景边界才该剔除。

def test_frame_canvas_keeps_platform_entering_from_left_edge(tmp_path):
    image = tmp_path / 'frame-left-platform.png'
    _write_image(image, size=(900, 560), lines=[(0, 300, 270, 300, 7)])

    assert detect(image, tmp_path).platform_candidates == []
    assert len(detect(image, tmp_path, frame_canvas=True).platform_candidates) == 1


def test_frame_canvas_keeps_platform_touching_right_edge(tmp_path):
    image = tmp_path / 'frame-right-platform.png'
    _write_image(image, size=(900, 560), lines=[(340, 110, 899, 110, 7)])

    assert detect(image, tmp_path).platform_candidates == []
    assert len(detect(image, tmp_path, frame_canvas=True).platform_candidates) == 1


def test_frame_canvas_keeps_platform_inside_top_band(tmp_path):
    image = tmp_path / 'frame-top-platform.png'
    _write_image(image, size=(900, 560), lines=[(120, 40, 420, 40, 7)])

    assert detect(image, tmp_path).platform_candidates == []
    assert len(detect(image, tmp_path, frame_canvas=True).platform_candidates) == 1


def test_frame_canvas_still_rejects_border_spanning_both_side_edges(tmp_path):
    image = tmp_path / 'frame-paper-border.png'
    _write_image(image, size=(900, 560), lines=[(0, 300, 899, 300, 7)])

    assert detect(image, tmp_path).platform_candidates == []
    assert detect(image, tmp_path, frame_canvas=True).platform_candidates == []


def test_cropped_real_photo_frame_canvas_recovers_top_platforms(tmp_path):
    from app.services.level_rectify import normalize_visible_canvas

    root = Path(__file__).resolve().parents[1]
    source = root / 'testdata' / 'levels' / 'real' / 'cropped-paper-markers.jpg'
    rectified = normalize_visible_canvas(source, tmp_path / 'rectify')

    paper = detect(rectified.rectified_path, tmp_path / 'paper')
    frame = detect(rectified.rectified_path, tmp_path / 'frame', frame_canvas=True)

    assert len(paper.platform_candidates) == 9
    assert len(frame.platform_candidates) == 12
    assert len(frame.block_candidates) == len(paper.block_candidates)
