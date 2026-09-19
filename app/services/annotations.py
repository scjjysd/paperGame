"""包装 vendor 的 image_to_annotations：把 assert/Exception 失败路径收敛为 NeedsCorrection。"""
import logging
import math
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict

import yaml

logger = logging.getLogger(__name__)

REASONS = {
    'Could not detect any drawn humanoids': 'NO_HUMANOID',
    'Could not detect any skeletons': 'NO_SKELETON',
    'skeletons with the character bounding box': 'MULTIPLE_SKELETONS',
    'Found no contours': 'NO_CONTOUR',
}

# vendor 源码目录：宿主在 <仓库根>/vendor，容器内同结构（Dockerfile COPY vendor/... ./vendor）。
# 由本模块统一推导、渲染层复用：两处各自推导过一次就飘过——server/ 提升至仓库根时
# 渲染层多拼了一段 'server'，os.chdir 抛 FileNotFoundError 被误报成 ASSET_MISSING。
VENDOR = Path(__file__).resolve().parents[2] / 'vendor' / 'AnimatedDrawings'
# vendor 的 examples 目录不在 pip 包内，模块加载时幂等加入 sys.path（裁定 1）
VENDOR_EXAMPLES = VENDOR / 'examples'
_vendor_examples_str = str(VENDOR_EXAMPLES)
if _vendor_examples_str not in sys.path:
    sys.path.insert(0, _vendor_examples_str)


class NeedsCorrection(Exception):
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f'{reason}: {detail}')


class AnalysisTimeout(TimeoutError):
    """图片分析总阶段超过配置时限，交给 worker 作为基础设施故障重试。"""


def _analysis_timeout_seconds() -> float:
    value = os.environ.get('ANALYZE_TIMEOUT_SECONDS', '30')
    try:
        seconds = float(value)
    except ValueError as exc:
        raise ValueError('ANALYZE_TIMEOUT_SECONDS must be a positive number') from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('ANALYZE_TIMEOUT_SECONDS must be a positive number')
    return seconds


def _supports_analysis_deadline() -> bool:
    return (threading.current_thread() is threading.main_thread()
            and hasattr(signal, 'SIGALRM') and hasattr(signal, 'ITIMER_REAL')
            and hasattr(signal, 'getitimer') and hasattr(signal, 'setitimer'))


@contextmanager
def _analysis_deadline(seconds: float):
    """在可用平台以 SIGALRM 限制一次完整 vendor 分析调用。"""
    if not _supports_analysis_deadline():
        logger.warning('图像分析总超时未启用：当前线程或平台不支持 SIGALRM')
        yield
        return

    old_handler = signal.getsignal(signal.SIGALRM)
    old_delay, old_interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def on_timeout(signum, frame):
        raise AnalysisTimeout('image analysis exceeded {:.3f} seconds'.format(seconds))

    handler_install_attempted = False
    try:
        handler_install_attempted = True
        signal.signal(signal.SIGALRM, on_timeout)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        if handler_install_attempted:
            # 即使首次安装 timer 失败，也要撤销可能已经替换的 handler/timer。
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                try:
                    signal.signal(signal.SIGALRM, old_handler)
                finally:
                    elapsed = time.monotonic() - started
                    if old_delay > elapsed:
                        remaining_delay = old_delay - elapsed
                    elif old_interval > 0:
                        periods_elapsed = math.floor((elapsed - old_delay) / old_interval) + 1
                        remaining_delay = old_delay + periods_elapsed * old_interval - elapsed
                    elif old_delay > 0:
                        # 零会取消 timer；用极小正数让内核向已恢复的 handler 投递信号。
                        remaining_delay = 1e-6
                    else:
                        remaining_delay = 0.0
                    signal.setitimer(signal.ITIMER_REAL, remaining_delay, old_interval)


def _classify(msg: str) -> str:
    for k, v in REASONS.items():
        if k in msg:
            return v
    return 'ANALYZE_FAILED'


def analyze(img_path, out_dir) -> Dict:
    img_path, out_dir = Path(img_path), Path(out_dir)
    if not img_path.exists():
        raise NeedsCorrection('NO_HUMANOID', f'input image not found: {img_path}')

    from image_to_annotations import image_to_annotations  # noqa: E402

    try:
        with _analysis_deadline(_analysis_timeout_seconds()):
            image_to_annotations(str(img_path), str(out_dir))
    except AnalysisTimeout:
        raise
    except (AssertionError, Exception) as exc:  # vendor 用 assert False 报错
        raise NeedsCorrection(_classify(str(exc)), str(exc)) from exc

    char_cfg = yaml.safe_load((out_dir / 'char_cfg.yaml').read_text())
    return {'skeleton_len': len(char_cfg['skeleton']),
            'height': char_cfg['height'], 'width': char_cfg['width']}
