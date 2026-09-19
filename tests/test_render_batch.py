import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import yaml
from PIL import Image

from app.services import render_scene


def test_mesh_metrics_calculates_unique_edges_and_dense_bytes():
    """重复三角边若未去重，会把 ARAP 的峰值内存估算放大。"""
    vertices = np.zeros((4, 2), dtype=np.float32)
    triangles = [np.array([0, 1, 2]), np.array([1, 2, 3])]

    metrics = render_scene._mesh_metrics(vertices, triangles, pin_count=2)

    assert metrics['vertices'] == 4
    assert metrics['triangles'] == 2
    assert metrics['edges'] == 5
    assert metrics['a1_rows'] == 14
    assert metrics['a1_cols'] == 8
    assert metrics['a1_bytes'] == 14 * 8 * 4
    assert metrics['g_bytes'] == 10 * 8 * 4
    assert metrics['a2_bytes'] == 7 * 4 * 4
    assert metrics['normal1_bytes'] == 8 * 8 * 4
    assert metrics['normal2_bytes'] == 4 * 4 * 4


def test_logged_animated_drawing_logs_dropped_pin_after_arap_construction(caplog, monkeypatch):
    """移动网格 hook 到 ARAP 后会失去 OOM 前的关键诊断日志。"""
    caplog.set_level(logging.INFO, logger=render_scene.__name__)
    events = []
    original_info = render_scene.logger.info

    def record_info(message, *args, **kwargs):
        if message.startswith('ARAP 构造前'):
            events.append('estimated.log')
        elif message.startswith('ARAP 构造后'):
            events.append('actual.log')
        original_info(message, *args, **kwargs)

    monkeypatch.setattr(render_scene.logger, 'info', record_info)

    class FakeBase:
        def __init__(self):
            self.mask = np.zeros((3, 4), dtype=np.uint8)
            self.char_cfg = SimpleNamespace(skeleton=[
                {'name': 'a', 'loc': [0.25, 0.5]},
                {'name': 'b', 'loc': [0.75, 0.5]},
            ])
            self._generate_mesh()
            events.append('arap.construct')
            self.arap = SimpleNamespace(
                pin_num=1,
                pin_mask=np.array([True, False]),
                A1=np.zeros((2, 4), dtype=np.float32),
                A2=np.zeros((1, 2), dtype=np.float32),
            )

        def _generate_mesh(self):
            self.mesh = {
                'vertices': np.zeros((4, 2), dtype=np.float32),
                'triangles': [np.array([0, 1, 2]), np.array([1, 2, 3])],
            }
            events.append('mesh.generated')

    Drawing = render_scene._logged_animated_drawing_class(FakeBase)
    Drawing()

    messages = [record.getMessage() for record in caplog.records]
    estimated_index = next(index for index, message in enumerate(messages)
                           if 'ARAP 构造前' in message)
    actual_index = next(index for index, message in enumerate(messages)
                        if 'ARAP 构造后' in message)
    assert 'mask=4x3' in messages[estimated_index]
    assert 'vertices=4' in messages[estimated_index]
    assert 'triangles=2' in messages[estimated_index]
    assert 'edges=5' in messages[estimated_index]
    assert 'pins=2' in messages[estimated_index]
    assert 'A1=14x8/' in messages[estimated_index]
    assert (events.index('mesh.generated') < events.index('estimated.log') <
            events.index('arap.construct') < events.index('actual.log'))
    assert 'effective_pins=1' in messages[actual_index]
    assert 'A1.shape=(2, 4)' in messages[actual_index]
    assert 'A1.nbytes=32' in messages[actual_index]
    assert 'A2.shape=(1, 2)' in messages[actual_index]
    assert 'A2.nbytes=8' in messages[actual_index]
    dropped_pin_message = next(message for message in messages if 'dropped=1/2' in message)
    assert 'b' in dropped_pin_message
    assert 'normalized_loc=[0.75, 0.5]' in dropped_pin_message


