from itertools import permutations
import json
from pathlib import Path

import cv2
import numpy as np

from app.services.level_detect import detect


def _write_image(path: Path, size=(420, 300), lines=(), flags=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = np.full((size[1], size[0], 3), 245, dtype=np.uint8)
    for x1, y1, x2, y2, width in lines:
        cv2.line(image, (x1, y1), (x2, y2), (25, 25, 25), width)
    for x, y, w, h in flags:
        cv2.rectangle(image, (x, y), (x + w, y + h), (0, 0, 255), 4)
        cv2.line(image, (x + w // 2, y - 25), (x + w // 2, y + h), (30, 30, 30), 3)
        cv2.fillPoly(image, [np.array([(x + w // 2, y - 25), (x + w, y - 12), (x + w // 2, y)], dtype=np.int32)], (0, 0, 255))
    cv2.imwrite(str(path), image)


def _endpoint_error(candidate, truth):
    direct = np.hypot(candidate.start.x - truth['x1'], candidate.start.y - truth['y1'])
    direct += np.hypot(candidate.end.x - truth['x2'], candidate.end.y - truth['y2'])
    reverse = np.hypot(candidate.start.x - truth['x2'], candidate.start.y - truth['y2'])
    reverse += np.hypot(candidate.end.x - truth['x1'], candidate.end.y - truth['y1'])
    return min(direct, reverse) / 2


def test_golden_c1levels_detects_seven_platforms_with_endpoint_accuracy(tmp_path):
    root = Path(__file__).resolve().parents[2]
    image = root / 'C1Levels' / 'level1-background.png'
    truth = json.loads((root / 'C1Levels' / 'level1.json').read_text(encoding='utf-8'))

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
    assert goal.region.x <= 300 and goal.region.y <= 45
    assert goal.region.x + goal.region.width >= 355
    assert goal.region.y + goal.region.height >= 150
    assert all(abs(p.start.x - 327) > 4 or abs(p.start.y - 70) > 4 for p in result.platform_candidates)


def test_multiple_flags_remain_separate_candidates(tmp_path):
    image = tmp_path / 'flags.png'
    _write_image(image, lines=[(30, 260, 390, 260, 7)], flags=[(55, 70, 35, 60), (300, 100, 45, 70)])

    first = detect(image, tmp_path / 'first')
    second = detect(image, tmp_path / 'second')

    assert [g.id for g in first.goal_candidates] == ['goal_001', 'goal_002']
    assert [(g.region.x, g.region.y) for g in first.goal_candidates] == [(g.region.x, g.region.y) for g in second.goal_candidates]

