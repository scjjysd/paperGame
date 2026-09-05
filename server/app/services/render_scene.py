"""生成 AnimatedDrawings 渲染场景 YAML 并执行渲染，输出透明 GIF。"""
import os
from pathlib import Path

import yaml

from app.services import motion_2d

REPO_ROOT = Path(__file__).resolve().parents[3]          # 仓库根
VENDOR = REPO_ROOT / 'server' / 'vendor' / 'AnimatedDrawings'
# 纯二维骨架（motion_2d 合成）必须配 flat2d：三组投影面全锁 frontal。
# vendor 的 fair1_ppf 用 pca，会给 run 的下肢选中 sagittal 投影、把二维动作压掉；
# 它只留作旧三维动捕资产（app/assets/motions/*.bvh）的回退，不再是默认。
RETARGET_CFG = str(motion_2d.RETARGET_CFG)
VENDOR_RETARGET_CFG = str(VENDOR / 'examples' / 'config' / 'retarget' / 'fair1_ppf.yaml')

# vendor 默认相机 [0, 0.7, 2.0] 对「pose 把骨架识别得偏扁」的输入不够：实测 s07（猪）
# 顶到画布上沿、头顶被裁（顶部 y=0）。拉到 2.8 后顶部留 19px 余量；代价是角色占画布
# 从 65% 降到 50%，而精灵表按内容包围盒裁切，最终分辨率仍够 Unity 用。
CAMERA_POS = [0.0, 0.7, 2.8]


def _resolve_motion_cfg(motion_cfg_fn: Path) -> Path:
    """filepath 按当前工作目录解析失败时，按 motion YAML 所在目录重写为绝对路径并输出 .resolved.yaml。"""
    motion_cfg_fn = motion_cfg_fn.resolve()
    motion = yaml.safe_load(motion_cfg_fn.read_text())
    fp = Path(motion.get('filepath', ''))
    if fp.exists():
        return motion_cfg_fn
    motion['filepath'] = str((motion_cfg_fn.parent / fp.name).resolve())
    resolved = motion_cfg_fn.with_name(motion_cfg_fn.name + '.resolved.yaml')
    resolved.write_text(yaml.safe_dump(motion))
    return resolved


def _mesa_default() -> bool:
    return os.environ.get('RENDER_USE_MESA', '').strip().lower() in ('1', 'true', 'yes')


def build_scene_cfg(char_anno_dir, motion_cfg_fn, out_gif, use_mesa=None, retarget_cfg=None) -> dict:
    """构建 AnimatedDrawings 渲染场景配置。use_mesa=None 时由 RENDER_USE_MESA 环境变量决定。"""
    char_anno_dir, out_gif = Path(char_anno_dir), Path(out_gif)
    if use_mesa is None:
        use_mesa = _mesa_default()
    cfg = {
        'scene': {'ANIMATED_CHARACTERS': [{
            'character_cfg': str(char_anno_dir / 'char_cfg.yaml'),
            'motion_cfg': str(_resolve_motion_cfg(Path(motion_cfg_fn))),
            'retarget_cfg': str(retarget_cfg or RETARGET_CFG),
        }]},
        'controller': {'MODE': 'video_render', 'OUTPUT_VIDEO_PATH': str(out_gif)},
    }
    cfg['view'] = {'CAMERA_POS': list(CAMERA_POS)}
    if use_mesa:
        cfg['view']['USE_MESA'] = True
    return cfg


def render_animation(char_anno_dir, motion_cfg_fn, out_gif, use_mesa=None, retarget_cfg=None) -> Path:
    out_gif = Path(out_gif)
    out_gif.parent.mkdir(parents=True, exist_ok=True)
    cfg = build_scene_cfg(char_anno_dir, motion_cfg_fn, out_gif, use_mesa=use_mesa, retarget_cfg=retarget_cfg)

    # character_cfg 内的相对路径以 vendor 仓库根为基准，渲染需在该目录下执行
    scene_yaml = out_gif.with_suffix('.scene.yaml')
    scene_yaml.write_text(yaml.safe_dump(cfg))

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
