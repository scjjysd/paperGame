"""包装 vendor 的 image_to_annotations：把 assert/Exception 失败路径收敛为 NeedsCorrection。"""
import logging
import sys
from pathlib import Path
from typing import Dict

import yaml

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
        image_to_annotations(str(img_path), str(out_dir))
    except (AssertionError, Exception) as e:  # vendor 用 assert False 报错
        raise NeedsCorrection(_classify(str(e)), str(e)) from e

    char_cfg = yaml.safe_load((out_dir / 'char_cfg.yaml').read_text())
    return {'skeleton_len': len(char_cfg['skeleton']),
            'height': char_cfg['height'], 'width': char_cfg['width']}