def test_logged_animated_drawing_warns_without_breaking_when_pin_diagnostics_fields_missing(caplog):
    """诊断字段缺失时不得把 vendor 正常构造转换为渲染失败。"""
    caplog.set_level(logging.WARNING, logger=render_scene.__name__)

    class FakeBase:
        def __init__(self):
            self.arap = SimpleNamespace(pin_num=1,
                                        A1=np.zeros((1, 1), dtype=np.float32),
                                        A2=np.zeros((1, 1), dtype=np.float32))

    Drawing = render_scene._logged_animated_drawing_class(FakeBase)

    Drawing()

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert '丢失 pin 诊断失败' in messages[0]


def test_render_animations_rejects_empty_motion_list(tmp_path):
    """空批次若继续进入 vendor 初始化，会创建无意义的 OpenGL 资源。"""
    with pytest.raises(ValueError, match='至少一个动作'):
        render_scene.render_animations(tmp_path, [])


def test_render_animations_rejects_duplicate_motion_names(tmp_path):
    """重复名称会覆盖返回值并使两个 GIF 的归属无法判定。"""
    motion = tmp_path / 'm.yaml'
    with pytest.raises(ValueError, match='动作名不能重复'):
        render_scene.render_animations(
            tmp_path,
             [('run', motion, tmp_path / 'a.gif'),
              ('run', motion, tmp_path / 'b.gif')])


@pytest.mark.parametrize('motions, pattern', [
    ([], '至少一个动作'),
    ([('run', Path('run.yaml'), Path('run.gif')),
      ('run', Path('jump.yaml'), Path('jump.gif'))], '动作名不能重复'),
])
def test_render_animations_validates_inputs_before_checking_vendor_assets(tmp_path, monkeypatch,
                                                                          motions, pattern):
    """坏输入不得被无关的 vendor 缺失掩盖，否则调用方无法修正动作参数。"""
    monkeypatch.setattr(render_scene, 'VENDOR', tmp_path / 'missing-vendor')

    with pytest.raises(ValueError, match=pattern):
        render_scene.render_animations(tmp_path, motions)


