import importlib
import importlib.util
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.services.level_detect import detect


def _covers(rectangles, point):
    x, y = point
    return any(a <= x < a + w and b <= y < b + h for a, b, w, h in rectangles)


def _regions(result):
    return [(b.region.x, b.region.y, b.region.width, b.region.height)
            for b in result.block_candidates]


def _shape_module():
    name = 'app.services.level_shape_rectangles'
    assert importlib.util.find_spec(name) is not None, 'shape-preserving decomposition is required'
    return importlib.import_module(name)


@pytest.mark.parametrize('mirror', [False, True])
def test_scanline_decomposition_keeps_l_shape_in_two_rectangles(mirror):
    mask = np.zeros((100, 180), np.uint8)
    mask[10:90, 10:40] = 255
    mask[60:90, 10:160] = 255
    if mirror:
        mask = np.fliplr(mask).copy()
    rectangles = _shape_module().shape_rectangles(mask)
    assert len(rectangles) == 2
    assert not _covers(rectangles, (80, 30))
    reconstructed = np.zeros_like(mask)
    for x, y, width, height in rectangles:
        reconstructed[y:y + height, x:x + width] = 255
    assert np.array_equal(reconstructed, mask)
    assert rectangles == _shape_module().shape_rectangles(mask)


def test_rectangle_limit_never_replaces_concave_shape_with_bbox():
    mask = np.zeros((100, 100), np.uint8)
    for y in range(10, 90):
        mask[y, 10:20 + y // 2] = 255
    rectangles = _shape_module().shape_rectangles(mask, max_rectangles=8)
    assert len(rectangles) <= 8
    assert not _covers(rectangles, (60, 15))
    assert not _covers(rectangles, (70, 85))
    if rectangles:
        covered = sum(w * h for _, _, w, h in rectangles)
        assert covered >= np.count_nonzero(mask) * .85


@pytest.mark.parametrize('kind', ['cross', 'stairs'])
def test_complex_concave_shapes_cover_the_subject_without_filling_empty_corners(kind):
    mask = np.zeros((130, 150), np.uint8)
    if kind == 'cross':
        mask[10:120, 60:85] = 255
        mask[50:80, 10:140] = 255
        empty = [(30, 25), (110, 100)]
    else:
        mask[10:40, 10:45] = 255
        mask[40:75, 10:85] = 255
        mask[75:120, 10:140] = 255
        empty = [(65, 25), (120, 55)]
    rectangles = _shape_module().shape_rectangles(mask)
    assert 2 <= len(rectangles) <= 4
    assert all(not _covers(rectangles, point) for point in empty)
    reconstruction = np.zeros_like(mask)
    for x, y, width, height in rectangles:
        reconstruction[y:y + height, x:x + width] = 255
    assert np.array_equal(mask, reconstruction)


@pytest.mark.parametrize('filled', [False, True])
@pytest.mark.parametrize('mirror', [False, True])
def test_detects_l_shapes_without_filling_the_concave_corner(tmp_path, filled, mirror):
    image = np.full((560, 900, 3), 235, np.uint8)
    points = np.array([(200, 180), (235, 180), (235, 270),
                       (390, 270), (390, 300), (200, 300)], np.int32)
    if mirror:
        points[:, 0] = 600 - points[:, 0]
    if filled:
        cv2.fillPoly(image, [points], (100, 100, 100))
    else:
        cv2.polylines(image, [points], True, (100, 100, 100), 3)
    path = tmp_path / 'l.png'
    cv2.imwrite(str(path), image)
    result = detect(path, tmp_path)
    rectangles = _regions(result)
    assert len(rectangles) == 2
    assert not _covers(rectangles, (300, 220))
    assert _covers(rectangles, (380 if mirror else 215, 230))
    assert _covers(rectangles, (300, 285))
    assert result.platform_candidates == []
    assert result.wall_candidates == []


@pytest.mark.parametrize('height', [12, 22, 35])
@pytest.mark.parametrize('gray', [95, 155])
def test_gray_long_hollow_rectangle_is_actual_size_without_duplicate_lines(tmp_path, height, gray):
    image = np.full((560, 900, 3), 210, np.uint8)
    points = np.array([(180, 180), (600, 177),
                       (600, 177 + height), (180, 180 + height)], np.int32)
    cv2.polylines(image, [points], True, (gray, gray, gray), 2)
    path = tmp_path / 'thin-gray.png'
    cv2.imwrite(str(path), image)
    result = detect(path, tmp_path)
    rectangles = _regions(result)
    assert len(rectangles) == 1
    x, y, width, actual_height = rectangles[0]
    assert abs(width - 421) <= 6
    assert abs(actual_height - height) <= 7
    assert result.platform_candidates == []
    assert result.wall_candidates == []


def test_independent_line_inside_l_concavity_is_preserved(tmp_path):
    image = np.full((560, 900, 3), 245, np.uint8)
    points = np.array([(200, 180), (230, 180), (230, 310),
                       (450, 310), (450, 340), (200, 340)], np.int32)
    cv2.polylines(image, [points], True, (25, 25, 25), 5)
    cv2.line(image, (270, 230), (420, 230), (25, 25, 25), 5)
    path = tmp_path / 'concavity-line.png'
    cv2.imwrite(str(path), image)
    result = detect(path, tmp_path)
    assert len(result.block_candidates) == 2
    assert len(result.platform_candidates) == 1
    assert result.platform_candidates[0].start.x <= 275
    assert result.platform_candidates[0].end.x >= 415


def test_real_photo_keeps_rectangles_and_l_corners(tmp_path):
    path = Path(__file__).parents[1] / 'testdata/levels/real/shape-preserving-rectangles.png'
    result = detect(path, tmp_path)
    rectangles = _regions(result)
    assert 12 <= len(rectangles) <= 28
    assert not _covers(rectangles, (640, 255))
    assert not _covers(rectangles, (140, 435))
    for point in [(150, 57), (270, 157), (230, 198), (470, 250),
                  (130, 330), (470, 375), (380, 454), (100, 462),
                  (30, 435), (650, 303), (720, 260), (550, 113), (840, 116)]:
        assert _covers(rectangles, point), point
    assert len(result.start_candidates) == 1
    assert len(result.goal_candidates) == 1
    assert not _covers(rectangles, (830, 83))
    assert result.platform_candidates == []
    assert result.wall_candidates == []


def test_open_parallel_lines_are_not_blocks(tmp_path):
    image = np.full((560, 900, 3), 210, np.uint8)
    cv2.line(image, (160, 180), (650, 180), (150, 150, 150), 3)
    cv2.line(image, (160, 202), (650, 202), (150, 150, 150), 3)
    path = tmp_path / 'parallel.png'
    cv2.imwrite(str(path), image)
    assert detect(path, tmp_path).block_candidates == []


def test_real_rectangles_photo_can_publish_without_line_platforms(tmp_path, monkeypatch):
    from app.services.level_parser import parse
    from app.services import level_rectify
    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    source = Path(__file__).parents[1] / 'testdata/levels/real/shape-preserving-rectangles.png'
    (tmp_path / 'input.png').write_bytes(source.read_bytes())
    # 输入已经是前次发布的900×560拉正图，不再对平台轮廓执行纸张查找。
    monkeypatch.setattr(level_rectify, 'rectify', level_rectify.normalize_visible_canvas)
    result = parse(tmp_path)
    assert result['status'] == 'ready'
    level = result['result']['level']
    assert level['platforms'] == []
    assert len(level['blocks']) >= 12
    rectangles = [(b['region']['x'], b['region']['y'], b['region']['width'], b['region']['height'])
                  for b in level['blocks']]
    assert not _covers(rectangles, (640, 255))
    assert not _covers(rectangles, (140, 435))


def test_original_capture_publishes_shape_preserving_rectangles(tmp_path, monkeypatch):
    from app.services.level_parser import parse
    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    path = Path(__file__).parents[1] / 'testdata/levels/real/shape-preserving-source.jpg'
    (tmp_path / 'input.png').write_bytes(path.read_bytes())
    result = parse(tmp_path)
    assert result['status'] == 'ready'
    level = result['result']['level']
    assert level['platforms'] == [] and level['walls'] == []
    assert len(level['blocks']) == 13
    rectangles = [(b['region']['x'], b['region']['y'], b['region']['width'], b['region']['height'])
                  for b in level['blocks']]
    assert not _covers(rectangles, (640, 255))
    assert not _covers(rectangles, (140, 435))
    assert _covers(rectangles, (720, 260)) and _covers(rectangles, (650, 303))
    assert result['result']['analysis']['playability'] == 'not_checked'


def test_publish_respects_shape_evidence_and_keeps_internal_rectangles():
    from app.services.level_shape_rectangles import ShapeEvidence
    from app.services.level_detect import BlockCandidate, RegionCandidate, DetectionResult
    from app.services.level_parser import _collision_geometry
    mask = np.full((60, 100), 255, np.uint8)
    source = np.zeros_like(mask)
    source[[0, -1], :] = 255
    internal = (20, 20, 60, 20)
    evidence = ShapeEvidence(0, 0, mask, source, (internal,))
    good = BlockCandidate('good', RegionCandidate(*internal, .9), .9, evidence)
    bad = BlockCandidate('bad', RegionCandidate(0, 0, 10, 10, .9), .9, evidence)
    result = DetectionResult([], [], Path('unused'), block_candidates=[good, bad])
    _, blocks = _collision_geometry(result, source, 100, 60)
    assert len(blocks) == 1
    assert blocks[0]['region']['x'] == 20
