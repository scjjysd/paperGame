import sys
import signal
import time
from pathlib import Path

import pytest

from app.services import annotations
from app.services.annotations import NeedsCorrection

VENDOR_EXAMPLES = Path(__file__).parent.parent / 'vendor' / 'AnimatedDrawings' / 'examples'


@pytest.fixture(autouse=True)
def _vendor_on_path():
    sys.path.insert(0, str(VENDOR_EXAMPLES))
    yield
    sys.path.remove(str(VENDOR_EXAMPLES))


def test_analyze_missing_image_raises_needs_correction(tmp_path):
    with pytest.raises(NeedsCorrection) as e:
        annotations.analyze(tmp_path / 'not_exist.png', tmp_path / 'out')
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
    result = annotations.analyze(img, out_dir)
    assert (out_dir / 'mask.png').exists()
    assert (out_dir / 'texture.png').exists()
    assert (out_dir / 'char_cfg.yaml').exists()
    assert result['skeleton_len'] == 16


def _install_vendor(monkeypatch, implementation):
    """替换慢且外部的 vendor 入口，保留 analyze 本身的异常与计时行为。"""
    class VendorModule:
        image_to_annotations = staticmethod(implementation)

    monkeypatch.setitem(sys.modules, 'image_to_annotations', VendorModule)


def test_analyze_timeout_is_not_needs_correction(tmp_path, monkeypatch):
    """删掉 deadline 包裹会让阻塞 vendor 调用超时失败。"""
    image = tmp_path / 'input.png'
    image.write_bytes(b'png')
    monkeypatch.setenv('ANALYZE_TIMEOUT_SECONDS', '0.01')

    def blocks(img_path, out_dir):
        time.sleep(1)

    _install_vendor(monkeypatch, blocks)

    with pytest.raises(annotations.AnalysisTimeout) as exc_info:
        annotations.analyze(image, tmp_path / 'anno')
    assert not isinstance(exc_info.value, NeedsCorrection)


@pytest.mark.parametrize('value', ['0', '-1', 'abc', 'nan', 'inf'])
def test_invalid_analysis_timeout_is_rejected(value, monkeypatch):
    """删掉正数校验会让无效环境配置静默进入分析调用。"""
    monkeypatch.setenv('ANALYZE_TIMEOUT_SECONDS', value)
    with pytest.raises(ValueError, match='ANALYZE_TIMEOUT_SECONDS'):
        annotations._analysis_timeout_seconds()


@pytest.mark.skipif(not hasattr(signal, 'SIGALRM') or not hasattr(signal, 'ITIMER_REAL'),
                    reason='platform does not support SIGALRM timers')
def test_analysis_deadline_restores_handler_and_remaining_timer(tmp_path, monkeypatch):
    """删掉退出恢复会吞掉调用方已有的 SIGALRM handler 或计时器。"""
    image = tmp_path / 'input.png'
    image.write_bytes(b'png')
    monkeypatch.setenv('ANALYZE_TIMEOUT_SECONDS', '1')

    def completes(img_path, out_dir):
        output = Path(out_dir)
        output.mkdir()
        (output / 'char_cfg.yaml').write_text('skeleton: []\nheight: 1\nwidth: 1\n')

    _install_vendor(monkeypatch, completes)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def previous_alarm(signum, frame):
        pass

    try:
        signal.signal(signal.SIGALRM, previous_alarm)
        signal.setitimer(signal.ITIMER_REAL, 30)
        timer_started = time.monotonic()
        annotations.analyze(image, tmp_path / 'anno')
        restored_delay, restored_interval = signal.getitimer(signal.ITIMER_REAL)
        elapsed = time.monotonic() - timer_started
        assert signal.getsignal(signal.SIGALRM) is previous_alarm
        assert 30 - elapsed - 0.05 < restored_delay <= 30 - elapsed + 0.05
        assert restored_interval == 0
    finally:
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def test_analysis_deadline_with_unsupported_signal_warns_and_completes(tmp_path, monkeypatch, caplog):
    """删掉降级路径会在非主线程安装 signal handler 并引发 ValueError。"""
    image = tmp_path / 'input.png'
    image.write_bytes(b'png')
    class NonMainThread:
        name = 'test-non-main-thread'

    monkeypatch.setattr(annotations.threading, 'current_thread', NonMainThread)
    monkeypatch.setattr(annotations.threading, 'main_thread', object)

    def completes(img_path, out_dir):
        output = Path(out_dir)
        output.mkdir()
        (output / 'char_cfg.yaml').write_text('skeleton: []\nheight: 1\nwidth: 1\n')

    _install_vendor(monkeypatch, completes)
    result = annotations.analyze(image, tmp_path / 'anno')
    assert result == {'skeleton_len': 0, 'height': 1, 'width': 1}
    assert '总超时未启用' in caplog.text
