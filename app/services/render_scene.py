"""生成 AnimatedDrawings 渲染场景 YAML 并执行渲染，输出透明 GIF。"""
import logging
import os
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import yaml

from app.services import motion_2d
from app.services.annotations import VENDOR      # vendor 路径单一来源，不在本模块重复推导
# 纯二维骨架（motion_2d 合成）必须配 flat2d：三组投影面全锁 frontal。
# vendor 的 fair1_ppf 用 pca，会给 run 的下肢选中 sagittal 投影、把二维动作压掉；
# 它只留作旧三维动捕资产（app/assets/motions/*.bvh）的回退，不再是默认。
RETARGET_CFG = str(motion_2d.RETARGET_CFG)
VENDOR_RETARGET_CFG = str(VENDOR / 'examples' / 'config' / 'retarget' / 'fair1_ppf.yaml')

# vendor 默认相机 [0, 0.7, 2.0] 对「pose 把骨架识别得偏扁」的输入不够：实测 s07（猪）
# 顶到画布上沿、头顶被裁（顶部 y=0）。拉到 2.8 后顶部留 19px 余量；代价是角色占画布
# 从 65% 降到 50%，而精灵表按内容包围盒裁切，最终分辨率仍够 Unity 用。
CAMERA_POS = [0.0, 0.7, 2.8]
MotionRender = Tuple[str, Path, Path]
logger = logging.getLogger(__name__)


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


def _validate_motions(motions: Sequence[MotionRender]) -> List[MotionRender]:
    items = [(name, Path(cfg).resolve(), Path(output).resolve())
             for name, cfg, output in motions]
    if not items:
        raise ValueError('批量渲染至少一个动作')
    names = [item[0] for item in items]
    if len(names) != len(set(names)):
        raise ValueError('批量渲染动作名不能重复')
    return items


def _load_vendor_components():
    """延迟导入 OpenGL 依赖，使批量编排可在无图形环境中完成契约测试。"""
    from animated_drawings.config import Config
    from animated_drawings.controller.video_render_controller import VideoRenderController
    from animated_drawings.model.animated_drawing import AnimatedDrawing
    from animated_drawings.model.scene import Scene
    from animated_drawings.view.view import View
    return Config, VideoRenderController, AnimatedDrawing, Scene, View


def _switch_motion(drawing, scene, motion_cfg, retarget_cfg) -> None:
    """以旧动作首帧恢复 rig 后，再安装下一个动作的独立 retarget 配置。"""
    drawing.set_time(0.0)
    drawing.update()
    drawing.retarget_cfg = retarget_cfg
    drawing._modify_retargeting_cfg_for_character()
    drawing._initialize_retargeter_bvh(motion_cfg, retarget_cfg)
    scene.set_time(0.0)
    drawing.set_time(0.0)
    drawing.update()


def _logged_animated_drawing_class(animated_drawing):
    """以运行时子类替代 vendor monkeypatch，保留项目层扩展点。"""
    class _LoggedAnimatedDrawing(animated_drawing):
        pass
    return _LoggedAnimatedDrawing


def _sequential_controller_class(video_render_controller):
    """每个动作收尾 writer，但把唯一的 View 留给批次 finally。"""
    class _SequentialVideoRenderController(video_render_controller):
        def _cleanup_after_run_loop(self) -> None:
            if getattr(self, '_sequential_cleanup_done', False):
                return
            self._sequential_cleanup_done = True
            logger.info('顺序渲染完成：%d 帧', self.frames_rendered)
            try:
                self.progress_bar.close()
            finally:
                self.video_writer.cleanup()
    return _SequentialVideoRenderController


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
    if not VENDOR.is_dir():
        # 先校验再干活：本异常会被 render_runner 归为 ASSET_MISSING，响应里只有错误码，
        # 排查全靠日志里这句带路径与补救动作的提示；放在导入与写文件前还能避开无用产物。
        raise FileNotFoundError(
            f'AnimatedDrawings 目录不存在，无法在其中执行渲染：{VENDOR}（宿主先跑 scripts/setup/setup-vendor.sh）')
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


def render_animations(char_anno_dir, motions: Sequence[MotionRender], use_mesa=None,
                      retarget_cfg=None) -> Dict[str, Path]:
    items = _validate_motions(motions)
    if not VENDOR.is_dir():
        raise FileNotFoundError(
            f'AnimatedDrawings 目录不存在，无法在其中执行渲染：{VENDOR}（宿主先跑 scripts/setup/setup-vendor.sh）')

    scene_yamls = []
    for _, motion_cfg, output_gif in items:
        cfg = build_scene_cfg(char_anno_dir, motion_cfg, output_gif,
                              use_mesa=use_mesa, retarget_cfg=retarget_cfg)
        scene_yaml = output_gif.with_suffix('.scene.yaml')
        scene_yaml.write_text(yaml.safe_dump(cfg))
        scene_yamls.append(scene_yaml)

    Config, VideoRenderController, AnimatedDrawing, Scene, View = _load_vendor_components()
    cwd = os.getcwd()
    view = None
    try:
        os.chdir(VENDOR)
        configs = [Config(str(scene_yaml)) for scene_yaml in scene_yamls]
        first_character = configs[0].scene.animated_characters[0]
        configs[0].scene.animated_characters = []
        scene = Scene(configs[0].scene)
        view = View.create_view(configs[0].view)

        LoggedAnimatedDrawing = _logged_animated_drawing_class(AnimatedDrawing)
        drawing = LoggedAnimatedDrawing(*first_character)
        scene.add_child(drawing)
        SequentialVideoRenderController = _sequential_controller_class(VideoRenderController)

        rendered = {}
        for index, (name, _, output_gif) in enumerate(items):
            if index:
                _, action_retarget_cfg, action_motion_cfg = configs[index].scene.animated_characters[0]
                _switch_motion(drawing, scene, action_motion_cfg, action_retarget_cfg)
            controller = SequentialVideoRenderController(configs[index].controller, scene, view)
            try:
                controller.run()
            except Exception:
                controller._cleanup_after_run_loop()
                raise
            if not output_gif.exists():
                raise RuntimeError('render finished but gif missing: %s' % output_gif)
            rendered[name] = output_gif
        return rendered
    finally:
        try:
            if view is not None:
                view.cleanup()
        finally:
            os.chdir(cwd)
