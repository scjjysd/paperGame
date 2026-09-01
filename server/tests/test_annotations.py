import sys
from pathlib import Path

import pytest

from app.services.annotations import NeedsCorrection, analyze

VENDOR_EXAMPLES = Path(__file__).parent.parent / 'vendor' / 'AnimatedDrawings' / 'examples'


@pytest.fixture(autouse=True)
def _vendor_on_path():
    sys.path.insert(0, str(VENDOR_EXAMPLES))
    yield
    sys.path.remove(str(VENDOR_EXAMPLES))


def test_analyze_missing_image_raises_needs_correction(tmp_path):
    with pytest.raises(NeedsCorrection) as e:
        analyze(tmp_path / 'not_exist.png', tmp_path / 'out')
    assert e.value.reason == 'NO_HUMANOID'


def test_analyze_valid_doodle_outputs_annotations(tmp_path):
    """需要 TorchServe 已启动（任务 1 步骤 4 通过）；否则跳过。"""
    import requests
    try:
        requests.get('http://localhost:8080/ping', timeout=2)
    except Exception:
        pytest.skip('TorchServe not running')

    img = VENDOR_EXAMPLES / 'drawings' / 'garlic.png'
    out_dir = tmp_path / 'anno'
    result = analyze(img, out_dir)
    assert (out_dir / 'mask.png').exists()
    assert (out_dir / 'texture.png').exists()
    assert (out_dir / 'char_cfg.yaml').exists()
    assert result['skeleton_len'] == 16
