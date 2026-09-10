"""标注质量诊断与验收：直接跑生产的 repair_or_reject + motion_2d，不重复实现算法。

七项指标（前三项是老的，后四项是二维动画方案落地后补的）：
  1. 全图笔画丢失率      —— 检测框与 mask 一共漏掉多少墨迹
  2. 吸附前关节偏移      —— 门禁 MAX_JOINT_OFFSET 的度量口径
  3. ARAP 丢 pin 数      —— 需 --render；丢掉的 pin 对形变零贡献
  4. 采用的框            —— ink(按墨迹重裁) / pad(按比例扩框) / plain(原框) / baseline
  5. 手脚末端缺口        —— extend_limb_tips 的效果，控制点离墨迹末端还差多少
  6. 首帧需掰动角度      —— **整套方案的立足点，必须 ≈0°**
  7. 首帧形状保真        —— 需 --render；渲染首帧宽高比 / 原画，1.00x = 与原画一致

用法：
    python scripts/diagnose_annotations.py                          # 只算指标
    python scripts/diagnose_annotations.py --anno-root out/review2d  # 指定产物目录
    python scripts/diagnose_annotations.py s07 --render              # 指定样本并实渲
    python scripts/diagnose_annotations.py --render --log out/diag.log

改 MAX_JOINT_OFFSET / PAD_RATIO / TIP_MIN_GAIN 或换样本集（尤其真实儿童画复测）后
必须用它重新标定。需要 --render 时依赖 OpenGL（宿主 GLFW）；指标模式不需要。
"""
import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

SERVER_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVER_DIR))

from app.services.annotation_repair import (  # noqa: E402
    MAX_JOINT_OFFSET, PAD_RATIO, repair_or_reject)
from app.services.annotations import NeedsCorrection, analyze  # noqa: E402
from app.services.motion_2d import RETARGET_CFG, char_bone_angles, synth_motion  # noqa: E402

DEFAULT_INPUT = SERVER_DIR / 'testdata' / 'characters'
logging.disable(logging.INFO)      # vendor 的 Retargeter 会刷大量 info，诊断输出要能读

def _tip_gaps(anno: Path):
    """脚 / 手控制点离墨迹末端还差多少（占图高 / 图宽的百分比）。

    extend_limb_tips 的效果度量：末端那截没有 pin，ARAP 会随邻近网格把它任意拖拽，
    表现就是「手脚被拉长扭曲」。修之前 20 份样本脚中位 8.2%、手中位 14.1%。

    局限：分子是「墨迹最外侧到手/脚关节的距离」，画里有非肢体笔画时会虚高——
    s07（猪）的 43.6% 大半来自尾巴，s14 的 56.5% 来自框外补回的其他笔画。
    看这一列要连着看画，不能只看数字。
    """
    cfg = yaml.safe_load((anno / 'char_cfg.yaml').read_text())
    loc = {j['name']: j['loc'] for j in cfg['skeleton']}
    ys, xs = np.nonzero(np.array(Image.open(anno / 'mask.png').convert('L')) > 127)
    if not len(ys):
        return 0.0, 0.0
    foot = (ys.max() - max(loc['left_foot'][1], loc['right_foot'][1])) / cfg['height']
    hx = (loc['left_hand'][0], loc['right_hand'][0])
    hand = max(xs.max() - max(hx), min(hx) - xs.min()) / cfg['width']
    return max(foot, 0.0) * 100, max(hand, 0.0) * 100


def _frame0_bend(anno: Path, motion_cfg: Path) -> float:
    """第 0 帧 ARAP 需要强行掰动的平均角度（度）——整套二维动画方案的立足点，应 ≈0。

    旧的三维动捕资产在这 20 份样本上平均要掰 64°、最狠 s11 的 left_hand 179°（等于对折），
    画因此被横向压扁到 50%、手臂压进躯干。换任何固定动捕资产最好也只能降到 34°。
    """
    from animated_drawings.config import MotionConfig, RetargetConfig
    from animated_drawings.model.retargeter import Retargeter
    want = char_bone_angles(anno / 'char_cfg.yaml')
    rc = RetargetConfig(str(RETARGET_CFG))
    rt = Retargeter(MotionConfig(str(motion_cfg)), rc)
    for cj, (prox, dist) in rc.char_joint_bvh_joints_mapping.items():
        rt.compute_orientations(prox, dist, cj)
    got = {cj: float(v[0]) for cj, v in rt.char_joint_to_orientation.items()}
    return float(np.mean([abs((got[j] - want[j] + 180) % 360 - 180) for j in want]))


