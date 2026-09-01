"""生成 AnimatedDrawings 渲染场景 YAML 并执行渲染，输出透明 GIF。"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]          # 仓库根
VENDOR = REPO_ROOT / 'server' / 'vendor' / 'AnimatedDrawings'
RETARGET_CFG = str(VENDOR / 'examples' / 'config' / 'retarget' / 'fair1_ppf.yaml')


def render_animation(char_anno_dir, motion_cfg_fn, out_gif, use_mesa=False) -> Path:
    char_anno_dir, out_gif = Path(char_anno_dir), Path(out_gif)
    out_gif.parent.mkdir(parents=True, exist_ok=True)

    cfg = {
        'scene': {'ANIMATED_CHARACTERS': [{
            'character_cfg': str(char_anno_dir / 'char_cfg.yaml'),
            'motion_cfg': str(motion_cfg_fn),
            'retarget_cfg': RETARGET_CFG,
        }]},
        'controller': {'MODE': 'video_render', 'OUTPUT_VIDEO_PATH': str(out_gif)},
    }
    if use_mesa:
        cfg['view'] = {'USE_MESA': True}

    # character_cfg 内的相对路径以 vendor 仓库根为基准，渲染需在该目录下执行
    scene_yaml = out_gif.with_suffix('.scene.yaml')
    scene_yaml.write_text(yaml.safe_dump(cfg))

    import os
    from animated_drawings import render
    cwd = os.getcwd()
    os.chdir(VENDOR)
    try:
        render.start(str(scene_yaml))
    finally:
        os.chdir(cwd)
    if not out_gif.exists():
        raise RuntimeError(f'render finished but gif missing: {out_gif}')
    return out_gif
