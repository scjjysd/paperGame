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


def test_circle_with_eighty_degree_gap_is_detected(tmp_path):
    image = drawing(circle=False, flag=False)
    cv2.ellipse(image, (140, 350), (32, 32), 0, 40, 320, (20, 20, 20), 4)
    path = tmp_path / 'open-circle.png'
    cv2.imwrite(str(path), image)
    result = detect(path, tmp_path)
    assert len(result.start_candidates) == 1


def test_small_rectangular_platform_is_not_detected_as_second_circle(tmp_path):
    image = drawing(circle=False, flag=False)
    cv2.circle(image, (120, 380), 18, (25, 25, 25), 3)
    # 接近正方形的小平台会触发霍夫圆，需要只保留真正的手绘起点圆。
    cv2.rectangle(image, (430, 340), (460, 368), (25, 25, 25), 2)
    path = tmp_path / 'circle-and-small-platform.png'
    cv2.imwrite(str(path), image)

    result = detect(path, tmp_path)

    assert len(result.start_candidates) == 1
    assert abs(result.start_candidates[0].x - 120) <= 5
    assert abs(result.start_candidates[0].y - 398) <= 5


def test_box_shaped_platforms_keep_single_start(tmp_path):
    """平台被画成闭合长方框时，框的端头/拐角会被当成起点圆圈（实测 AMBIGUOUS_START）。

    方框端头局部看就是一段圆角弧：两条长边恰好落在候选半径附近，端弧补上其余方向，
    `_looks_circular` 与轮廓复核都会通过。只有看候选中心所在**背景空隙**的整体形状
    （圆环内部近似方形空腔 vs 方框内部的细长走廊）才能区分。
    """
    image = np.full((560, 900, 3), 245, np.uint8)
    ink = (25, 25, 25)
    for x0, y0, x1, y1 in [(30, 40, 340, 68), (115, 196, 395, 212), (403, 246, 610, 270),
                           (25, 447, 210, 478), (615, 292, 760, 360), (250, 480, 535, 500),
                           (260, 140, 322, 176), (25, 395, 255, 415)]:
        cv2.rectangle(image, (x0, y0), (x1, y1), ink, 4)
    # 手绘起点圆（略微不圆）
    cv2.ellipse(image, (155, 166), (16, 18), 12, 0, 360, ink, 3)
    # 手绘终点旗帜：旗杆 + 实心三角旗面
    cv2.line(image, (850, 70), (852, 133), ink, 4)
    cv2.fillPoly(image, [np.array([[850, 72], [870, 92], [851, 116]], np.int32)], ink)
    path = tmp_path / 'box-shaped-platforms.png'
    cv2.imwrite(str(path), image)

    result = detect(path, tmp_path)

    assert len(result.start_candidates) == 1
    assert abs(result.start_candidates[0].x - 155) <= 8
    assert len(result.goal_candidates) == 1


def test_tiny_closed_noise_blob_is_not_a_start(tmp_path):
    """空白纸纹经自适应阈值偶发的小闭合噪点不能算起点。

    实测噪点外框仅 13×10 像素（合成起点圆直径约 0.07 倍画布宽），轮廓复核却会接受它，
    于是同一个画面上出现第二个起点 → AMBIGUOUS_START。
    """
    image = drawing()
    noisy = np.array([[300, 408], [307, 410], [308, 417], [301, 419], [296, 415], [297, 411]],
                     np.int32)
    cv2.polylines(image, [noisy], True, (25, 25, 25), 1)
    path = tmp_path / 'circle-and-paper-noise.png'
    cv2.imwrite(str(path), image)

    result = detect(path, tmp_path)

    assert len(result.start_candidates) == 1
    assert abs(result.start_candidates[0].x - 120) <= 5
    assert abs(result.start_candidates[0].y - 398) <= 5


