"""生成 AnimatedDrawings 渲染场景 YAML 并执行渲染，输出透明 GIF。"""
from contextlib import contextmanager
import logging
import os
import time
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


@contextmanager
def _timed_render_stage(stage: str, action: str) -> None:
    """即使 vendor 阶段失败也输出已耗时间，保留慢失败诊断信息。"""
    started = time.perf_counter()
    try:
        yield
    finally:
        logger.info('角色渲染计时：action=%s stage=%s elapsed=%.3fs',
                    action, stage, time.perf_counter() - started)


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


def _mesh_metrics(vertices, triangles, pin_count: int) -> Dict[str, int]:
    """根据已经生成的网格估算 ARAP 稠密矩阵的内存规模。"""
    edges = set()
    for v0, v1, v2 in triangles:
        edges.update((
            tuple(sorted((int(v0), int(v1)))),
            tuple(sorted((int(v1), int(v2)))),
            tuple(sorted((int(v2), int(v0)))),
        ))
    vertex_count, edge_count = len(vertices), len(edges)
    a1_rows, a1_cols = 2 * (edge_count + pin_count), 2 * vertex_count
    a2_rows, a2_cols = edge_count + pin_count, vertex_count
    return {
        'vertices': vertex_count,
        'triangles': len(triangles),
        'edges': edge_count,
        'pins': pin_count,
        'a1_rows': a1_rows,
        'a1_cols': a1_cols,
        'a1_bytes': a1_rows * a1_cols * 4,
        'g_bytes': (2 * edge_count) * a1_cols * 4,
        'a2_rows': a2_rows,
        'a2_cols': a2_cols,
        'a2_bytes': a2_rows * a2_cols * 4,
        'normal1_bytes': a1_cols * a1_cols * 4,
        'normal2_bytes': a2_cols * a2_cols * 4,
    }


def _load_vendor_components():
    """先仅加载不触发 OpenGL 平台选择的配置与 View 工厂。"""
    from animated_drawings.config import Config
    from animated_drawings.view.view import View
    return Config, View


def _load_render_components():
    """MesaView 设置 PyOpenGL 平台后才导入所有 GL 相关渲染类型。"""
    from animated_drawings.model.animated_drawing import AnimatedDrawing
    from animated_drawings.model.scene import Scene
    from animated_drawings.controller.video_render_controller import VideoRenderController
    return AnimatedDrawing, Scene, VideoRenderController


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
        def _generate_mesh(self) -> None:
            super()._generate_mesh()
            metrics = _mesh_metrics(self.mesh['vertices'], self.mesh['triangles'],
                                    pin_count=len(self.char_cfg.skeleton))
            mask_height, mask_width = self.mask.shape[:2]
            logger.info(
                'ARAP 构造前：mask=%dx%d，vertices=%d，triangles=%d，edges=%d，pins=%d，'
                'A1=%dx%d/%d bytes，G=%d bytes，A2=%dx%d/%d bytes，'
                'normal1=%d bytes，normal2=%d bytes',
                mask_width, mask_height, metrics['vertices'], metrics['triangles'],
                metrics['edges'], metrics['pins'], metrics['a1_rows'], metrics['a1_cols'],
                metrics['a1_bytes'], metrics['g_bytes'], metrics['a2_rows'], metrics['a2_cols'],
                metrics['a2_bytes'], metrics['normal1_bytes'], metrics['normal2_bytes'])

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            logger.info('ARAP 构造后：effective_pins=%d，A1.shape=%s，A1.nbytes=%d，'
                        'A2.shape=%s，A2.nbytes=%d',
                        self.arap.pin_num, self.arap.A1.shape, self.arap.A1.nbytes,
                        self.arap.A2.shape, self.arap.A2.nbytes)
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
        output_gif.parent.mkdir(parents=True, exist_ok=True)
        cfg = build_scene_cfg(char_anno_dir, motion_cfg, output_gif,
                              use_mesa=use_mesa, retarget_cfg=retarget_cfg)
        scene_yaml = output_gif.with_suffix('.scene.yaml')
        scene_yaml.write_text(yaml.safe_dump(cfg))
        scene_yamls.append(scene_yaml)

    Config, View = _load_vendor_components()
    cwd = os.getcwd()
    view = None
    try:
        os.chdir(VENDOR)
        configs = [Config(str(scene_yaml)) for scene_yaml in scene_yamls]
        first_character = configs[0].scene.animated_characters[0]
        configs[0].scene.animated_characters = []
        view = View.create_view(configs[0].view)
        AnimatedDrawing, Scene, VideoRenderController = _load_render_components()
        scene = Scene(configs[0].scene)

        LoggedAnimatedDrawing = _logged_animated_drawing_class(AnimatedDrawing)
        first_name = items[0][0]
        static_init_started = time.perf_counter()
        with _timed_render_stage('static_scene_init', first_name):
            with _timed_render_stage('retarget_%s' % first_name, first_name):
                drawing = LoggedAnimatedDrawing(*first_character)
        static_init_elapsed = time.perf_counter() - static_init_started
        logger.info('静态角色初始化（含网格、ARAP 与动作 %s retarget）完成，耗时 %.3f 秒',
                    first_name, static_init_elapsed)
        logger.info('动作 %s retarget 完成（包含静态角色初始化），耗时 %.3f 秒',
                    first_name, static_init_elapsed)
        scene.add_child(drawing)
        SequentialVideoRenderController = _sequential_controller_class(VideoRenderController)

        rendered = {}
        for index, (name, _, output_gif) in enumerate(items):
            if index:
                _, action_retarget_cfg, action_motion_cfg = configs[index].scene.animated_characters[0]
                retarget_started = time.perf_counter()
                with _timed_render_stage('retarget_%s' % name, name):
                    _switch_motion(drawing, scene, action_motion_cfg, action_retarget_cfg)
                logger.info('动作 %s retarget 完成，耗时 %.3f 秒', name,
                            time.perf_counter() - retarget_started)
            controller = SequentialVideoRenderController(configs[index].controller, scene, view)
            render_started = time.perf_counter()
            with _timed_render_stage('render_%s' % name, name):
                try:
                    controller.run()
                except Exception:
                    try:
                        controller._cleanup_after_run_loop()
                    except Exception:
                        logger.exception('动作渲染异常后的资源清理失败：action=%s', name)
                    raise
            logger.info('动作 %s 渲染完成，耗时 %.3f 秒', name,
                        time.perf_counter() - render_started)
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