def _install_lifecycle_fakes(monkeypatch, events, fail_action=None, omit_output=False,
                             render_error=None, writer_cleanup_error=None, declared_frames=1,
                             frames_rendered=1, gif_frames=1):
    """替代缓慢的 OpenGL/vendor 边界，保留批量编排本身的所有真实调用。"""
    configs = []

    class FakeConfig:
        def __init__(self, scene_yaml):
            raw = yaml.safe_load(Path(scene_yaml).read_text())
            character = raw['scene']['ANIMATED_CHARACTERS'][0]
            name = Path(character['motion_cfg']).stem.split('.')[0]
            item = (SimpleNamespace(name='character'),
                    SimpleNamespace(name=name), SimpleNamespace(name=name))
            self.scene = SimpleNamespace(animated_characters=[item])
            self.view = SimpleNamespace(name='view')
            self.controller = SimpleNamespace(output_video_path=raw['controller']['OUTPUT_VIDEO_PATH'], name=name)
            configs.append(self)

    class FakeDrawing:
        def __init__(self, character, retarget_cfg, motion_cfg):
            self.retarget_cfg = retarget_cfg
            self.motion_cfg = motion_cfg
            self.char_cfg = SimpleNamespace(skeleton=[{'name': 'root', 'loc': [0.5, 0.5]}])
            self.arap = SimpleNamespace(pin_num=1, pin_mask=np.array([True]),
                                        A1=np.zeros((2, 2), dtype=np.float32),
                                        A2=np.zeros((1, 1), dtype=np.float32))
            self.retargeter = SimpleNamespace(
                bvh=SimpleNamespace(frame_max_num=declared_frames))
            events.append(('drawing.create', motion_cfg.name))

        def set_time(self, value):
            events.append(('drawing.time', value))

        def update(self):
            events.append('drawing.update')

        def _modify_retargeting_cfg_for_character(self):
            events.append('drawing.runtime-check')

        def _initialize_retargeter_bvh(self, motion_cfg, retarget_cfg):
            self.motion_cfg = motion_cfg
            events.append(('drawing.retarget', motion_cfg.name))

    class FakeScene:
        def __init__(self, cfg):
            assert cfg.animated_characters == []
            self.children = []

        def add_child(self, child):
            self.children.append(child)

        def set_time(self, value):
            events.append(('scene.time', value))

    class FakeView:
        @staticmethod
        def create_view(cfg):
            events.append('view.create')
            return FakeView()

        def cleanup(self):
            events.append('view.cleanup')

    class FakeWriter:
        def __init__(self, name):
            self.name = name

        def cleanup(self):
            events.append(('writer.cleanup', self.name))
            if writer_cleanup_error is not None:
                raise writer_cleanup_error('writer cleanup failed')

    class FakeProgress:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(('progress.close', self.name))

    class FakeController:
        def __init__(self, cfg, scene, view):
            self.cfg = cfg
            self.scene = scene
            self.view = view
            self.frames_rendered = frames_rendered
            self.progress_bar = FakeProgress(cfg.name)
            self.video_writer = FakeWriter(cfg.name)

        def run(self):
            events.append(('render.start', self.cfg.name))
            if self.cfg.name == fail_action:
                if render_error is not None:
                    raise render_error('original render failure')
                raise RuntimeError('render failed: %s' % fail_action)
            if not omit_output:
                frames = [Image.new('RGBA', (2, 2), (index, 0, 0, 255))
                          for index in range(gif_frames)]
                frames[0].save(self.cfg.output_video_path, save_all=True,
                               append_images=frames[1:], format='GIF', loop=0)
            events.append(('render.end', self.cfg.name))
            self._cleanup_after_run_loop()

    def load_components():
        return FakeConfig, FakeView

    monkeypatch.setattr(render_scene, '_load_vendor_components', load_components)
    monkeypatch.setattr(render_scene, '_load_render_components',
                        lambda: (FakeDrawing, FakeScene, FakeController))
    return configs


def _motions(tmp_path):
    motions = []
    for name in ('run', 'jump'):
        motion = tmp_path / (name + '.yaml')
        motion.write_text(yaml.safe_dump({'filepath': str(tmp_path / (name + '.bvh'))}))
        (tmp_path / (name + '.bvh')).write_text('motion')
        motions.append((name, motion, tmp_path / (name + '.gif')))
    return motions


def test_render_animations_creates_mesa_view_before_loading_video_controller(tmp_path, monkeypatch):
    """MesaView 必须先设置 PyOpenGL 平台，controller 顶层导入 GL 才不会锁到 GLX。"""
    events = []

    class FakeConfig:
        def __init__(self, scene_yaml):
            raw = yaml.safe_load(Path(scene_yaml).read_text())
            character = raw['scene']['ANIMATED_CHARACTERS'][0]
            item = (SimpleNamespace(name='character'),
                    SimpleNamespace(name='run'), SimpleNamespace(name='run'))
            self.scene = SimpleNamespace(animated_characters=[item])
            self.view = SimpleNamespace(name='mesa')
            self.controller = SimpleNamespace(output_video_path=raw['controller']['OUTPUT_VIDEO_PATH'],
                                              name='run')

    class FakeView:
        @staticmethod
        def create_view(cfg):
            events.append('view.create')
            return SimpleNamespace(cleanup=lambda: events.append('view.cleanup'))

    class FakeDrawing:
        def __init__(self, *args):
            self.arap = SimpleNamespace(pin_num=1, A1=np.zeros((1, 1)), A2=np.zeros((1, 1)))

    class FakeScene:
        def __init__(self, cfg):
            pass

        def add_child(self, child):
            pass

    class FakeController:
        def __init__(self, cfg, scene, view):
            self.cfg = cfg
            self.frames_rendered = 0
            self.progress_bar = SimpleNamespace(close=lambda: None)
            self.video_writer = SimpleNamespace(cleanup=lambda: None)

        def run(self):
            Image.new('RGBA', (2, 2), (0, 0, 0, 0)).save(self.cfg.output_video_path, format='GIF')

    monkeypatch.setattr(render_scene, '_load_vendor_components',
                        lambda: (FakeConfig, FakeView))
    monkeypatch.setattr(render_scene, '_load_render_components',
                        lambda: (events.append('drawing.import') or FakeDrawing,
                                 FakeScene,
                                 events.append('controller.import') or FakeController))

    render_scene.render_animations(tmp_path, [_motions(tmp_path)[0]], use_mesa=True)

    assert events == ['view.create', 'drawing.import', 'controller.import', 'view.cleanup']


