"""角色动画管线门面：PNG -> 透明 PNG 精灵表 + 元数据。"""
from pathlib import Path
from typing import Dict, Sequence

from app.services.annotations import analyze
from app.services.render_scene import render_animation
from app.services.sprite_sheet import build_sprite_sheet, compute_content_bbox

FPS = 12


class CharacterPipeline:
    def __init__(self, assets_dir, out_root):
        self.assets_dir = Path(assets_dir)
        self.out_root = Path(out_root)

    def render(self, input_path, motion: str) -> Dict:
        """motion in {'run', 'jump'}。失败时抛 NeedsCorrection（由 API 层转 needs_correction）。"""
        motion_bvh = self.assets_dir / f'{motion}.bvh'
        motion_cfg = self.assets_dir / f'{motion}.yaml'
        if not motion_cfg.exists() or not motion_bvh.exists():
            raise FileNotFoundError(f'motion assets missing: {motion}')

        out_dir = self.out_root / Path(input_path).stem / motion
        out_dir.mkdir(parents=True, exist_ok=True)

        analyze(input_path, out_dir / 'anno')
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

        motion_cfgs = {}
        for m in motions:
            motion_cfg = self.assets_dir / f'{m}.yaml'
            if not motion_cfg.exists() or not (self.assets_dir / f'{m}.bvh').exists():
                raise FileNotFoundError(f'motion assets missing: {m}')
            motion_cfgs[m] = motion_cfg

        gifs = {}
        for m in motions:
            out_dir = char_dir / m
            out_dir.mkdir(parents=True, exist_ok=True)
            gifs[m] = render_animation(char_dir / 'anno', motion_cfgs[m], out_dir / f'{m}.gif')

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
