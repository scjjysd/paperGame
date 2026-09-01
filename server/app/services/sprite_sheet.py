"""透明 GIF -> 横向透明 PNG 精灵表。帧尺寸取所有帧内容包围盒的并集，逐帧居中粘贴。"""
from pathlib import Path
from typing import Dict

from PIL import Image


def _load_frames(gif_path: Path):
    gif = Image.open(gif_path)
    frames = []
    try:
        while True:
            frames.append(gif.convert('RGBA').copy())
            gif.seek(gif.tell() + 1)
    except EOFError:
        pass
    return frames


def build_sprite_sheet(gif_path, out_path, fps: int = 12) -> Dict:
    gif_path, out_path = Path(gif_path), Path(out_path)
    frames = _load_frames(gif_path)
    if not frames:
        raise ValueError(f'GIF contains no frames: {gif_path}')

    # 所有帧内容包围盒的并集（跨帧角色位移不裁切）
    left, top, right, bottom = None, None, None, None
    for f in frames:
        bbox = f.getbbox()  # 非透明像素包围盒
        if bbox is None:
            continue
        left = bbox[0] if left is None else min(left, bbox[0])
        top = bbox[1] if top is None else min(top, bbox[1])
        right = bbox[2] if right is None else max(right, bbox[2])
        bottom = bbox[3] if bottom is None else max(bottom, bbox[3])
    if left is None:
        raise ValueError(f'GIF frames are fully transparent: {gif_path}')

    frame_w, frame_h = right - left, bottom - top
    sheet = Image.new('RGBA', (frame_w * len(frames), frame_h), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f.crop((left, top, right, bottom)), (i * frame_w, 0))
    sheet.save(out_path)

    # 脚底锚点：帧内水平居中、垂直贴内容底部
    return {
        'spriteSheetUrl': out_path.name,
        'frameCount': len(frames),
        'fps': fps,
        'frameWidth': frame_w,
        'frameHeight': frame_h,
        'footAnchor': {'x': frame_w // 2, 'y': frame_h},
    }
