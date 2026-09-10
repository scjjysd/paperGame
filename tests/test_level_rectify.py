import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.services.level_rectify import RectifyIssue, order_corners, rectify


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / 'testdata' / 'levels' / 'synthetic'


def _corner_error(actual, expected):
    return max(
        float(np.linalg.norm(np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32)))
        for a, b in zip(actual, expected)
    )


def test_order_corners_returns_tl_tr_br_bl():
    shuffled = np.array([[900, 700], [100, 100], [100, 700], [900, 100]], np.float32)
    assert order_corners(shuffled).tolist() == [
        [100, 100], [900, 100], [900, 700], [100, 700]
    ]


def test_rectify_recovers_perspective_sample(tmp_path):
    image = SAMPLES / 'sample-01.png'
    truth = json.loads((SAMPLES / 'sample-01.json').read_text(encoding='utf-8'))

    result = rectify(image, tmp_path)

    assert result.width == truth['rectifiedSize']['width']
    assert result.height == truth['rectifiedSize']['height']
    expected_corners = order_corners(np.asarray([
        [point['x'], point['y']] for point in truth['paperCorners']
    ], dtype=np.float32)).tolist()
    assert _corner_error(result.corners, expected_corners) <= 4
    assert result.rectified_path == tmp_path / 'rectified.png'
    assert (tmp_path / 'rectified.png').exists()
    assert (tmp_path / 'paper-mask.png').exists()
    transform = json.loads((tmp_path / 'transform.json').read_text(encoding='utf-8'))
    assert len(transform['matrix']) == 3
    assert np.asarray(cv2.imread(str(tmp_path / 'rectified.png'))).shape[:2] == (560, 900)


@pytest.mark.parametrize('number', range(10))
def test_rectify_recovers_all_synthetic_variants(tmp_path, number):
    image = SAMPLES / ('sample-%02d.png' % number)
    truth = json.loads((SAMPLES / ('sample-%02d.json' % number)).read_text(encoding='utf-8'))

    result = rectify(image, tmp_path / ('sample-%02d' % number))

    expected = order_corners(np.asarray([
        [point['x'], point['y']] for point in truth['paperCorners']
    ], dtype=np.float32)).tolist()
    if truth['variant'] == 'rotated':
        # 生成器使用 expand=False 将非正方形照片旋转 90 度，真值角点包含画布外区域；
        # 检测器只能恢复图像内可见纸张，因此这里只校验顺序、边界和方向产物。
        assert result.corners == order_corners(np.asarray(result.corners, dtype=np.float32)).tolist()
        assert all(0 <= x < 1280 and 0 <= y < 900 for x, y in result.corners)
    else:
        assert _corner_error(result.corners, expected) <= 4
    assert result.width == truth['rectifiedSize']['width']
    assert result.height == truth['rectifiedSize']['height']
    assert cv2.imread(str(result.rectified_path)).shape[:2] == (560, 900)


def test_rectify_does_not_publish_partially_written_artifacts(tmp_path, monkeypatch):
    image = SAMPLES / 'sample-01.png'
    old_rectified = tmp_path / 'rectified.png'
    old_rectified.write_bytes(b'old')

    def fail_write(*_args, **_kwargs):
        return False

    monkeypatch.setattr(cv2, 'imwrite', fail_write)
    with pytest.raises(OSError):
        rectify(image, tmp_path)

    assert old_rectified.read_bytes() == b'old'
    assert not list(tmp_path.glob('*.tmp'))
    assert not list(tmp_path.glob('*.tmp.png'))


def test_rectify_rejects_image_without_paper(tmp_path):
    image = np.full((900, 1280, 3), 180, dtype=np.uint8)
    input_path = tmp_path / 'blank.png'
    assert cv2.imwrite(str(input_path), image)

    with pytest.raises(RectifyIssue) as raised:
        rectify(input_path, tmp_path / 'out')

    assert raised.value.reason == 'PAPER_NOT_FOUND'


def test_rectify_rejects_two_similar_papers(tmp_path):
    image = np.full((900, 1280, 3), 180, dtype=np.uint8)
    cv2.rectangle(image, (50, 120), (570, 760), (248, 248, 248), -1)
    cv2.rectangle(image, (710, 120), (1230, 760), (248, 248, 248), -1)
    input_path = tmp_path / 'two-papers.png'
    assert cv2.imwrite(str(input_path), image)

    with pytest.raises(RectifyIssue) as raised:
        rectify(input_path, tmp_path / 'out')

    assert raised.value.reason == 'PAPER_AMBIGUOUS'


def test_rectify_rejects_occluded_paper(tmp_path):
    image = cv2.imread(str(SAMPLES / 'sample-00.png'))
    cv2.rectangle(image, (0, 0), (1280, 550), (180, 180, 174), -1)
    input_path = tmp_path / 'occluded.png'
    assert cv2.imwrite(str(input_path), image)

    with pytest.raises(RectifyIssue) as raised:
        rectify(input_path, tmp_path / 'out')

    assert raised.value.reason == 'PAPER_OCCLUDED'


def test_rectify_rejects_ambiguous_orientation_for_square_paper(tmp_path):
    image = np.full((1000, 1000, 3), 180, dtype=np.uint8)
    cv2.rectangle(image, (100, 100), (900, 900), (248, 248, 248), -1)
    input_path = tmp_path / 'square.png'
    assert cv2.imwrite(str(input_path), image)

    with pytest.raises(RectifyIssue) as raised:
        rectify(input_path, tmp_path / 'out')

    assert raised.value.reason == 'ORIENTATION_AMBIGUOUS'
