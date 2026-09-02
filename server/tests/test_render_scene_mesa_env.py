from pathlib import Path

import yaml

from app.services.render_scene import build_scene_cfg


def _cfg(tmp_path, monkeypatch, env_value):
    if env_value is None:
        monkeypatch.delenv('RENDER_USE_MESA', raising=False)
    else:
        monkeypatch.setenv('RENDER_USE_MESA', env_value)
    anno = tmp_path / 'anno'
    anno.mkdir()
    motion = tmp_path / 'm.yaml'
    motion.write_text(yaml.safe_dump({'filepath': str(tmp_path / 'm.bvh')}))
    (tmp_path / 'm.bvh').write_text('dummy')
    return build_scene_cfg(anno, motion, tmp_path / 'o.gif', use_mesa=None)


def test_env_true_enables_mesa(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, 'true')
    assert cfg['view'] == {'USE_MESA': True}


def test_env_1_enables_mesa(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, '1')
    assert cfg['view'] == {'USE_MESA': True}


def test_env_absent_disables_mesa(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, None)
    assert 'view' not in cfg


def test_explicit_param_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv('RENDER_USE_MESA', 'true')
    anno = tmp_path / 'anno'
    anno.mkdir()
    motion = tmp_path / 'm.yaml'
    motion.write_text(yaml.safe_dump({'filepath': str(tmp_path / 'm.bvh')}))
    (tmp_path / 'm.bvh').write_text('dummy')
    cfg = build_scene_cfg(anno, motion, tmp_path / 'o.gif', use_mesa=False)
    assert 'view' not in cfg
