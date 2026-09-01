"""角色动画管线门面：PNG -> 透明 PNG 精灵表 + 元数据。"""
from pathlib import Path
from typing import Dict

from app.services.annotations import analyze
from app.services.render_scene import render_animation
from app.services.sprite_sheet import build_sprite_sheet

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
