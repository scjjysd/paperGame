"""按身体部位分别选投影面的动图对比（评估用，不改生产代码）。

三组各自可选 pca / frontal / sagittal。所有变体都已开启 F1（mask 修复 + 关节中轴投影），
因此画面差异只来自投影面选择。

实测语义（vendor retargeter.py，注意与解剖学直觉相反）：
    frontal  → 法线 x_axis → 屏幕横轴 = -z（深度）
    sagittal → 法线 z_axis → 屏幕横轴 =  x（真实左右）
    pca      → 取 PC3 就近选 x_axis / z_axis，随动作数据而变

用法：
    python scripts/compare_projection.py                       # 默认 6 组配置 x s08/s09/s16 x run+jump
    python scripts/compare_projection.py s08 --motion run
    python scripts/compare_projection.py --configs 0 2 5       # 只跑指定编号的配置

产物：out/compare/{样本}_{动作}.gif（多联横向同步播放）
      out/compare/retarget_*.yaml（派生配置）
"""
import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml
from PIL import Image, ImageDraw
from scipy import ndimage
from skimage.morphology import skeletonize

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

from app.services.annotation_repair import _ink, rebuild_mask  # noqa: E402

SPIKE = SERVER_DIR / 'out' / 'spike'
OUT = SERVER_DIR / 'out' / 'compare'
BASE_RETARGET = SERVER_DIR / 'vendor/AnimatedDrawings/examples/config/retarget/fair1_ppf.yaml'
ASSETS = SERVER_DIR / 'app' / 'assets' / 'motions'
TILE, LABEL_H = 200, 20
BG = (205, 205, 205)
WORKER = OUT / '_render_worker.py'

WORKER_SRC = '''"""单次渲染子进程。用法: _render_worker.py <anno_dir> <motion> <out.gif> <retarget_cfg>"""
import sys
from pathlib import Path
sys.path.insert(0, {server!r})
import app.services.render_scene as rs
rs.RETARGET_CFG = str(Path(sys.argv[4]).resolve())
rs.render_animation(Path(sys.argv[1]).resolve(),
                    (Path({assets!r}) / (sys.argv[2] + '.yaml')).resolve(),
                    Path(sys.argv[3]).resolve(), use_mesa=False)
'''

# (短标签, Upper Limbs, Lower Limbs, Trunk)——短标签用于动图面板，完整含义见下方 LEGEND
CONFIGS = [
    ('0 现状pca',      'pca',      'pca',      'frontal'),
    ('1 全frontal',    'frontal',  'frontal',  'frontal'),
    ('2 腿sag',        'frontal',  'sagittal', 'frontal'),
    ('3 臂sag',        'sagittal', 'pca',      'frontal'),
    ('4 臂sag腿fro',   'sagittal', 'frontal',  'frontal'),
    ('5 全sag',        'sagittal', 'sagittal', 'frontal'),
    ('6 躯干sag',      'pca',      'pca',      'sagittal'),
]
LEGEND = '\n'.join(f'  {lab:<14} = Upper:{u} | Lower:{l} | Trunk:{t}' for lab, u, l, t in CONFIGS)


def make_retarget(upper: str, lower: str, trunk: str) -> Path:
    """从 vendor fair1_ppf 派生：按组覆盖 method。"""
    cfg = yaml.load(BASE_RETARGET.read_text(), Loader=yaml.FullLoader)
    want = {'Upper Limbs': upper, 'Lower Limbs': lower, 'Trunk': trunk}
    for group in cfg['bvh_projection_bodypart_groups']:
        group['method'] = want[group['name']]
    path = OUT / f'retarget_{upper}-{lower}-{trunk}.yaml'
    path.write_text(yaml.dump(cfg, allow_unicode=True))
    return path


def project_joints_to_axis(skeleton, mask: np.ndarray):
    """F1：把 mask 外的关节吸附到 mask 中轴。

    必须是中轴而非「最近的 mask 像素」——网格三角面只覆盖 mask 内部区域
    （内部顶点是固定 40×40 网格），落在边缘的关节仍会被 ARAP 丢弃 pin。
    """
    inside = mask > 0
    axis = skeletonize(inside)
    if not axis.any():
        return skeleton, 0
    nearest = ndimage.distance_transform_edt(~axis, return_indices=True)[1]
    out, moved = [], 0
    for joint in skeleton:
        x, y = joint['loc']
        if 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and inside[y, x]:
            out.append(joint)
            continue
        out.append({**joint, 'loc': [int(nearest[1][y, x]), int(nearest[0][y, x])]})
        moved += 1
    return out, moved


