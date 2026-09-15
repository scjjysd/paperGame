from pathlib import Path

import cv2

from app.services.level_markers import detect_markers
from app.services.level_rectify import rectify


def test_real_short_platform_photo_detects_hand_drawn_solid_flag(tmp_path):
    source = (Path(__file__).resolve().parents[1] / 'testdata' / 'levels'
              / 'real' / 'short-platform.jpg')
    rectified = rectify(source, tmp_path)

    circles, flags = detect_markers(cv2.imread(str(rectified.rectified_path)))

    assert len(circles) == 1
    assert len(flags) == 1
    x, y, width, height = flags[0]
    assert abs(x - 796) <= 5 and abs(y - 97) <= 5
    assert width >= 40 and height >= 50
