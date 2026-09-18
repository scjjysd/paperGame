from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.services import render_scene


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


def _install_lifecycle_fakes(monkeypatch, events, fail_action=None, omit_output=False):
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
            self.frames_rendered = 1
            self.progress_bar = FakeProgress(cfg.name)
            self.video_writer = FakeWriter(cfg.name)

        def run(self):
            events.append(('render.start', self.cfg.name))
            if self.cfg.name == fail_action:
                raise RuntimeError('render failed: %s' % fail_action)
            if not omit_output:
                Path(self.cfg.output_video_path).write_text(self.cfg.name)
            events.append(('render.end', self.cfg.name))
            self._cleanup_after_run_loop()

    monkeypatch.setattr(render_scene, '_load_vendor_components',
                        lambda: (FakeConfig, FakeController, FakeDrawing, FakeScene, FakeView))
    return configs


def _motions(tmp_path):
    motions = []
    for name in ('run', 'jump'):
        motion = tmp_path / (name + '.yaml')
        motion.write_text(yaml.safe_dump({'filepath': str(tmp_path / (name + '.bvh'))}))
        (tmp_path / (name + '.bvh')).write_text('motion')
        motions.append((name, motion, tmp_path / (name + '.gif')))
    return motions


def test_render_animations_reuses_drawing_and_resets_before_next_motion(tmp_path, monkeypatch):
    """遗漏旧动作归零会让 jump 从 run 末帧姿态计算新的 retargeter。"""
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


@pytest.mark.parametrize('fail_action', ['run', 'jump'])
def test_render_animations_cleans_view_once_when_an_action_fails(tmp_path, monkeypatch, fail_action):
    """任一动作抛错时必须收尾该动作资源，并只释放一次唯一 OpenGL View。"""
    events = []
    _install_lifecycle_fakes(monkeypatch, events, fail_action=fail_action)

    with pytest.raises(RuntimeError, match='render failed'):
        render_scene.render_animations(tmp_path, _motions(tmp_path))

    assert events.count(('progress.close', fail_action)) == 1
    assert events.count(('writer.cleanup', fail_action)) == 1
    assert events.count('view.cleanup') == 1


def test_render_animations_fails_when_controller_does_not_create_gif(tmp_path, monkeypatch):
    """控制器静默返回但没有产物时，不能把不存在的 GIF 发布给后续精灵表流程。"""
    events = []
    _install_lifecycle_fakes(monkeypatch, events, omit_output=True)

    with pytest.raises(RuntimeError, match='gif missing'):
        render_scene.render_animations(tmp_path, _motions(tmp_path))

    assert events.count('view.cleanup') == 1
