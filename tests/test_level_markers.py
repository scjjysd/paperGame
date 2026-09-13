from pathlib import Path

import cv2
import numpy as np
import pytest

from app.services.level_detect import detect
from app.services.level_rectify import rectify


def drawing(circle=True, flag=True, color=(25, 25, 25), filled=False):
    image = np.full((560, 900, 3), 245, np.uint8)
    cv2.line(image, (40, 400), (280, 400), color, 4)
    cv2.line(image, (600, 220), (840, 220), color, 4)
    if circle:
        cv2.circle(image, (120, 380), 18, color, -1 if filled else 3)
    if flag:
        cv2.line(image, (740, 150), (742, 220), color, 3)
        triangle = np.array([[740, 150], [780, 161], [741, 180]], np.int32)
        if filled:
            cv2.fillPoly(image, [triangle], color)
        else:
            cv2.polylines(image, [triangle], True, color, 3)
    return image


@pytest.mark.parametrize('filled', [False, True])
@pytest.mark.parametrize('color', [(25, 25, 25), (0, 0, 220), (170, 50, 30)])
def test_detect_circle_and_triangle_flag_without_color_requirement(tmp_path, color, filled):
    path = tmp_path / 'drawing.png'
    cv2.imwrite(str(path), drawing(color=color, filled=filled))
    result = detect(path, tmp_path)
    assert len(result.start_candidates) == 1
    assert abs(result.start_candidates[0].x - 120) <= 5
    assert abs(result.start_candidates[0].y - 398) <= 5
    assert len(result.goal_candidates) == 1
    region = result.goal_candidates[0].region
    assert 730 <= region.x <= 745 and 140 <= region.y <= 155
    assert region.x + region.width >= 775


def test_triangle_without_pole_and_square_are_not_markers(tmp_path):
    image = drawing(circle=False, flag=False)
    cv2.polylines(image, [np.array([[400, 90], [440, 105], [400, 125]])], True, (20, 20, 20), 3)
    cv2.rectangle(image, (100, 100), (130, 130), (20, 20, 20), 3)
    path = tmp_path / 'noise.png'
    cv2.imwrite(str(path), image)
    result = detect(path, tmp_path)
    assert not result.start_candidates
    assert not result.goal_candidates


@pytest.mark.parametrize('face', ['rectangle', 'tee', 'elbow', 'diagonal'])
def test_pole_with_non_triangular_face_is_not_flag(tmp_path, face):
    image = drawing(circle=False, flag=False)
    cv2.line(image, (420, 90), (420, 170), (20, 20, 20), 3)
    if face == 'rectangle':
        cv2.rectangle(image, (420, 90), (465, 125), (20, 20, 20), 3)
    elif face == 'tee':
        cv2.line(image, (420, 90), (465, 90), (20, 20, 20), 3)
    elif face == 'elbow':
        cv2.line(image, (420, 125), (465, 125), (20, 20, 20), 3)
    else:
        cv2.line(image, (420, 90), (465, 125), (20, 20, 20), 3)
    path = tmp_path / f'{face}.png'
    cv2.imwrite(str(path), image)
    assert not detect(path, tmp_path).goal_candidates


def test_large_open_circle_is_detected_without_becoming_flag(tmp_path):
    image = drawing(circle=False, flag=False)
    cv2.ellipse(image, (140, 350), (48, 48), 0, 25, 335, (20, 20, 20), 4)
    path = tmp_path / 'large-open-circle.png'
    cv2.imwrite(str(path), image)
    result = detect(path, tmp_path)
    assert len(result.start_candidates) == 1
    assert abs(result.start_candidates[0].x - 140) <= 8
    assert not result.goal_candidates


@pytest.mark.parametrize('points', [
    [[740, 150], [780, 150], [740, 185]],
    [[740, 150], [780, 185], [740, 185]],
])
def test_right_triangle_flag_is_detected(tmp_path, points):
    image = drawing(circle=False, flag=False)
    cv2.line(image, (740, 150), (740, 220), (20, 20, 20), 3)
    cv2.polylines(image, [np.asarray(points, np.int32)], True, (20, 20, 20), 3)
    path = tmp_path / 'right-triangle.png'
    cv2.imwrite(str(path), image)
    assert len(detect(path, tmp_path).goal_candidates) == 1


def test_real_photo_detects_black_flag_but_no_start(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'testdata/levels/real/low-contrast-paper.jpg'
    rectified = rectify(source, tmp_path)
    result = detect(rectified.rectified_path, tmp_path)
    assert not result.start_candidates
    assert len(result.goal_candidates) == 1


@pytest.mark.parametrize('add_circle', [False, True])
def test_real_photo_full_parse_requires_start(tmp_path, monkeypatch, add_circle):
    from app.services.level_parser import parse
    from app.level_contracts import LevelReady, LevelFailed
    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    source = Path(__file__).resolve().parents[1] / 'testdata/levels/real/low-contrast-paper.jpg'
    image = cv2.imread(str(source))
    if add_circle:
        cv2.circle(image, (160, 675), 15, (25, 25, 25), 3)
    cv2.imwrite(str(tmp_path / 'input.png'), image)
    result = parse(tmp_path)
    if add_circle:
        LevelReady.model_validate(result)
        assert result['result']['analysis']['playability'] == 'not_checked'
        assert result['result']['level']['playerStart']['source'] == 'detected'
    else:
        LevelFailed.model_validate(result)
        assert result['error']['code'] == 'START_NOT_FOUND'
        assert not result['error']['retryable']


def test_multiple_circles_and_flags_are_preserved_for_rejection(tmp_path):
    image = drawing()
    cv2.circle(image, (330, 350), 18, (25, 25, 25), 3)
    cv2.line(image, (420, 90), (420, 160), (25, 25, 25), 3)
    cv2.polylines(image, [np.array([[420, 90], [460, 105], [420, 120]])], True, (25, 25, 25), 3)
    cv2.imwrite(str(tmp_path / 'drawing.png'), image)
    result = detect(tmp_path / 'drawing.png', tmp_path)
    assert len(result.start_candidates) == 2
    assert len(result.goal_candidates) == 2


def test_real_hand_drawn_open_circle_and_filled_flag_are_detected(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'testdata/levels/real/hand-drawn-markers.jpg'
    rectified = rectify(source, tmp_path)
    result = detect(rectified.rectified_path, tmp_path)
    assert len(result.start_candidates) == 1
    assert len(result.goal_candidates) == 1
    assert len(result.platform_candidates) >= 6
    assert abs(result.start_candidates[0].x - 148) <= 8
    assert abs(result.start_candidates[0].y - 182) <= 8
    goal = result.goal_candidates[0].region
    assert abs(goal.x - 800) <= 10 and abs(goal.y - 93) <= 10
    assert goal.width >= 35 and goal.height >= 45


def test_real_hand_drawn_photo_generates_level(tmp_path, monkeypatch):
    from app.level_contracts import LevelReady
    from app.services.level_parser import parse

    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    source = Path(__file__).resolve().parents[1] / 'testdata/levels/real/hand-drawn-markers.jpg'
    (tmp_path / 'input.png').write_bytes(source.read_bytes())

    result = LevelReady.model_validate(parse(tmp_path))

    assert result.result.analysis.playability == 'not_checked'
    assert result.result.level.playerStart.source == 'detected'
    assert result.result.level.goalRegion.x >= 750
    assert result.result.level.goalRegion.width > 0