def test_vendor_loaders_keep_opengl_modules_out_of_early_import_subprocess(tmp_path):
    """真实 early loader 不能提前加载会锁定 PyOpenGL 平台的 vendor 模块。"""
    shim_dir = tmp_path / 'shim'
    shim_dir.mkdir()
    if importlib.util.find_spec('pkg_resources') is None:
        (shim_dir / 'pkg_resources.py').write_text(
            'def resource_filename(_package, resource):\n    return resource\n')

    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python_path = [str(shim_dir), str(repo_root / 'vendor' / 'AnimatedDrawings')]
    if env.get('PYTHONPATH'):
        python_path.append(env['PYTHONPATH'])
    env['PYTHONPATH'] = os.pathsep.join(python_path)
    script = """
import json
import sys
from app.services import render_scene

tracked = (
    'animated_drawings.model.animated_drawing',
    'animated_drawings.controller.video_render_controller',
    'OpenGL.GL',
)
before = [name for name in tracked if name in sys.modules]
render_scene._load_vendor_components()
early = [name for name in tracked if name in sys.modules]
render_scene._load_render_components()
late = [name for name in tracked if name in sys.modules]
print(json.dumps({'before': before, 'early': early, 'late': late}))
"""
    result = subprocess.run([sys.executable, '-c', script], env=env, text=True,
                            capture_output=True, check=True)
    observed = json.loads(result.stdout)

    assert observed['before'] == []
    assert observed['early'] == []
    assert observed['late'] == [
        'animated_drawings.model.animated_drawing',
        'animated_drawings.controller.video_render_controller',
        'OpenGL.GL',
    ]


def test_render_animations_reuses_drawing_and_resets_before_next_motion(tmp_path, monkeypatch, caplog):
    """遗漏旧动作归零会让 jump 从 run 末帧姿态计算新的 retargeter。"""
    caplog.set_level(logging.INFO, logger=render_scene.__name__)
    events = []
    configs = _install_lifecycle_fakes(monkeypatch, events)

    result = render_scene.render_animations(tmp_path, _motions(tmp_path))

    assert list(result) == ['run', 'jump']
    assert list(result.values()) == [tmp_path / 'run.gif', tmp_path / 'jump.gif']
    assert [event for event in events if isinstance(event, tuple) and event[0] == 'drawing.create'] == [
        ('drawing.create', 'run')]
    assert [event for event in events if isinstance(event, tuple) and event[0] == 'render.start'] == [
        ('render.start', 'run'), ('render.start', 'jump')]
    reset_index = events.index(('drawing.time', 0.0))
    update_index = events.index('drawing.update', reset_index)
    jump_retarget_index = events.index(('drawing.retarget', 'jump'))
    assert reset_index < update_index < jump_retarget_index
    assert ('scene.time', 0.0) in events[jump_retarget_index:]
    assert events.count(('progress.close', 'run')) == 1
    assert events.count(('writer.cleanup', 'run')) == 1
    assert events.count(('progress.close', 'jump')) == 1
    assert events.count(('writer.cleanup', 'jump')) == 1
    assert events.count('view.cleanup') == 1
    assert configs[0].scene.animated_characters == []
    assert configs[0].scene is not configs[1].scene
    assert '动作 run retarget' in caplog.text
    assert '动作 run 渲染' in caplog.text
    assert '动作 jump retarget' in caplog.text
    assert '动作 jump 渲染' in caplog.text


