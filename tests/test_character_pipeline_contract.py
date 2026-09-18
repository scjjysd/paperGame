from pathlib import Path
import logging

import pytest

from app.services.character_pipeline import FPS, CharacterPipeline
from app.services.annotations import NeedsCorrection
from app.services import character_pipeline

VENDOR_EXAMPLES = Path(__file__).parent.parent / 'vendor' / 'AnimatedDrawings' / 'examples'


@pytest.fixture
def pipeline(tmp_path):
    return CharacterPipeline(out_root=tmp_path)


def test_render_invalid_input_returns_needs_correction(pipeline, tmp_path):
    with pytest.raises(NeedsCorrection):
        pipeline.render(tmp_path / 'missing.png', motion='run')


def test_render_ready_contains_contract_fields(pipeline):
    """需要 TorchServe 已启动；否则跳过。"""
    import requests
    try:
        requests.get('http://localhost:8080/ping', timeout=2)
    except Exception:
        pytest.skip('TorchServe not running')

    img = VENDOR_EXAMPLES / 'drawings' / 'garlic.png'
    result = pipeline.render(img, motion='run')
    assert result['status'] == 'ready'
    anim = result['animations']['run']
    for key in ('spriteSheetUrl', 'frameCount', 'fps', 'frameWidth', 'frameHeight', 'footAnchor'):
        assert key in anim
    assert anim['fps'] == FPS
    assert Path(result['animations']['run']['spriteSheetUrl']).exists()


def test_render_character_frame_size_consistent_across_motions(pipeline):
    """需要 TorchServe 已启动；否则跳过。"""
    import requests
    try:
        requests.get('http://localhost:8080/ping', timeout=2)
    except Exception:
        pytest.skip('TorchServe not running')

    img = VENDOR_EXAMPLES / 'drawings' / 'garlic.png'
    result = pipeline.render_character(img)
    assert result['status'] == 'ready'
    run, jump = result['animations']['run'], result['animations']['jump']
    assert run['frameWidth'] == jump['frameWidth']
    assert run['frameHeight'] == jump['frameHeight']
    assert Path(run['spriteSheetUrl']).exists()
    assert Path(jump['spriteSheetUrl']).exists()


def test_render_character_batches_motions_in_caller_order_and_logs_stages(
        pipeline, tmp_path, monkeypatch, caplog):
    """逐动作回退到 render_animation 会重复初始化静态角色，并破坏调用方指定的顺序。"""
    caplog.set_level(logging.INFO, logger=character_pipeline.__name__)
    calls = {'batch': [], 'sprites': []}

    monkeypatch.setattr(character_pipeline, 'analyze', lambda *_: None)
    monkeypatch.setattr(character_pipeline, 'repair_or_reject', lambda *_: None)

    def fake_synth(char_cfg, motion, out_dir):
        return Path(out_dir) / (motion + '.yaml')

    def fake_batch(char_anno_dir, motions):
        calls['batch'].append((Path(char_anno_dir), list(motions)))
        return {name: output for name, _, output in motions}

    def fake_bbox(gif):
        return (0, 0, 10, 20) if Path(gif).stem == 'jump' else (0, 0, 5, 10)

    def fake_sprite(gif, output, fps, frame_size, scale):
        calls['sprites'].append((Path(gif), Path(output), fps, frame_size, scale))
        return {'frameCount': 3, 'fps': fps, 'frameWidth': frame_size[0],
                'frameHeight': frame_size[1], 'footAnchor': 0}

    monkeypatch.setattr(character_pipeline, 'synth_motion', fake_synth)
    monkeypatch.setattr(character_pipeline, 'render_animations', fake_batch)
    monkeypatch.setattr(character_pipeline, 'compute_content_bbox', fake_bbox)
    monkeypatch.setattr(character_pipeline, 'build_sprite_sheet', fake_sprite)

    result = pipeline.render_character(tmp_path / 'hero.png', motions=('jump', 'run'))

    assert len(calls['batch']) == 1
    assert [triple[0] for triple in calls['batch'][0][1]] == ['jump', 'run']
    assert [triple[1] for triple in calls['batch'][0][1]] == [
        tmp_path / 'hero' / 'jump' / 'jump.yaml', tmp_path / 'hero' / 'run' / 'run.yaml']
    assert [triple[2] for triple in calls['batch'][0][1]] == [
        tmp_path / 'hero' / 'jump' / 'jump.gif', tmp_path / 'hero' / 'run' / 'run.gif']
    assert [sprite[3] for sprite in calls['sprites']] == [(14, 24), (14, 24)]
    assert [sprite[4] for sprite in calls['sprites']] == [1.0, 2.0]
    assert list(result['animations']) == ['jump', 'run']
    for stage in ('analyze', 'repair', 'synth_motion', 'sprite_sheet', 'total'):
        assert 'stage=%s' % stage in caplog.text


def test_render_character_logs_total_when_repair_needs_correction(pipeline, tmp_path,
                                                                  monkeypatch, caplog):
    """校正终态若跳过 finally，慢失败任务就无法从总耗时日志中定位。"""
    caplog.set_level(logging.INFO, logger=character_pipeline.__name__)
    monkeypatch.setattr(character_pipeline, 'analyze', lambda *_: None)

    def reject(*_):
        raise NeedsCorrection('REPAIR_FAILED', 'needs correction')

    monkeypatch.setattr(character_pipeline, 'repair_or_reject', reject)

    with pytest.raises(NeedsCorrection, match='needs correction'):
        pipeline.render_character(tmp_path / 'hero.png')

    assert 'stage=analyze' in caplog.text
    assert 'stage=repair' in caplog.text
    assert 'stage=total' in caplog.text
