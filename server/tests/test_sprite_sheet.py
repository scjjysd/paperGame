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