def test_composited_start_circle_on_platform_line_keeps_start(tmp_path):
    """客户端合成的起点标记：空心圆正压在起点平台线上（C1YugongPhotoComposite 的几何）。

    圆的右侧弧会被概率霍夫当成旗杆，并凑出一块伪三角旗面；旧逻辑按「圆圈与旗帜重叠」
    删掉真圆圈、只留伪旗，于是每次上传都必然报 START_NOT_FOUND。
    """
    image = np.full((560, 900, 3), 245, np.uint8)
    # 半径 0.035W、线宽 0.006W，起点平台线紧贴圆的下缘
    cv2.circle(image, (90, 347), 31, (0, 0, 0), 6)
    cv2.rectangle(image, (27, 372), (153, 384), (0, 0, 0), -1)
    # 用户手绘的平台线（自然倾斜），端点避开标记擦除区
    cv2.line(image, (175, 325), (280, 303), (40, 40, 40), 3)
    cv2.line(image, (376, 145), (539, 118), (40, 40, 40), 3)
    # 合成终点旗帜：旗杆 + 右向三角旗面
    cv2.rectangle(image, (789, 252), (795, 325), (0, 0, 0), -1)
    cv2.fillPoly(image, [np.array([[792, 252], [824, 271], [792, 291]], np.int32)], (0, 0, 0))
    path = tmp_path / 'composited-markers.png'
    cv2.imwrite(str(path), image)

    result = detect(path, tmp_path)

    assert len(result.start_candidates) == 1
    assert abs(result.start_candidates[0].x - 89) <= 6
    assert abs(result.start_candidates[0].y - 378) <= 6
    assert len(result.goal_candidates) == 1


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


def test_real_flag_overlapping_circle_candidate_generates_level(tmp_path, monkeypatch):
    """真实旗帜即使同时触发霍夫圆，也必须由旗杆＋三角旗面的正向证据保留下来。"""
    from app.level_contracts import LevelReady
    from app.services.level_parser import parse

    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    source = Path(__file__).resolve().parents[1] / 'testdata/levels/real/phone.jpg'
    (tmp_path / 'input.png').write_bytes(source.read_bytes())

    result = LevelReady.model_validate(parse(tmp_path))

    assert abs(result.result.level.playerStart.x - 163) <= 8
    assert abs(result.result.level.playerStart.y - 202) <= 8
    assert abs(result.result.level.goalRegion.x - 832) <= 8
    assert abs(result.result.level.goalRegion.y - 65) <= 8


def test_visible_canvas_fallback_keeps_large_valid_flag(tmp_path, monkeypatch):
    """纸边不可见时，大旗帜已通过形状检测就不能再被解析器的固定高度阈值删除。"""
    from app.level_contracts import LevelReady
    from app.services.level_parser import parse

    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    source = Path(__file__).resolve().parents[1] / 'testdata/levels/real/phone.png'
    (tmp_path / 'input.png').write_bytes(source.read_bytes())

    result = LevelReady.model_validate(parse(tmp_path))

    assert len(result.result.level.platforms) == 3
    assert abs(result.result.level.playerStart.x - 90) <= 8
    assert abs(result.result.level.playerStart.y - 370) <= 8
    assert abs(result.result.level.goalRegion.x - 788) <= 8
    assert result.result.level.goalRegion.height >= 140


@pytest.mark.parametrize('filename,start_x,goal_x,platform_count,block_count', [
    # 降级画布下平台侧不再沿用纸张拉正的贴边门槛：cropped 顶部 3 条真笔迹此前被
    # 当成「纸外背景」吃掉（9→12）。rolled 的纸卷上沿会横跨画布两边，仍被剔除。
    ('cropped-paper-markers.jpg', 82, 808, 12, 3),
    ('rolled-page-markers.jpg', 199, 690, 11, 2),
])
def test_real_photo_without_four_visible_paper_edges_generates_level(
        tmp_path, monkeypatch, filename, start_x, goal_x, platform_count, block_count):
    from app.level_contracts import LevelReady
    from app.services.level_parser import parse

    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    source = Path(__file__).resolve().parents[1] / 'testdata/levels/real' / filename
    (tmp_path / 'input.png').write_bytes(source.read_bytes())

    result = LevelReady.model_validate(parse(tmp_path))

    assert result.result.analysis.playability == 'not_checked'
    assert result.result.level.playerStart.source == 'detected'
    assert abs(result.result.level.playerStart.x - start_x) <= 10
    assert abs(result.result.level.goalRegion.x - goal_x) <= 10
    assert result.result.level.goalRegion.width > 0
    assert len(result.result.level.platforms) == platform_count
    assert len(result.result.level.blocks) == block_count


