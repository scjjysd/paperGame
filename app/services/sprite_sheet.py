"""透明 GIF -> 横向透明 PNG 精灵表。帧尺寸取所有帧内容包围盒的并集，逐帧居中粘贴。

scale 参数用于跨动作角色大小归一化：渲染器对 run/jump 产生的角色像素高度不同，
将每个动画的角色内容等比缩放到统一目标高度后，再居中放入帧中，保证 Unity 显示时
视觉大小一致。
"""
from pathlib import Path
from typing import Dict, Optional, Tuple

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


def _union_bbox(frames):
    """所有帧内容包围盒的并集（跨帧角色位移不裁切）；全透明返回 None。"""
    left, top, right, bottom = None, None, None, None
    for f in frames:
        bbox = f.getbbox()  # 非透明像素包围盒
        if bbox is None:
            continue
        left = bbox[0] if left is None else min(left, bbox[0])
        top = bbox[1] if top is None else min(top, bbox[1])
        right = bbox[2] if right is None else max(right, bbox[2])
        bottom = bbox[3] if bottom is None else max(bottom, bbox[3])
    return None if left is None else (left, top, right, bottom)


def compute_content_bbox(gif_path) -> Tuple[int, int, int, int]:
    """GIF 所有帧内容包围盒的并集 (left, top, right, bottom)，供角色级统一帧尺寸复用。"""
    frames = _load_frames(Path(gif_path))
    if not frames:
        raise ValueError(f'GIF contains no frames: {gif_path}')
    bbox = _union_bbox(frames)
    if bbox is None:
        raise ValueError(f'GIF frames are fully transparent: {gif_path}')
    return bbox


def build_sprite_sheet(gif_path, out_path, fps: int = 12,
                       frame_size: Optional[Tuple[int, int]] = None,
                       scale: float = 1.0) -> Dict:
    """frame_size=(w,h) 给定时用它作为统一帧尺寸（内容水平居中、底部对齐粘贴）；不给定时按本 GIF 自身并集。

    scale 对裁切后的角色内容做等比缩放（>1 放大、<1 缩小），用于跨动作角色大小归一化。
    """
    gif_path, out_path = Path(gif_path), Path(out_path)
    frames = _load_frames(gif_path)
    if not frames:
        raise ValueError(f'GIF contains no frames: {gif_path}')

    bbox = _union_bbox(frames)
    if bbox is None:
        raise ValueError(f'GIF frames are fully transparent: {gif_path}')
    left, top, right, bottom = bbox
    content_w, content_h = right - left, bottom - top

    # 按 scale 等比缩放角色内容
    if scale != 1.0 and content_w > 0 and content_h > 0:
        scaled_w = max(1, round(content_w * scale))
        scaled_h = max(1, round(content_h * scale))
    else:
        scaled_w, scaled_h = content_w, content_h

    if frame_size is not None:
        frame_w, frame_h = frame_size
        offset_x, offset_y = (frame_w - scaled_w) // 2, frame_h - scaled_h
    else:
        frame_w, frame_h = scaled_w, scaled_h
        offset_x = offset_y = 0

    sheet = Image.new('RGBA', (frame_w * len(frames), frame_h), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        cropped = f.crop((left, top, right, bottom))
        if scale != 1.0 and (scaled_w, scaled_h) != (content_w, content_h):
            cropped = cropped.resize((scaled_w, scaled_h), Image.LANCZOS)
        sheet.paste(cropped, (i * frame_w + offset_x, offset_y))
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