def build_anno(sample: str) -> Path:
    """F1 处理后的标注目录（同一份供所有投影配置复用）。"""
    src = SPIKE / sample / 'run' / 'anno'
    dst = OUT / 'anno' / sample
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    texture = cv2.imread(str(src / 'texture.png'), cv2.IMREAD_COLOR)
    mask = rebuild_mask(texture, np.array(Image.open(src / 'mask.png')))
    cfg = yaml.load((src / 'char_cfg.yaml').read_text(), Loader=yaml.FullLoader)
    cfg['skeleton'], moved = project_joints_to_axis(cfg['skeleton'], mask)
    shutil.copy(src / 'texture.png', dst / 'texture.png')
    Image.fromarray(np.ascontiguousarray(mask)).save(dst / 'mask.png')
    (dst / 'char_cfg.yaml').write_text(yaml.safe_dump(cfg))
    print(f'  {sample}: mask 修复 + 关节吸附 {moved} 个')
    return dst


def render(anno: Path, motion: str, gif: Path, retarget: Path) -> int:
    if gif.exists():
        gif.unlink()
    proc = subprocess.run([sys.executable, str(WORKER), str(anno), motion, str(gif), str(retarget)],
                          capture_output=True, text=True, cwd=str(SERVER_DIR))
    if proc.returncode != 0 or not gif.exists():
        print(f'    渲染失败: {proc.stderr.strip().splitlines()[-1][:90] if proc.stderr else "无输出"}')
        return -1
    return (proc.stdout + proc.stderr).count('not inside or on edge')


def gif_frames(path: Path):
    gif = Image.open(path)
    frames = []
    try:
        while True:
            frames.append(gif.convert('RGBA').copy())
            gif.seek(gif.tell() + 1)
    except EOFError:
        pass
    return frames


def panel(frames, label: str):
    """归一到全序列内容包围盒的并集，缩放到统一 tile，加标签条。"""
    boxes = []
    for f in frames:
        alpha = np.asarray(f)[..., 3] > 128
        if alpha.any():
            ys, xs = np.where(alpha)
            boxes.append((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    if not boxes:
        return None
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[2] for b in boxes)
    bottom = max(b[3] for b in boxes)
    scale = (TILE - 8) / (max(right - left, bottom - top) or 1)

    out = []
    for f in frames:
        crop = f.crop((left, top, right, bottom))
        size = (max(1, int(crop.size[0] * scale)), max(1, int(crop.size[1] * scale)))
        crop = crop.resize(size, Image.LANCZOS)
        tile = Image.new('RGBA', (TILE, TILE + LABEL_H), BG + (255,))
        tile.alpha_composite(crop, ((TILE - size[0]) // 2, LABEL_H + (TILE - size[1]) // 2))
        draw = ImageDraw.Draw(tile)
        draw.rectangle([0, 0, TILE, LABEL_H], fill=(25, 25, 25))
        draw.text((5, 4), label, fill=(255, 235, 0))
        out.append(tile.convert('RGB'))
    return out


def compose(panels, path: Path):
    count = min(len(p) for p in panels)
    frames = []
    for i in range(count):
        canvas = Image.new('RGB', (TILE * len(panels), TILE + LABEL_H), BG)
        for k, p in enumerate(panels):
            canvas.paste(p[i], (k * TILE, 0))
        frames.append(canvas)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=83, loop=0)
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('samples', nargs='*', default=['s08', 's09', 's16'])
    ap.add_argument('--motion', nargs='*', default=['run', 'jump'])
    ap.add_argument('--configs', nargs='*', type=int, default=list(range(len(CONFIGS))))
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    WORKER.write_text(WORKER_SRC.format(server=str(SERVER_DIR), assets=str(ASSETS)))
    configs = [CONFIGS[i] for i in args.configs]
    print(f'投影配置 {len(configs)} 组，格式 = Upper|Lower|Trunk\n{LEGEND}\n')

    for sample in args.samples:
        if not (SPIKE / sample / 'run' / 'anno').exists():
            print(f'{sample}: 无尖刺产物，跳过')
            continue
        anno = build_anno(sample)
        for motion in args.motion:
            print(f'  {sample} {motion}:')
            rendered, hashes = [], {}
            for label, upper, lower, trunk in configs:
                rt = make_retarget(upper, lower, trunk)
                gif = OUT / f'_{label.split()[0]}_{sample}_{motion}.gif'
                skips = render(anno, motion, gif, rt)
                if skips < 0:
                    continue
                digest = hashlib.md5(gif.read_bytes()).hexdigest()[:10]
                dup = hashes.get(digest)
                hashes[digest] = dup or label
                note = f'  ≡ 与 {dup} 逐字节相同' if dup else ''
                print(f'    {label:<14} Upper:{upper:<9} Lower:{lower:<9} Trunk:{trunk:<9} 丢pin={skips}{note}')
                if not dup:
                    rendered.append((label, gif))
            if len(rendered) < 2:
                print(f'    → 去重后不足 2 组，跳过拼图')
                continue
            panels = [p for p in (panel(gif_frames(g), lab) for lab, g in rendered) if p]
            out = OUT / f'{sample}_{motion}.gif'
            n = compose(panels, out)
            print(f'    → {out.relative_to(SERVER_DIR)}  ({len(panels)}联 x {n}帧)\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