def test_closeup_without_visible_paper_edges_keeps_corner_flag(tmp_path, monkeypatch):
    """纸铺满画面时，画在纸角附近的旗帜不能被降级路径的固定边缘内缩删掉。

    降级画布就是取景框内区域本身，画布内任何位置都可能是合法标记；此前 5% 内缩
    （45px）会把旗面中心在 96.3% 宽度处的合法终点剔除，报出 GOAL_NOT_FOUND。
    """
    from app.level_contracts import LevelReady
    from app.services.level_parser import parse

    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    source = Path(__file__).resolve().parents[1] / 'testdata/levels/real/closeup-corner-flag.png'
    (tmp_path / 'input.png').write_bytes(source.read_bytes())

    result = LevelReady.model_validate(parse(tmp_path))

    assert result.result.level.playerStart.source == 'detected'
    assert abs(result.result.level.playerStart.x - 175) <= 10
    assert abs(result.result.level.playerStart.y - 157) <= 10
    assert abs(result.result.level.goalRegion.x - 846) <= 10
    assert abs(result.result.level.goalRegion.y - 32) <= 10
    assert result.result.level.goalRegion.width > 0
    assert result.result.level.goalRegion.height >= 40
    assert len(result.result.level.platforms) >= 5


def test_image_without_edges_or_markers_fails_for_missing_markers(tmp_path, monkeypatch):
    from app.level_contracts import LevelFailed
    from app.services.level_parser import parse

    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    cv2.imwrite(str(tmp_path / 'input.png'), np.full((720, 1080, 3), 180, np.uint8))

    result = LevelFailed.model_validate(parse(tmp_path))

    assert result.error.code == 'START_AND_GOAL_NOT_FOUND'
    assert not result.error.retryable


def test_real_short_platform_photo_generates_all_ten_platforms(tmp_path, monkeypatch):
    from app.level_contracts import LevelReady
    from app.services.level_parser import parse

    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    source = Path(__file__).resolve().parents[1] / 'testdata/levels/real/short-platform.jpg'
    (tmp_path / 'input.png').write_bytes(source.read_bytes())
    result = LevelReady.model_validate(parse(tmp_path))
    assert result.result.analysis.playability == 'not_checked'
    platforms = result.result.level.platforms
    assert len(platforms) == 10
    short = [p for p in platforms if abs(p.start.x - 240) <= 15
             and abs(p.start.y - 180) <= 15]
    assert len(short) == 1
    assert 56 <= short[0].end.x - short[0].start.x <= 109