@pytest.mark.parametrize('fail_action', ['run', 'jump'])
def test_render_animations_cleans_view_once_when_an_action_fails(tmp_path, monkeypatch,
                                                                 caplog, fail_action):
    """任一动作抛错时必须收尾该动作资源，并只释放一次唯一 OpenGL View。"""
    caplog.set_level(logging.INFO, logger=render_scene.__name__)
    events = []
    _install_lifecycle_fakes(monkeypatch, events, fail_action=fail_action)

    with pytest.raises(RuntimeError, match='render failed'):
        render_scene.render_animations(tmp_path, _motions(tmp_path))

    assert events.count(('progress.close', fail_action)) == 1
    assert events.count(('writer.cleanup', fail_action)) == 1
    assert events.count('view.cleanup') == 1
    assert 'stage=render_%s' % fail_action in caplog.text


def test_render_animations_preserves_render_error_when_action_cleanup_fails(tmp_path, monkeypatch,
                                                                             caplog):
    """空帧 GIF 收尾失败不能覆盖 OpenGL 首帧前的真实渲染异常。"""
    class OriginalRenderError(RuntimeError):
        pass

    class CleanupError(RuntimeError):
        pass

    events = []
    caplog.set_level(logging.ERROR, logger=render_scene.__name__)
    _install_lifecycle_fakes(monkeypatch, events, fail_action='run',
                             render_error=OriginalRenderError,
                             writer_cleanup_error=CleanupError)

    with pytest.raises(OriginalRenderError, match='original render failure'):
        render_scene.render_animations(tmp_path, _motions(tmp_path))

    assert events.count(('progress.close', 'run')) == 1
    assert events.count(('writer.cleanup', 'run')) == 1
    assert events.count('view.cleanup') == 1
    assert 'action=run' in caplog.text
    assert 'writer cleanup failed' in caplog.text


def test_render_animations_fails_when_controller_does_not_create_gif(tmp_path, monkeypatch):
    """控制器静默返回但没有产物时，不能把不存在的 GIF 发布给后续精灵表流程。"""
    events = []
    _install_lifecycle_fakes(monkeypatch, events, omit_output=True)

    with pytest.raises(RuntimeError, match='gif missing'):
        render_scene.render_animations(tmp_path, _motions(tmp_path))

    assert events.count('view.cleanup') == 1


def test_render_animations_logs_matching_declared_rendered_and_gif_frame_counts(tmp_path,
                                                                                  monkeypatch, caplog):
    """三方帧数一致时应留下可审计的正常渲染诊断。"""
    caplog.set_level(logging.INFO, logger=render_scene.__name__)
    _install_lifecycle_fakes(monkeypatch, [], declared_frames=2, frames_rendered=2,
                             gif_frames=2)

    render_scene.render_animations(tmp_path, [_motions(tmp_path)[0]])

    assert '声明=2，渲染=2，GIF=2' in caplog.text
    assert not [record for record in caplog.records if record.levelno == logging.WARNING]


def test_render_animations_frame_count_warns_when_gif_encoder_merges_repeated_frames(tmp_path,
                                                                                      monkeypatch, caplog):
    """编码器丢帧不能被 controller 已渲染帧数掩盖。"""
    caplog.set_level(logging.WARNING, logger=render_scene.__name__)
    _install_lifecycle_fakes(monkeypatch, [], declared_frames=8, frames_rendered=8,
                             gif_frames=7)

    render_scene.render_animations(tmp_path, [_motions(tmp_path)[0]])

    assert '声明=8，渲染=8，GIF=7' in caplog.text
    assert '编码器可能合并连续重复帧' in caplog.text
