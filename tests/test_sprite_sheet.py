from pathlib import Path
from PIL import Image
import pytest

from app.services.sprite_sheet import build_sprite_sheet


@pytest.fixture
def three_frame_gif(tmp_path):
    """3 帧 RGBA GIF：每帧左上角有一个 40x40 的不透明方块。"""
    frames = []
    for i in range(3):
        img = Image.new('RGBA', (100, 120), (0, 0, 0, 0))
        for x in range(40):
            for y in range(40):
                img.putpixel((x + i * 5, y), (255, 0, 0, 255))  # 每帧方块横移 5px
        frames.append(img)
    p = tmp_path / 'anim.gif'
    frames[0].save(p, save_all=True, append_images=frames[1:], duration=83, disposal=2, loop=0)
    return p


def test_build_sprite_sheet_returns_transparent_png_and_metadata(three_frame_gif, tmp_path):
    out_png = tmp_path / 'run.png'
    meta = build_sprite_sheet(gif_path=three_frame_gif, out_path=out_png, fps=12)

    assert meta['frameCount'] == 3
    assert meta['fps'] == 12
    assert meta['frameWidth'] > 0 and meta['frameHeight'] > 0

    sheet = Image.open(out_png)
    assert sheet.mode == 'RGBA'
    assert sheet.size == (meta['frameWidth'] * 3, meta['frameHeight'])


def test_foot_anchor_is_bottom_of_content(three_frame_gif, tmp_path):
    out_png = tmp_path / 'run.png'
    meta = build_sprite_sheet(gif_path=three_frame_gif, out_path=out_png, fps=12)
    # 方块底边在 y=39，脚底锚点应指向内容底部区域而非画布外
    assert 30 <= meta['footAnchor']['y'] <= meta['frameHeight']
    assert 0 < meta['footAnchor']['x'] < meta['frameWidth']


def test_single_frame_gif(tmp_path):
    img = Image.new('RGBA', (50, 60), (0, 0, 0, 0))
    img.putpixel((10, 10), (0, 0, 255, 255))
    p = tmp_path / 'one.gif'
    img.save(p)
    meta = build_sprite_sheet(gif_path=p, out_path=tmp_path / 'o.png', fps=12)
    assert meta['frameCount'] == 1


def _make_gif(tmp_path, name, canvas_size, content_rect, color=(255, 0, 0, 255)):
    """创建单帧 GIF：在 content_rect 区域内填充不透明像素。"""
    img = Image.new('RGBA', canvas_size, (0, 0, 0, 0))
    for x in range(content_rect[0], content_rect[0] + content_rect[2]):
        for y in range(content_rect[1], content_rect[1] + content_rect[3]):
            img.putpixel((x, y), color)
    p = tmp_path / name
    img.save(p)
    return p


def test_build_sprite_sheet_with_scale_enlarges_content(tmp_path):
    """scale=2.0 应将 20x30 的角色内容放大到约 40x60。"""
    gif = _make_gif(tmp_path, 'small.gif', (100, 100), (10, 10, 20, 30))
    meta = build_sprite_sheet(gif_path=gif, out_path=tmp_path / 'out.png', fps=12, scale=2.0)

    sheet = Image.open(tmp_path / 'out.png')
    # 无 frame_size 时，帧尺寸 = 缩放后内容尺寸
    assert meta['frameWidth'] == 40
    assert meta['frameHeight'] == 60
    assert sheet.size == (40, 60)

    # 验证精灵表内确实有缩放后的不透明像素（中心区域应不透明）
    center_pixel = sheet.getpixel((20, 30))
    assert center_pixel[3] > 0  # alpha > 0


def test_build_sprite_sheet_with_scale_shrinks_content(tmp_path):
    """scale=0.5 应将 40x60 的角色内容缩小到约 20x30。"""
    gif = _make_gif(tmp_path, 'big.gif', (100, 100), (10, 10, 40, 60))
    meta = build_sprite_sheet(gif_path=gif, out_path=tmp_path / 'out.png', fps=12, scale=0.5)

    assert meta['frameWidth'] == 20
    assert meta['frameHeight'] == 30


def test_different_scales_produce_same_character_height(tmp_path):
    """两个不同原始大小的 GIF，经各自 scale 缩放到同一目标高度后，精灵表中角色像素高度一致。"""
    # 小角色：20x30，大角色：40x60。目标高度 = 60，小角色 scale=2.0，大角色 scale=1.0
    small_gif = _make_gif(tmp_path, 'small.gif', (100, 100), (10, 10, 20, 30))
    big_gif = _make_gif(tmp_path, 'big.gif', (100, 100), (10, 10, 40, 60))

    target_h = 60
    small_scale = target_h / 30  # 2.0
    big_scale = target_h / 60    # 1.0

    small_meta = build_sprite_sheet(small_gif, tmp_path / 'small.png', fps=12, scale=small_scale)
    big_meta = build_sprite_sheet(big_gif, tmp_path / 'big.png', fps=12, scale=big_scale)

    # 缩放后角色内容高度一致（都等于 target_h）
    assert small_meta['frameHeight'] == big_meta['frameHeight'] == target_h