def _aspect(alpha: np.ndarray) -> float:
    mask = alpha > 8
    if not mask.any():
        return float('nan')
    ys, xs = np.nonzero(mask)
    return (xs.max() - xs.min() + 1) / (ys.max() - ys.min() + 1)


def _shape_fidelity(anno: Path, gif: Path) -> float:
    """渲染首帧的内容宽高比 / 原画 mask 的宽高比。1.00x = 形状与原画一致。

    首帧掰动角为 0 却仍偏离 1.00x，说明形变来自 ARAP 之外的环节——本轮就是靠它抓出
    s07 的 0.67x（neck 骨骼语义错配，头被硬转 25°，而「头」占了那张画一半面积）。
    """
    src = _aspect(np.array(Image.open(anno / 'mask.png').convert('L')))
    return _aspect(np.asarray(Image.open(gif).convert('RGBA'))[..., 3]) / src


# 渲染必须在子进程里跑：vendor 渲染时 chdir 到 vendor 目录，且 ARAP 的告警走
# 导入时绑定的 stderr，进程内 redirect/disable 都拦不住（实测计数会偏低一半）
_RENDER_SNIPPET = '''
import sys
from pathlib import Path
sys.path.insert(0, {server!r})
from app.services.render_scene import render_animation
render_animation(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(),
                 Path(sys.argv[3]).resolve(), use_mesa=False)
'''


def render_pin_drops(anno: Path, motion_cfg: Path, out_gif: Path, timeout: int = 90):
    """返回 (退出码, ARAP 丢 pin 次数, 帧数, 空帧数)。丢 pin 的关节对形变零贡献。

    必须带超时：网格断开时 vendor 会卡在 arap.py 的 `while det == 0` 扰动循环里
    （实测 >300s、983% CPU），不带超时会连带卡死整个诊断进程。
    """
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as fh:
        fh.write(_RENDER_SNIPPET.format(server=str(SERVER_DIR)))
        worker = fh.name
    try:
        proc = subprocess.run(
            [sys.executable, worker, str(anno), str(motion_cfg), str(out_gif)],
            capture_output=True, text=True, cwd=str(SERVER_DIR), timeout=timeout)
    except subprocess.TimeoutExpired:
        return -2, -1, 0, 0
    finally:
        Path(worker).unlink(missing_ok=True)
    if proc.returncode != 0 or not out_gif.exists():
        return proc.returncode, -1, 0, 0
    pins = (proc.stdout + proc.stderr).count('not inside or on edge')
    gif = Image.open(out_gif)
    frames = empty = 0
    try:
        while True:
            empty += int((np.asarray(gif.convert('RGBA'))[..., 3] > 128).sum() < 50)
            frames += 1
            gif.seek(gif.tell() + 1)
    except EOFError:
        pass
    return 0, pins, frames, empty


def preflight() -> str:
    """返回空串表示 TorchServe 就绪，否则返回该打印给用户的排障提示。"""
    import urllib.request
    try:
        urllib.request.urlopen('http://localhost:8080/ping', timeout=3).read()
        return ''
    except Exception as e:
        return (f'TorchServe (localhost:8080) 不可用：{e}\n'
                f'  起服务：cd {SERVER_DIR} && docker compose up -d torchserve\n'
                f'  验证  ：curl http://localhost:8080/ping   # 期望 {{"status": "Healthy"}}')


