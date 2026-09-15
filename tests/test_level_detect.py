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
    for points, filled in polygons:
        contour = np.asarray(points, dtype=np.int32)
        if filled:
            cv2.fillPoly(image, [contour], (25, 25, 25))
        else:
            cv2.polylines(image, [contour], True, (25, 25, 25), 5)
    cv2.imwrite(str(path), image)


def _endpoint_error(candidate, truth):
    direct = np.hypot(candidate.start.x - truth['x1'], candidate.start.y - truth['y1'])
    direct += np.hypot(candidate.end.x - truth['x2'], candidate.end.y - truth['y2'])
    reverse = np.hypot(candidate.start.x - truth['x2'], candidate.start.y - truth['y2'])
    reverse += np.hypot(candidate.end.x - truth['x1'], candidate.end.y - truth['y1'])
    return min(direct, reverse) / 2


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

    assert len(result.block_candidates) == 1
    region = result.block_candidates[0].region
    assert region.x <= 472 and region.x + region.width >= 608
    assert region.y <= 147 and region.y + region.height >= 268


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
    assert len(result.block_candidates) == 1


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


@pytest.mark.parametrize('delta_y', [40, -40])
def test_platform_at_six_degree_tolerance_is_detected(tmp_path, delta_y):
    image = tmp_path / f'platform-six-{delta_y}.png'
    _write_image(image, size=(900, 560),
                 lines=[(180, 220, 561, 220 + delta_y, 7)])

    result = detect(image, tmp_path)

    assert len(result.platform_candidates) == 1


@pytest.mark.parametrize('delta_y', [41, -41])
def test_platform_beyond_six_degree_tolerance_is_ignored(tmp_path, delta_y):
    image = tmp_path / f'platform-over-six-{delta_y}.png'
    _write_image(image, size=(900, 560),
                 lines=[(180, 220, 561, 220 + delta_y, 7)])

    result = detect(image, tmp_path)

    assert result.platform_candidates == []


@pytest.mark.parametrize('delta_x', [40, -40])
def test_wall_at_six_degree_tolerance_is_detected(tmp_path, delta_x):
    image = tmp_path / f'wall-six-{delta_x}.png'
    _write_image(image, size=(900, 560),
                 lines=[(350, 120, 350 + delta_x, 501, 7)])

    result = detect(image, tmp_path)

    assert len(result.wall_candidates) == 1


@pytest.mark.parametrize('delta_x', [41, -41])
def test_wall_beyond_six_degree_tolerance_is_ignored(tmp_path, delta_x):
    image = tmp_path / f'wall-over-six-{delta_x}.png'
    _write_image(image, size=(900, 560),
                 lines=[(350, 120, 350 + delta_x, 501, 7)])

    result = detect(image, tmp_path)

    assert result.wall_candidates == []


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


def test_cropped_real_photo_keeps_all_fifteen_platforms(tmp_path):
    from app.services.level_rectify import normalize_visible_canvas

    root = Path(__file__).resolve().parents[1]
    source = root / 'testdata' / 'levels' / 'real' / 'cropped-paper-markers.jpg'
    rectified = normalize_visible_canvas(source, tmp_path / 'rectify')

    result = detect(rectified.rectified_path, tmp_path / 'detect')

    assert len(result.platform_candidates) == 15
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

    assert len(result.block_candidates) == 1
    region = result.block_candidates[0].region
    gray = cv2.imread(str(image), cv2.IMREAD_GRAYSCALE)
    dark_coverage = np.mean(gray[region.y:region.y + region.height,
                                 region.x:region.x + region.width] < 100)
    assert .35 <= dark_coverage <= .40
    assert region.x <= 252 and region.x + region.width >= 408
    assert region.y <= 152 and region.y + region.height >= 288


def test_detects_exact_low_density_solid_concave_polygon_as_block(tmp_path):
    image = tmp_path / 'exact-low-density-concave-block.png'
    points = [(300, 150), (420, 150), (420, 175),
              (325, 175), (325, 270), (300, 270)]
    _write_image(image, size=(900, 560), polygons=[(points, True)])

    result = detect(image, tmp_path)

    assert len(result.block_candidates) == 1
    assert result.platform_candidates == []
    assert result.wall_candidates == []
