"""按涂鸦自身姿态合成纯二维 BVH：首帧＝原画姿态，后续帧只叠加小幅摆动。

为什么不用三维动捕：`jump.bvh` 首帧是「立正站好」（大臂 174°/182°、大腿 178°/180°），
而孩子画的火柴人手举高腿叉开，ARAP 在第 0 帧就要把肢体平均掰 64°（最狠 179°），
画被横向压扁到 50%、手臂压进躯干。实测换任何固定动捕资产（含官方 dab/jumping/
jumping_jacks）最好也只能降到 34°——20 张画姿态各异，固定起始姿势救不了。

做法：BVH 的 OFFSET 直接取原画骨向量（映射到 ZY 平面），于是「零旋转姿态」就是原画姿态；
每帧只写 Xrotation（绕 X 轴＝在 ZY 平面内转），配 flat2d.yaml 的 frontal 投影
（vendor 取 (-z, y)）1:1 还原，x 恒为 0 故零深度分量、零投影压缩。
"""
import math
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import yaml

ASSETS = Path(__file__).resolve().parents[1] / 'assets'
RETARGET_CFG = ASSETS / 'retarget' / 'flat2d.yaml'

# char_joint -> char_cfg 里的 (父, 子)，与 flat2d.yaml 的 char_joint_bvh_joints_mapping 一一对应
BONES = {
    # 注意 neck：retarget 里 BVH 那侧是 Hips->Neck（跨多级），但 char 侧 neck 的父是 torso，
    # vendor 会把那个方向施加到 torso->neck 这根骨上。照抄 BVH 的关节对会让头被硬转
    # （实测 s07 转 25°、s09 23°、s20 22°，s07 的「头」占面积一半，整只猪看着就歪了）。
    'torso': ('hip', 'torso'), 'neck': ('torso', 'neck'),
    'right_elbow': ('right_shoulder', 'right_elbow'), 'right_hand': ('right_elbow', 'right_hand'),
    'left_elbow': ('left_shoulder', 'left_elbow'), 'left_hand': ('left_elbow', 'left_hand'),
    'right_knee': ('right_hip', 'right_knee'), 'right_foot': ('right_knee', 'right_foot'),
    'left_knee': ('left_hip', 'left_knee'), 'left_foot': ('left_knee', 'left_foot'),
}
# BVH 骨架（fair1 命名，flat2d.yaml 引用的关节必须齐全）：(关节, 父)，顺序即深度优先顺序
TREE: Sequence[Tuple[str, Optional[str]]] = (
    ('Hips', None), ('Spine', 'Hips'), ('Spine1', 'Spine'), ('Spine2', 'Spine1'), ('Spine3', 'Spine2'),
    ('Neck', 'Spine3'), ('Head', 'Neck'),
    ('RightShoulder', 'Spine3'), ('RightArm', 'RightShoulder'), ('RightForeArm', 'RightArm'),
    ('RightHand', 'RightForeArm'), ('RightHandEnd', 'RightHand'),
    ('LeftShoulder', 'Spine3'), ('LeftArm', 'LeftShoulder'), ('LeftForeArm', 'LeftArm'),
    ('LeftHand', 'LeftForeArm'), ('LeftHandEnd', 'LeftHand'),
    ('RightUpLeg', 'Hips'), ('RightLeg', 'RightUpLeg'), ('RightFoot', 'RightLeg'), ('RightToeBase', 'RightFoot'),
    ('LeftUpLeg', 'Hips'), ('LeftLeg', 'LeftUpLeg'), ('LeftFoot', 'LeftLeg'), ('LeftToeBase', 'LeftFoot'),
)
# char_joint 的角度由「哪个 BVH 关节的 Xrotation」驱动：该关节转多少，其子骨就偏多少
DRIVER = {'right_elbow': 'RightArm', 'right_hand': 'RightForeArm',
          'left_elbow': 'LeftArm', 'left_hand': 'LeftForeArm',
          'right_knee': 'RightUpLeg', 'right_foot': 'RightLeg',
          'left_knee': 'LeftUpLeg', 'left_foot': 'LeftLeg'}
# 局部旋转 = 目标累积角 - 父累积角，故末端骨要减掉上一节的量
PARENT_ANGLE = {'right_hand': 'right_elbow', 'left_hand': 'left_elbow',
                'right_foot': 'right_knee', 'left_foot': 'left_knee'}

FPS = 15                           # 提速：12fps × 13 帧的走路循环要 1.08s，用户反馈「有点慢」
FRAME_TIME = 1.0 / FPS
MIN_BONE = 1e-3                    # OFFSET 长度下限：全零向量会让 vendor 归一化除零出 NaN


