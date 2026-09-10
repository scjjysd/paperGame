"""motion_2d 的地基断言：生成的 BVH 经 vendor Retargeter 反算，首帧角度必须等于原画角度。

首帧一致 = ARAP 第 0 帧无需强行掰动 = 零初始变形，这是整套方案的立足点。
坐标系约定（图像 y 向下 / vendor y 向上、关节对顺序、投影面）极易搞错，故用往返断言兜住。
"""
import math

import pytest
import yaml

from app.services.motion_2d import (AMP_FLOOR, RECIPE, RETARGET_CFG, amplitude_scale,
                                    char_bone_angles, synth_motion)

# 手举高 + 腿叉开的火柴人：复现孩子实际画法（也是当前 jump 首帧要掰 179° 的那类姿态）
SKELETON = [
    ('root', (100, 150), None), ('hip', (100, 150), 'root'),
    ('torso', (115, 100), 'hip'), ('neck', (100, 40), 'torso'),   # 躯干前倾：hip→torso 与 torso→neck 差约 30°
    ('right_shoulder', (75, 95), 'torso'), ('right_elbow', (50, 60), 'right_shoulder'),
    ('right_hand', (30, 20), 'right_elbow'),
    ('left_shoulder', (125, 95), 'torso'), ('left_elbow', (150, 60), 'left_shoulder'),
    ('left_hand', (170, 20), 'left_elbow'),
    ('right_hip', (85, 150), 'root'), ('right_knee', (70, 210), 'right_hip'),
    ('right_foot', (50, 280), 'right_knee'),
    ('left_hip', (115, 150), 'root'), ('left_knee', (130, 210), 'left_hip'),
    ('left_foot', (150, 280), 'left_knee'),
]


def _char_cfg(tmp_path, skeleton=SKELETON):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / 'char_cfg.yaml'
    p.write_text(yaml.safe_dump({
        'width': 200, 'height': 300,
        'skeleton': [{'name': n, 'loc': list(loc), 'parent': par} for n, loc, par in skeleton]}))
    return p


def _angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def _retarget_angles(motion_yaml, frame=0):
    """走 vendor 真实链路反算每根骨头的目标角度（度，0°=朝上，180°=朝下）。"""
    from animated_drawings.config import MotionConfig, RetargetConfig
    from animated_drawings.model.retargeter import Retargeter
    rc = RetargetConfig(str(RETARGET_CFG))
    rt = Retargeter(MotionConfig(str(motion_yaml)), rc)
    for cj, (pj, dj) in rc.char_joint_bvh_joints_mapping.items():
        rt.compute_orientations(pj, dj, cj)
    return {cj: float(v[frame]) for cj, v in rt.char_joint_to_orientation.items()}


@pytest.mark.parametrize('motion', ['run', 'jump'])
def test_首帧角度等于原画角度(tmp_path, motion):
    cfg = _char_cfg(tmp_path)
    want = char_bone_angles(cfg)

    got = _retarget_angles(synth_motion(cfg, motion, tmp_path))

    # 零长骨会让 vendor 归一化除零，角度变 NaN；而 NaN 在 max/比较里恒为 False，会静默溜过断言
    assert all(math.isfinite(v) for v in got.values()), f'{motion} 有骨的角度算成了 NaN'
    worst = max((_angle_diff(got[j], want[j]), j) for j in want)
    assert worst[0] < 1.0, f'{motion} 首帧 {worst[1]} 偏离原画 {worst[0]:.1f}°，应 <1°'


def _angle_series(motion_yaml):
    """每根骨的逐帧目标角度序列。"""
    from animated_drawings.config import MotionConfig, RetargetConfig
    from animated_drawings.model.retargeter import Retargeter
    rc = RetargetConfig(str(RETARGET_CFG))
    rt = Retargeter(MotionConfig(str(motion_yaml)), rc)
    for cj, (pj, dj) in rc.char_joint_bvh_joints_mapping.items():
        rt.compute_orientations(pj, dj, cj)
    return {cj: [float(x) for x in v] for cj, v in rt.char_joint_to_orientation.items()}


def test_走路摆幅等于配方峰值(tmp_path):
    """摆动必须真的发生且不超配方——幅度是变形的唯一来源，失控就等于回到原来的问题。"""
    cfg = _char_cfg(tmp_path)
    want = char_bone_angles(cfg)
    series = _angle_series(synth_motion(cfg, 'run', tmp_path, amp_scale=1.0))

    base = max(_angle_diff(v, want[cj]) for cj in ('left_knee', 'right_knee', 'left_elbow', 'right_elbow')
               for v in series[cj])
    # 末端骨（小腿/小臂）必须单独查：局部旋转漏减父角时只有它们会翻倍，大腿大臂看不出来。
    # 小腿还叠了膝弯，上限是 limb + 2*bend（1-cos 的幅值是 2）。
    tip = max(_angle_diff(v, want[cj]) for cj in ('left_foot', 'right_foot', 'left_hand', 'right_hand')
              for v in series[cj])
    r = RECIPE['run']
    assert r['limb'] * 0.9 < base < r['limb'] * 1.1, f'大腿大臂峰值 {base:.1f}°'
    assert tip < (r['limb'] + 2 * r['bend']) * 1.1, f'末端峰值 {tip:.1f}°，漏减父角会翻倍'