def report(img: Path, do_render: bool) -> dict:
    """从原图跑完整标注路径并输出指标。

    必须从原图起，不能拿已有的 anno 目录：产物里的 char_cfg 坐标已相对修复后的新框，
    再跑一次 repair_or_reject 会把 plain_box 当参照系，算出巨大偏移被门禁误判
    （实测 s07 会被拦成 SKELETON_MISFIT）。
    """
    name = img.stem
    with tempfile.TemporaryDirectory() as tmp:
        anno = Path(tmp) / 'anno'
        try:
            analyze(img, anno)
            info = dict(repair_or_reject(anno), name=name, gated=False)
        except NeedsCorrection as e:
            print(f'{name:<6} needs_correction({e.reason})')
            return {'name': name, 'gated': True, 'reason': e.reason}

        foot_gap, hand_gap = _tip_gaps(anno)
        # 动作按修好的骨架现场合成——诊断必须跑生产路径，不能拿旧的静态 bvh 充数
        cfgs = {m: synth_motion(anno / 'char_cfg.yaml', m, Path(tmp) / m) for m in ('run', 'jump')}
        bend = max(_frame0_bend(anno, c) for c in cfgs.values())
        info.update(foot_gap=foot_gap, hand_gap=hand_gap, bend=bend)

        line = (f'{name:<6}{info["crop"]:>7}{info["loss"]:>9.1f}%{info["offset"] * 100:>11.1f}%'
                f'{info["moved"]:>6}{info["extended"]:>6}{foot_gap:>7.1f}%{hand_gap:>7.1f}%'
                f'{bend:>9.2f}°')
        if do_render:
            for motion, cfg in cfgs.items():
                gif = Path(tmp) / f'{name}_{motion}.gif'
                rc, pins, frames, empty = render_pin_drops(anno, cfg, gif)
                if rc == -2:
                    line += f'  {motion}:⛔死锁(>90s)'
                    info[f'{motion}_bad'] = True
                    continue
                shape = _shape_fidelity(anno, gif) if rc == 0 and gif.exists() else float('nan')
                # 形状窗口 0.90~1.15x 的依据：二维合成方案下 20 份样本实测 19 份落在
                # 0.92~1.15x，唯一在窗外的 s19（0.71x）经目视确认确实变形（那张画本身
                # 只是几根抽象线条）。frames < 6 是「配方最少 7 帧、少于 6 说明帧被合并或渲染断了」。
                bad = '★退化' if rc or empty or frames < 6 or not 0.9 <= shape <= 1.15 else ''
                line += f'  {motion}:丢pin={pins} 帧={frames} 空={empty} 形状={shape:.2f}x{bad}'
                info[f'{motion}_pins'] = pins
                info[f'{motion}_shape'] = shape
                info[f'{motion}_bad'] = bool(bad) or rc != 0
        print(line)
        return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('input', nargs='?', default=str(DEFAULT_INPUT),
                   help=f'涂鸦图片或目录（默认 {DEFAULT_INPUT}）')
    ap.add_argument('--limit', type=int, help='只处理前 N 张')
    ap.add_argument('--render', action='store_true',
                    help='真实渲染，额外统计 ARAP 丢 pin 与首帧形状保真（慢）')
    ap.add_argument('--log', help='同时把输出写到该文件')
    args = ap.parse_args()

    src = Path(args.input)
    images = [src] if src.is_file() else sorted(
        q for q in src.iterdir() if q.suffix.lower() in ('.png', '.jpg', '.jpeg'))
    if not images:
        print(f'{src} 下没有图片')
        return 1
    if args.limit:
        images = images[:args.limit]
    if (err := preflight()):
        print(err)
        return 1
    header = (f'门禁 MAX_JOINT_OFFSET={MAX_JOINT_OFFSET:.0%}（吸附前度量）  扩框 PAD_RATIO={PAD_RATIO}'
              f'  输入 {src}\n'
              f'{"样本":<6}{"框":>7}{"全图丢失":>10}{"吸附前偏移":>12}{"吸附":>6}{"外推":>6}'
              f'{"脚缺口":>8}{"手缺口":>8}{"首帧掰动":>10}')
    lines = []

    class Tee:
        def write(self, text):
            lines.append(text)
            sys.__stdout__.write(text)

        def flush(self):
            sys.__stdout__.flush()

    real, sys.stdout = sys.stdout, Tee()
    try:
        print(header)
        results = [r for r in (report(q, args.render) for q in images) if r]
        ok = [r for r in results if not r['gated']]
        gated = [r['name'] for r in results if r['gated']]
        bad = [r['name'] for r in results if r.get('run_bad') or r.get('jump_bad')]
        print(f'\n通过门禁 {len(ok)}/{len(results)}（验收线 ≥16/20）   拦下: {gated or "无"}')
        if ok:
            worst_bend = max(ok, key=lambda r: r['bend'])
            print(f'全图丢失中位 {np.median([r["loss"] for r in ok]):.1f}%'
                  f'   首帧掰动最大 {worst_bend["bend"]:.2f}°（{worst_bend["name"]}，应 <1°）'
                  f'   框来源 {dict((c, sum(1 for r in ok if r["crop"] == c)) for c in ("ink", "pad", "plain", "baseline"))}')
        if args.render:
            pins = [r.get('run_pins', 0) + r.get('jump_pins', 0) for r in ok]
            shapes = [r[k] for r in ok for k in ('run_shape', 'jump_shape') if not np.isnan(r.get(k, np.nan))]
            print(f'ARAP 丢 pin 合计 {sum(pins)}'
                  f'   首帧形状 {min(shapes):.2f}x~{max(shapes):.2f}x（1.00x = 与原画一致）'
                  f'   渲染退化: {bad or "无"}' if shapes else f'ARAP 丢 pin 合计 {sum(pins)}')
    finally:
        sys.stdout = real
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        Path(args.log).write_text(''.join(lines))
        print(f'已写入 {args.log}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
