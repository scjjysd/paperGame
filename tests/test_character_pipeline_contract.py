from pathlib import Path

import pytest

from app.services.character_pipeline import FPS, CharacterPipeline
from app.services.annotations import NeedsCorrection

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