def test_走路时双腿反相(tmp_path):
    cfg = _char_cfg(tmp_path)
    want = char_bone_angles(cfg)
    series = _angle_series(synth_motion(cfg, 'run', tmp_path, amp_scale=1.0))

    def signed(cj, i):
        return (series[cj][i] - want[cj] + 180) % 360 - 180

    products = [signed('left_knee', i) * signed('right_knee', i) for i in range(len(series['left_knee']))]
    assert min(products) < -1.0, '双腿始终同向，走路会变成并腿蹦'


def test_跳跃不靠大幅转肢体(tmp_path):
    """跳只应是「上下 + 轻微摆」——用户明确说上下就够，转得多少就变形多少。"""
    cfg = _char_cfg(tmp_path)
    want = char_bone_angles(cfg)
    series = _angle_series(synth_motion(cfg, 'jump', tmp_path, amp_scale=1.0))

    peak = max(_angle_diff(v, want[cj]) for cj, vs in series.items() for v in vs)
    assert peak <= RECIPE['jump']['limb'] * 1.1, f'跳跃肢体转了 {peak:.1f}°，应 ≤{RECIPE["jump"]["limb"]}°'


def test_非人形骨架幅度自动压到下限(tmp_path):
    """用户裁定：猪这类非人形不拦，改为幅度自动调小。"""
    normal = _char_cfg(tmp_path / 'a')
    # 复现 s07（猪）：pose 模型硬套人体骨架，一侧手臂被拉到对侧，双臂长差约 48%
    lopsided = _char_cfg(tmp_path / 'b',
                         [(n, (30, 20) if n == 'left_hand' else loc, p) for n, loc, p in SKELETON])

    assert amplitude_scale(normal) > 0.9
    assert amplitude_scale(lopsided) < 0.5
    assert amplitude_scale(lopsided) >= AMP_FLOOR


def test_渲染默认走二维retarget配置(tmp_path):
    """flat2d（三组全 frontal）是纯二维骨架的前提：换回 pca 会让 run 的下肢改走 sagittal 投影，
    把只存在于 ZY 平面的动作整体压掉（实测大腿摆幅 76°→19°）。"""
    from app.services.render_scene import build_scene_cfg
    anno = tmp_path / 'anno'
    anno.mkdir()
    motion = synth_motion(_char_cfg(tmp_path), 'run', tmp_path)

    cfg = build_scene_cfg(anno, motion, tmp_path / 'o.gif', use_mesa=False)

    assert cfg['scene']['ANIMATED_CHARACTERS'][0]['retarget_cfg'] == str(RETARGET_CFG)


def test_渲染视口给畸形骨架留余量(tmp_path):
    """pose 把骨架识别得偏扁时（s07 猪），角色会顶到画布上沿被裁掉头顶：
    实测默认相机 z=2.0 下 s07 触上边、顶部 y=0；拉到 2.8 后顶部留 19px 余量，
    代价是角色占画布从 65% 降到 50%（精灵表按内容裁切，够 Unity 用）。
    改回默认会静默裁头，故用断言钉住。"""
    from app.services.render_scene import build_scene_cfg
    anno = tmp_path / 'anno'
    anno.mkdir()
    motion = synth_motion(_char_cfg(tmp_path), 'jump', tmp_path)

    cfg = build_scene_cfg(anno, motion, tmp_path / 'o.gif', use_mesa=False)

    assert cfg['view']['CAMERA_POS'][2] >= 2.8, '相机太近，畸形骨架会被裁头'


def _motion_lines(bvh_path):
    lines = bvh_path.read_text().splitlines()
    return [ln for ln in lines[lines.index('MOTION') + 3:] if ln.strip()]


def test_每帧姿态互不相同(tmp_path):
    """纯 sin 摆动 + 纯 cos 起伏在 i 与 n/2-i 处取值完全相同，GIF 编码会合并这些重复帧
    （实测配方写 10 帧只出 8 帧），既白算两帧、循环处也会顿一下。膝弯项负责打破该对称。"""
    cfg = _char_cfg(tmp_path)
    synth_motion(cfg, 'run', tmp_path)

    lines = _motion_lines(tmp_path / 'run.bvh')
    assert len(lines) == RECIPE['run']['frames']
    assert len(set(lines)) == len(lines), f'{len(lines) - len(set(lines))} 帧与别的帧完全重复'