def _load(char_cfg) -> Dict[str, Tuple[float, float]]:
    cfg = yaml.safe_load(Path(char_cfg).read_text())
    return {j['name']: (float(j['loc'][0]), float(j['loc'][1])) for j in cfg['skeleton']}


def _angle(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """骨骼相对 +Y 的 CCW 角度（度）。char_cfg 是图像坐标（y 向下），vendor 用 y 向上。"""
    return (math.degrees(math.atan2(-(b[1] - a[1]), b[0] - a[0])) - 90) % 360


def char_bone_angles(char_cfg) -> Dict[str, float]:
    """原画每根骨的目标角度——合成动画的第 0 帧就必须精确等于它。"""
    loc = _load(char_cfg)
    return {cj: _angle(loc[a], loc[b]) for cj, (a, b) in BONES.items()}


def _flat(a: Tuple[float, float], b: Tuple[float, float]) -> Tuple[float, float, float]:
    """原画向量 -> 纯二维 BVH 的 OFFSET：屏幕横轴映到 -z、纵轴映到 y，x 恒为 0。"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if math.hypot(dx, dy) < MIN_BONE:
        dy = -MIN_BONE                      # 退化骨（如 s14 躯干仅占 3%）给一段朝上的最小长度
    return (0.0, -dy, -dx)


def _scaled(v: Tuple[float, float, float], k: float) -> Tuple[float, float, float]:
    return (v[0] * k, v[1] * k, v[2] * k)


def _offsets(loc: Dict[str, Tuple[float, float]]) -> Dict[str, Tuple[float, float, float]]:
    """零旋转时骨架＝原画骨架。肩/髋宽用固定符号（Left 在 -z、Right 在 +z），
    这样 get_skeleton_fwd 的 Left->Right 恒沿 +Z、forward 恒为 +X，
    不受个别画左右标反的影响（反了会让 rotate_between_vectors 撞零四元数奇点出 NaN）。"""
    trunk = _flat(loc['hip'], loc['torso'])
    spine = _scaled(trunk, 0.25)                             # 四段等分，累积＝ hip->torso
    # vendor 取 BVH 的 Hips->Neck 方向去驱动 char 的 torso->neck 骨，所以这里要让
    # Hips->Neck 这条**累积**向量指向原画的 torso->neck。Neck 自己的 OFFSET 因此是
    # c*A - B（A=torso->neck, B=hip->torso），方向看着怪但累积方向才是被读取的那个；
    # c 取 1+|B|/|A| 只为让这段不退化成零长（零长会让 vendor 归一化除零出 NaN）。
    head_dir = _flat(loc['torso'], loc['neck'])
    la, lb = math.hypot(*head_dir[1:]), math.hypot(*trunk[1:])
    c = 1.0 + lb / max(la, MIN_BONE)
    neck = tuple(c * a - b for a, b in zip(head_dir, trunk))
    sh = max(abs(loc['left_shoulder'][0] - loc['right_shoulder'][0]) / 2, MIN_BONE)
    hw = max(abs(loc['left_hip'][0] - loc['right_hip'][0]) / 2, MIN_BONE)
    o = {'Hips': (0.0, 0.0, 0.0), 'Spine': spine, 'Spine1': spine, 'Spine2': spine, 'Spine3': spine,
         'Neck': neck, 'Head': _scaled(head_dir, 0.3)}
    for side, sign in (('right', 1.0), ('left', -1.0)):
        S = side.capitalize()
        o[f'{S}Shoulder'] = (0.0, _flat(loc['torso'], loc[f'{side}_shoulder'])[1], sign * sh)
        o[f'{S}Arm'] = (0.0, 0.0, 0.0)      # 不参与任何角度，留零
        o[f'{S}ForeArm'] = _flat(loc[f'{side}_shoulder'], loc[f'{side}_elbow'])
        o[f'{S}Hand'] = _flat(loc[f'{side}_elbow'], loc[f'{side}_hand'])
        o[f'{S}HandEnd'] = _scaled(o[f'{S}Hand'], 0.2)
        o[f'{S}UpLeg'] = (0.0, _flat(loc['hip'], loc[f'{side}_hip'])[1], sign * hw)
        o[f'{S}Leg'] = _flat(loc[f'{side}_hip'], loc[f'{side}_knee'])
        o[f'{S}Foot'] = _flat(loc[f'{side}_knee'], loc[f'{side}_foot'])
        o[f'{S}ToeBase'] = _scaled(o[f'{S}Foot'], 0.2)
    return o


def _hierarchy(offsets) -> str:
    kids: Dict[Optional[str], list] = {}
    for name, parent in TREE:
        kids.setdefault(parent, []).append(name)
    out = ['HIERARCHY']

    def emit(name: str, depth: int) -> None:
        pad = '\t' * (depth + 1)
        out.append(f'{pad}{"ROOT" if depth == 0 else "JOINT"} {name}')
        out.append(f'{pad}{{')
        out.append('{}\tOFFSET {:.5f} {:.5f} {:.5f}'.format(pad, *offsets[name]))
        out.append(f'{pad}\tCHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation'
                   if depth == 0 else f'{pad}\tCHANNELS 3 Zrotation Yrotation Xrotation')
        if name in kids:
            for k in kids[name]:
                emit(k, depth + 1)
        else:
            out.extend([f'{pad}\tEnd Site', f'{pad}\t{{',
                        f'{pad}\t\tOFFSET 0.00000 -1.00000 0.00000', f'{pad}\t}}'])
        out.append(f'{pad}}}')

    emit('Hips', 0)
    return '\n'.join(out)


# 摆动配方。走路式小幅摆是产品裁定：宁可动作朴素，也不要画被扭曲。
# limb/arm 为峰值角度（度），bob 为身体起伏、air 为腾空，均按 char_cfg.height 取比例。
# 幅度按用户目视迭代过一轮：初版 15°/8° 被评「动作太弱、旧版跑跳更好看」。
# 旧三维资产的宽度振幅是初版的 1.7~2.6 倍（s02 35.7% vs 13.9%），故上调到接近那个观感；
# 敢上调是因为首帧已零偏差——旧版的变形是「首帧掰 64° + 摆幅 76°」叠加出来的，不是摆幅单独造成。
# bend 是小腿相对大腿的额外膝弯。它还有个必须存在的理由：纯 sin 摆动 + 纯 cos 起伏在
# i 与 n/2-i 处取值完全相同，GIF 编码会把重复帧合并（实测 10 帧只出 8 帧）；
# 1-cos(2πi/n) 关于 i=n/2 而非 i=n/4 对称，正好打破它，且 i=0 时仍为 0（首帧零偏差）。
RECIPE = {
    'run':  {'frames': 10, 'limb': 35.0, 'arm': 35.0, 'bend': 8.0, 'bob': 0.02, 'air': 0.0},
    'jump': {'frames': 8, 'limb': 18.0, 'arm': 18.0, 'bend': 0.0, 'bob': 0.0, 'air': 0.14},
}
AMP_FLOOR = 0.33          # 幅度下限倍数：非人形（猪/崩坏骨架）不拦，但压到 1/3


def _dist(a, b) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def amplitude_scale(char_cfg) -> float:
    """骨架越不像人形，摆动幅度越小（用户裁定：非人形不拦，改为幅度自动调小）。

    阈值取自 20 份代理样本实测：s07（猪）双臂长差 69% / 腿长仅占身高 20%，
    s15（骨架崩坏）双腿差 59%；正常样本 s02 双臂差 2% / 腿长 28%。
    真人参考：腿长约占身高 45%、左右差 <5%。换样本集需用 diagnose_annotations.py 重标定。
    """
    loc = _load(char_cfg)
    height = max(_dist(loc['neck'], loc['left_foot']), _dist(loc['neck'], loc['right_foot']), MIN_BONE)
    legs = [_dist(loc[f'{s}_hip'], loc[f'{s}_knee']) + _dist(loc[f'{s}_knee'], loc[f'{s}_foot'])
            for s in ('left', 'right')]
    arms = [_dist(loc[f'{s}_shoulder'], loc[f'{s}_elbow']) + _dist(loc[f'{s}_elbow'], loc[f'{s}_hand'])
            for s in ('left', 'right')]
    asym = max(abs(legs[0] - legs[1]) / max(legs), abs(arms[0] - arms[1]) / max(max(arms), MIN_BONE))
    leg_ratio = sum(legs) / 2 / height
    unit = lambda x, lo, hi: max(0.0, min(1.0, (x - lo) / (hi - lo)))  # noqa: E731
    score = min(unit(0.50 - asym, 0.0, 0.45), unit(leg_ratio, 0.12, 0.30))
    return AMP_FLOOR + (1.0 - AMP_FLOOR) * score


def _deltas(motion: str, i: int, n: int, amp: float) -> Dict[str, float]:
    """第 i 帧每根骨相对原画的角度增量。i=0 必须全为 0，否则首帧就变形。"""
    r = RECIPE[motion]
    # jump 是一次性动作：半周期，首末帧都落地收回。run 是循环：整周期，末帧接回首帧。
    phase = math.sin(math.pi * i / max(n - 1, 1)) if motion == 'jump' else math.sin(2 * math.pi * i / n)
    limb, arm = r['limb'] * amp * phase, r['arm'] * amp * phase
    if motion == 'jump':
        # 四肢同相轻微摆动，腾空全靠根位移表现
        d = {j: limb for j in ('left_knee', 'right_knee')}
        d.update({j: -arm for j in ('left_elbow', 'right_elbow')})
    else:
        # 走路：左右腿反相，手臂与同侧腿反相
        d = {'left_knee': limb, 'right_knee': -limb, 'left_elbow': -arm, 'right_elbow': arm}
    # 小腿在大腿基础上叠加膝弯；小臂保持刚性跟随（用户只要「手摆动」，不需要肘部动作）
    # ponytail: 两腿同相弯膝（真实步态应反相）。火柴人尺度看不出，天花板是步态偏「同时下蹲」；
    # 升级路径是给左右腿各自的相位，但两者在 i=0 都必须为 0，否则首帧就偏离原画。
    bend = r['bend'] * amp * (1 - math.cos(2 * math.pi * i / n))
    for tip, base in PARENT_ANGLE.items():
        d[tip] = d.get(base, 0.0) + (bend if tip.endswith('_foot') else 0.0)
    return d


def _motion_rows(motion: str, offsets, height: float, amp: float):
    """MOTION 段数据行。关节顺序＝TREE 顺序＝vendor 深度优先顺序。"""
    r = RECIPE[motion]
    n = r['frames']
    rows = []
    for i in range(n):
        d = _deltas(motion, i, n, amp)
        # 局部 Xrotation = 目标累积角 - 父累积角；屏幕 CCW 与绕 X 右手旋转同号
        xrot = {DRIVER[cj]: (d.get(cj, 0.0) - d.get(PARENT_ANGLE[cj], 0.0)) if cj in PARENT_ANGLE
                else d.get(cj, 0.0) for cj in DRIVER}
        # 腾空用物理抛物线（重力）而非 sin：sin 的顶部太圆滑，看起来「飘」
        t = i / max(n - 1, 1)
        y = height * (r['air'] * (1 - (2 * t - 1) ** 2)
                      + r['bob'] * (1 - math.cos(4 * math.pi * i / n)) / 2)
        vals = [0.0, y, 0.0, 0.0, 0.0, 0.0]           # Hips: 位移 + ZYX 旋转
        for name, _parent in TREE[1:]:
            vals += [0.0, 0.0, xrot.get(name, 0.0)]
        rows.append(' '.join(f'{v:.5f}' for v in vals))
    return n, rows


def synth_motion(char_cfg, motion: str, out_dir, amp_scale: Optional[float] = None) -> Path:
    """合成 <motion>.bvh + <motion>.yaml，返回 motion yaml 路径（供 render_animation 使用）。"""
    if motion not in RECIPE:
        raise ValueError(f'unknown motion: {motion}')
    if not RETARGET_CFG.exists():                  # 部署漏文件 -> 契约里的 failed:ASSET_MISSING
        raise FileNotFoundError(f'retarget config missing: {RETARGET_CFG}')
    char_cfg, out_dir = Path(char_cfg), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    loc = _load(char_cfg)
    offsets = _offsets(loc)
    height = max(_dist(loc['neck'], loc['left_foot']), _dist(loc['neck'], loc['right_foot']), MIN_BONE)
    amp = amplitude_scale(char_cfg) if amp_scale is None else amp_scale

    n, rows = _motion_rows(motion, offsets, height, amp)
    bvh = out_dir / f'{motion}.bvh'
    bvh.write_text('{}\nMOTION\nFrames: {}\nFrame Time: {:.7f}\n{}\n'.format(
        _hierarchy(offsets), n, FRAME_TIME, '\n'.join(rows)))

    cfg = out_dir / f'{motion}.yaml'
    cfg.write_text(yaml.safe_dump({
        'filepath': str(bvh.resolve()),        # chdir(VENDOR) 后相对路径会失效，必须绝对
        'start_frame_idx': 0, 'end_frame_idx': n,
        'groundplane_joint': 'LeftFoot',
        # Left->Right 顺序：_offsets 把 Right 固定在 +z，故 forward 恒为 +X，不会撞零四元数奇点
        'forward_perp_joint_vectors': [['LeftShoulder', 'RightShoulder'], ['LeftUpLeg', 'RightUpLeg']],
        'scale': 0.01, 'up': '+y', 'frame_time': FRAME_TIME}))
    return cfg
