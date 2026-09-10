"""角色动画管线门面：PNG -> 透明 PNG 精灵表 + 元数据。"""
from pathlib import Path
from typing import Dict, Sequence

from app.services.annotation_repair import repair_or_reject
from app.services.annotations import analyze
from app.services.motion_2d import FPS, synth_motion
from app.services.render_scene import render_animation
from app.services.sprite_sheet import build_sprite_sheet, compute_content_bbox


class CharacterPipeline:
    """动作不再来自外部动捕资产，而是按每张画自己的骨架现场合成（见 motion_2d）。"""

    def __init__(self, out_root):
        self.out_root = Path(out_root)

    def render(self, input_path, motion: str) -> Dict:
        """motion in {'run', 'jump'}。失败时抛 NeedsCorrection（由 API 层转 needs_correction）。"""
        out_dir = self.out_root / Path(input_path).stem / motion
        out_dir.mkdir(parents=True, exist_ok=True)

        analyze(input_path, out_dir / 'anno')
        repair_or_reject(out_dir / 'anno')   # 补回被 vendor 丢弃的部件；修不好则 needs_correction
        # 合成动作必须在 repair 之后：用的是修好并吸附过的关节，否则首帧姿态对不上原画
        motion_cfg = synth_motion(out_dir / 'anno' / 'char_cfg.yaml', motion, out_dir)
        gif = render_animation(out_dir / 'anno', motion_cfg, out_dir / f'{motion}.gif')
        meta = build_sprite_sheet(gif, out_dir / f'{motion}.png', fps=FPS)

        return {
            'status': 'ready',
            'animations': {motion: {**meta, 'spriteSheetUrl': str(out_dir / f'{motion}.png')}},
        }

    def render_character(self, input_path, motions: Sequence[str] = ('run', 'jump')) -> Dict:
        """角色级入口：标注只做一次，帧尺寸取所有动作帧内容包围盒的并集，保证各动作一致。"""
        char_dir = self.out_root / Path(input_path).stem
        (char_dir / 'anno').mkdir(parents=True, exist_ok=True)
        analyze(input_path, char_dir / 'anno')
        repair_or_reject(char_dir / 'anno')  # 补回被 vendor 丢弃的部件；修不好则 needs_correction
        char_cfg = char_dir / 'anno' / 'char_cfg.yaml'

        gifs = {}
        for m in motions:
            out_dir = char_dir / m
            out_dir.mkdir(parents=True, exist_ok=True)
            gifs[m] = render_animation(char_dir / 'anno', synth_motion(char_cfg, m, out_dir),
                                       out_dir / f'{m}.gif')

        # 所有动作全部帧内容包围盒的并集 -> 统一帧尺寸
        left = top = None
        right = bottom = 0
        for gif in gifs.values():
            l, t, r, b = compute_content_bbox(gif)
            left, top = l if left is None else min(left, l), t if top is None else min(top, t)
            right, bottom = max(right, r), max(bottom, b)
        frame_size = (right - left, bottom - top)

        animations = {}
        for m in motions:
            out_dir = char_dir / m
            meta = build_sprite_sheet(gifs[m], out_dir / f'{m}.png', fps=FPS, frame_size=frame_size)
            animations[m] = {**meta, 'spriteSheetUrl': str(out_dir / f'{m}.png')}

        return {'status': 'ready', 'animations': animations}