def _marker_mask(image):
    """与 detect_markers 完全一致的前处理，供直接观测 _inner_dot 用。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 31, 7)
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))


def _thick_line_circle(dot_radius=None, radius=14, thickness=14):
    """半径 14 的圆，圆心下方 14px 处一条 14px 粗的平台线（线顶边距圆心 7px）。"""
    image = np.full((560, 900, 3), 245, np.uint8)
    cv2.line(image, (40, 314), (400, 314), (25, 25, 25), thickness)
    cv2.circle(image, (150, 300), radius, (25, 25, 25), 3)
    if dot_radius:
        cv2.circle(image, (150, 300), dot_radius, (25, 25, 25), -1)
    return image, (150 - radius, 300 - radius, 2 * radius, 2 * radius)


def test_start_circle_with_inner_dot_survives_thick_platform_line(tmp_path):
    """方案 A（圈中点）：细圆被很粗的平台线切过时，内点是唯一能救回起点的正向判据。

    实测该几何（半径 14、线宽 14、线顶边距圆心 7px）下 `_looks_circular` 判定失败、
    `_is_quadrilateral_outline` 判成四边形，轮廓通道也收不到，起点整个丢失；
    圈内点一个点后判据命中，并绕开这两个形状判据，起点恢复。
    """
    from app.services import level_markers

    without_dot, region = _thick_line_circle()
    cv2.imwrite(str(tmp_path / 'without-dot.png'), without_dot)
    assert not level_markers._inner_dot(_marker_mask(without_dot), region)
    assert not detect(tmp_path / 'without-dot.png', tmp_path).start_candidates

    with_dot, region = _thick_line_circle(dot_radius=3)
    cv2.imwrite(str(tmp_path / 'with-dot.png'), with_dot)
    assert level_markers._inner_dot(_marker_mask(with_dot), region)
    result = detect(tmp_path / 'with-dot.png', tmp_path)
    assert len(result.start_candidates) == 1
    assert abs(result.start_candidates[0].x - 150) <= 8


@pytest.mark.parametrize('shape', ['slot-end', 'long-slot-end', 'small-box',
                                   'sharp-corner', 'paper-noise', 'plain-circle'])
def test_pseudo_circle_families_have_no_inner_dot(shape):
    """白名单的前置条件：已知伪圆家族环内必须取不到内点，否则会成倍放大误检。"""
    from app.services import level_markers

    ink = (25, 25, 25)
    image = np.full((560, 900, 3), 245, np.uint8)
    if shape in ('slot-end', 'long-slot-end'):
        length = 260 if shape == 'slot-end' else 420
        cv2.line(image, (120, 362), (120 + length, 362), ink, 4)
        cv2.line(image, (120, 398), (120 + length, 398), ink, 4)
        cv2.ellipse(image, (120, 380), (18, 18), 0, 90, 270, ink, 4)
        region = (102, 362, 36, 36)
    elif shape == 'small-box':
        cv2.rectangle(image, (100, 352), (130, 380), ink, 3)
        region = (100, 352, 30, 28)
    elif shape == 'sharp-corner':
        cv2.rectangle(image, (80, 240), (320, 380), ink, 4)
        region = (302, 362, 36, 36)
    elif shape == 'paper-noise':
        cv2.polylines(image, [np.array([[300, 408], [307, 410], [308, 417], [301, 419],
                                        [296, 415], [297, 411]], np.int32)],
                      True, ink, 1)
        region = (296, 408, 12, 11)
    else:
        cv2.circle(image, (120, 380), 18, ink, 3)
        region = (99, 362, 35, 35)
    assert not level_markers._inner_dot(_marker_mask(image), region)


def test_dot_outside_the_ring_is_not_an_inner_dot(tmp_path):
    """点必须落在环内：画在圈外的点不能触发白名单（否则任何涂鸦都能当起点）。"""
    from app.services import level_markers

    image, region = _thick_line_circle(dot_radius=3)
    image = np.full((560, 900, 3), 245, np.uint8)
    cv2.line(image, (40, 314), (400, 314), (25, 25, 25), 14)
    cv2.circle(image, (150, 300), 14, (25, 25, 25), 3)
    cv2.circle(image, (250, 300), 3, (25, 25, 25), -1)
    assert not level_markers._inner_dot(_marker_mask(image), region)


def test_scribbled_ring_interior_is_not_an_inner_dot():
    """环内涂满小点不是「点一个点」：超过上限的点数一律不算，避免涂鸦被当成起点。"""
    from app.services import level_markers

    image, region = _thick_line_circle()
    for offset in (-8, -4, 0, 4, 8):
        cv2.circle(image, (150 + offset, 300), 2, (25, 25, 25), -1)
    assert not level_markers._inner_dot(_marker_mask(image), region)
