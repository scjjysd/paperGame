import logging
from pathlib import Path

import pytest

from app.services import character_pipeline
from app.services.character_pipeline import CharacterPipeline, FPS


def _install_pipeline_fakes(monkeypatch, calls, fail_stage=None):
    monkeypatch.setattr(character_pipeline, 'analyze', lambda *_: None)
    monkeypatch.setattr(character_pipeline, 'repair_or_reject', lambda *_: None)

    def fake_synth(char_cfg, motion, out_dir):
        if fail_stage == 'synth_motion.jump' and motion == 'jump':
            raise RuntimeError('jump synth failed')
        return Path(out_dir) / (motion + '.yaml')

    def fake_batch(char_anno_dir, motions):
        calls['batch'].append((Path(char_anno_dir), list(motions)))
        if fail_stage == 'render_animations':
            raise RuntimeError('batch render failed')
        return {name: output for name, _, output in motions}

    def fake_bbox(gif):
        return (0, 0, 10, 20) if Path(gif).stem == 'jump' else (0, 0, 5, 10)

    def fake_sprite(gif, output, fps, frame_size, scale):
        if fail_stage == 'sprite_sheet.jump' and Path(gif).stem == 'jump':
            raise RuntimeError('jump sprite failed')
        calls['sprites'].append((Path(gif), Path(output), fps, frame_size, scale))
        return {'frameCount': 3, 'fps': fps, 'frameWidth': frame_size[0],
                'frameHeight': frame_size[1], 'footAnchor': 0}

    monkeypatch.setattr(character_pipeline, 'synth_motion', fake_synth)
    monkeypatch.setattr(character_pipeline, 'render_animations', fake_batch)
    monkeypatch.setattr(character_pipeline, 'compute_content_bbox', fake_bbox)
    monkeypatch.setattr(character_pipeline, 'build_sprite_sheet', fake_sprite)


def test_render_character_batches_in_caller_order_preserves_sprite_contract_and_logs_each_stage(
        tmp_path, monkeypatch, caplog):
    """逐动作渲染或合并阶段键会掩盖重复静态初始化和具体慢动作。"""
    caplog.set_level(logging.INFO, logger=character_pipeline.__name__)
    calls = {'batch': [], 'sprites': []}
    _install_pipeline_fakes(monkeypatch, calls)

    result = CharacterPipeline(tmp_path).render_character(tmp_path / 'hero.png',
                                                           motions=('jump', 'run'))

    assert len(calls['batch']) == 1
    assert [triple[0] for triple in calls['batch'][0][1]] == ['jump', 'run']
    assert [triple[1] for triple in calls['batch'][0][1]] == [
        tmp_path / 'hero' / 'jump' / 'jump.yaml', tmp_path / 'hero' / 'run' / 'run.yaml']
    assert [triple[2] for triple in calls['batch'][0][1]] == [
        tmp_path / 'hero' / 'jump' / 'jump.gif', tmp_path / 'hero' / 'run' / 'run.gif']
    assert [sprite[2:] for sprite in calls['sprites']] == [
        (FPS, (14, 24), 1.0), (FPS, (14, 24), 2.0)]
    assert list(result['animations']) == ['jump', 'run']
    for stage in ('analyze', 'repair', 'synth_motion.jump', 'synth_motion.run',
                  'render_animations', 'sprite_sheet.jump', 'sprite_sheet.run', 'total'):
        assert 'stage=%s' % stage in caplog.text


@pytest.mark.parametrize('fail_stage, absent_stage', [
    ('synth_motion.jump', 'render_animations'),
    ('render_animations', 'sprite_sheet.jump'),
    ('sprite_sheet.jump', 'sprite_sheet.run'),
])
def test_render_character_logs_failed_exact_stage_and_total_without_future_stage_logs(
        tmp_path, monkeypatch, caplog, fail_stage, absent_stage):
    """失败若只写笼统阶段或继续伪造后续阶段，诊断会错误归因。"""
    caplog.set_level(logging.INFO, logger=character_pipeline.__name__)
    calls = {'batch': [], 'sprites': []}
    _install_pipeline_fakes(monkeypatch, calls, fail_stage=fail_stage)

    with pytest.raises(RuntimeError):
        CharacterPipeline(tmp_path).render_character(tmp_path / 'hero.png',
                                                      motions=('jump', 'run'))

    assert 'stage=%s' % fail_stage in caplog.text
    assert 'stage=total' in caplog.text
    assert 'stage=%s' % absent_stage not in caplog.text
