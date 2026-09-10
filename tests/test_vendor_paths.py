"""vendor 路径推导：仓库布局变动时最容易写歪，歪了会让角色渲染 100% 失败。

历史教训：`server/` 内容提升至仓库根（commit 76775e1）时，`render_scene` 仍按旧的
`parents[3] / 'server' / 'vendor'` 推导，`os.chdir(VENDOR)` 抛 FileNotFoundError，
被 render_runner 的 `except FileNotFoundError` 归成 `failed:ASSET_MISSING`——
响应里只有一个看不出根因的稳定错误码。
"""
import sys
from pathlib import Path

import pytest

from app.services import annotations, render_scene

REPO_ROOT = Path(annotations.__file__).resolve().parents[2]


def test_vendor_path_stays_inside_the_repo():
    """路径必须落在仓库内：跑出仓库等价于「换了目录布局就坏」。"""
    assert annotations.VENDOR == REPO_ROOT / 'vendor' / 'AnimatedDrawings'
    assert annotations.VENDOR.is_relative_to(REPO_ROOT)


def test_render_layer_reuses_the_same_vendor_path():
    """渲染层不得再自己推导一遍——两处各自推导正是这次漂移的成因。"""
    assert render_scene.VENDOR == annotations.VENDOR
    assert render_scene.VENDOR_RETARGET_CFG.startswith(str(annotations.VENDOR))


def test_vendor_examples_is_derived_from_vendor_and_on_sys_path():
    assert annotations.VENDOR_EXAMPLES == annotations.VENDOR / 'examples'
    # 模块加载即幂等注入；漏注入会让 image_to_annotations 报找不到模块，而非清晰的资产缺失
    assert str(annotations.VENDOR_EXAMPLES) in sys.path


def test_missing_vendor_raises_actionable_error(tmp_path, monkeypatch):
    """vendor 真缺失时的报错必须带上路径与补救动作，不能只留一个错误码。"""
    missing = tmp_path / 'no-such-vendor'
    monkeypatch.setattr(render_scene, 'VENDOR', missing)
    anno = tmp_path / 'anno'
    anno.mkdir()
    motion = tmp_path / 'm.yaml'
    motion.write_text('filepath: dummy.bvh\n')

    with pytest.raises(FileNotFoundError) as exc:
        render_scene.render_animation(anno, motion, tmp_path / 'o.gif', use_mesa=False)

    message = str(exc.value)
    assert str(missing) in message
    assert 'setup-vendor.sh' in message
    assert not (tmp_path / 'o.scene.yaml').exists(), '校验应在写产物之前，不然会留垃圾文件'
