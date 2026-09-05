"""批量审查工具：把一个目录下的涂鸦图跑成 run/jump 动画，产出单页 HTML 供人工审查。

跑的是生产管线本体（CharacterPipeline.render_character），所以看到的就是服务端会给
Unity 的东西；本脚本只做编排、截图与排版，不含任何算法。

前置条件（缺任一都跑不了，脚本启动会自检 TorchServe）：
    docker compose up -d torchserve     # 标注模型，必须在线
    宿主 OpenGL（GLFW）                  # 渲染用，宿主直接跑即可；容器内需 Mesa

用法（在 server/ 下，先 source .venv/bin/activate）：
    python scripts/review_batch.py                         # 默认 ../testdata/characters 全部
    python scripts/review_batch.py /path/to/doodles         # 指定目录（*.png/*.jpg/*.jpeg）
    python scripts/review_batch.py /path/to/one.png         # 也接受单个文件
    python scripts/review_batch.py --limit 3                # 先试 3 张，别等全量
    open out/review/index.html                              # 看结果

每次运行全量重渲染（先清掉旧产物），所以 index.html 永远对应当前代码。
产物全部落在 out/review/ 下且用相对路径互引，整个目录可打包拷走。
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from PIL import Image, ImageDraw

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

from app.services.annotations import NeedsCorrection  # noqa: E402
from app.services.character_pipeline import CharacterPipeline  # noqa: E402

SUFFIXES = ('.png', '.jpg', '.jpeg')
# 生产 render_runner 用 120s（同样是 run+jump 全量）；这里放宽到 180s 容忍宿主负载，
# 但必须有上限：mask 网格断开时 vendor arap.py 会卡在 det 扰动循环（实测 >300s、983% CPU）
TIMEOUT_SEC = 180
STATUS_ORDER = {'failed': 0, 'needs_correction': 1, 'ready': 2}
STATUS_COLOR = {'ready': '#1a7f37', 'needs_correction': '#9a6700', 'failed': '#cf222e'}


def preflight() -> str:
    """返回空串表示依赖就绪，否则返回该打印给用户的排障提示。"""
    try:
        urllib.request.urlopen('http://localhost:8080/ping', timeout=3).read()
        return ''
    except Exception as e:
        return (f'TorchServe (localhost:8080) 不可用：{e}\n'
                f'  起服务：cd {SERVER_DIR} && docker compose up -d torchserve\n'
                f'  验证  ：curl http://localhost:8080/ping   # 期望 {{"status": "Healthy"}}')


def _worker(img: Path, out_root: Path) -> int:
    """子进程入口：跑生产管线，把业务终态写进 <name>/review.json。

    必须隔离在子进程里（与 diagnose_annotations.py 同理）：render_animation 会
    os.chdir(vendor)，且 GLFW 偶发整体初始化失败——一张挂了不能拖垮整批。
    退出码沿用项目协议：0 = 业务终态已落盘；非 0 = 基础设施故障，由父进程记为 failed。
    """
    t0 = time.time()
    char_dir = out_root / img.stem
    char_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = CharacterPipeline(out_root).render_character(img)
        info = {'status': 'ready', 'animations': result['animations']}
    except NeedsCorrection as e:
        info = {'status': 'needs_correction', 'reason': e.reason, 'detail': e.detail}
    info['elapsed'] = round(time.time() - t0, 1)
    (char_dir / 'review.json').write_text(json.dumps(info, ensure_ascii=False))
    return 0


def _mask_overlay(anno_dir: Path, out_path: Path) -> Optional[Path]:
    """texture 上叠红色半透明 mask + 青色关节点，一眼看出哪些笔画会被丢弃、关节落在哪。

    mask 内染红，故留在原色（黑）的笔画就是 _load_txtr 会强制透明、成片里彻底消失的部分。
    早期失败（NO_HUMANOID 等）时 anno 目录是空的，返回 None 由页面显示占位块。
    """
    texture_path, mask_path = anno_dir / 'texture.png', anno_dir / 'mask.png'
    if not (texture_path.exists() and mask_path.exists()):
        return None
    texture = Image.open(texture_path).convert('RGB')
    mask = Image.open(mask_path).convert('L').resize(texture.size, Image.NEAREST)
    red = Image.new('RGB', texture.size, (255, 40, 40))
    out = Image.composite(Image.blend(texture, red, 0.45), texture, mask)

    cfg_path = anno_dir / 'char_cfg.yaml'
    if cfg_path.exists():
        draw = ImageDraw.Draw(out)
        radius = max(2, min(out.size) // 90)
        for joint in (yaml.safe_load(cfg_path.read_text()) or {}).get('skeleton', []):
            x, y = joint['loc']
            draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                         fill=(0, 210, 255), outline=(0, 0, 0))
    out.save(out_path)
    return out_path


def run_one(img: Path, out_root: Path, timeout: int = TIMEOUT_SEC) -> Dict:
    """全量重渲染一张图，返回页面需要的结果视图（路径均相对 out_root）。"""
    name = img.stem
    char_dir = out_root / name
    shutil.rmtree(char_dir, ignore_errors=True)
    char_dir.mkdir(parents=True)
    source = char_dir / ('source' + img.suffix.lower())
    shutil.copy(img, source)

    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--worker',
             str(img.resolve()), str(out_root.resolve())],
            capture_output=True, text=True, cwd=str(SERVER_DIR), timeout=timeout)
    except subprocess.TimeoutExpired:
        info = {'status': 'failed', 'elapsed': float(timeout),
                'detail': f'超时 >{timeout}s：mask 网格可能断开，vendor ARAP 卡在 det 扰动循环'}
    else:
        review = char_dir / 'review.json'
        if proc.returncode == 0 and review.exists():
            info = json.loads(review.read_text())
        else:
            tail = (proc.stderr or proc.stdout).strip().splitlines()
            info = {'status': 'failed', 'elapsed': round(time.time() - t0, 1),
                    'detail': tail[-1][:200] if tail else f'子进程退出码 {proc.returncode}'}

    anno = char_dir / 'anno'
    overlay = _mask_overlay(anno, char_dir / 'mask_overlay.png')
    cfg_path = anno / 'char_cfg.yaml'
    joints = (yaml.safe_load(cfg_path.read_text()) or {}).get('skeleton', []) if cfg_path.exists() else []
    animations = {
        motion: {'gif': f'{name}/{motion}/{motion}.gif', 'sheet': f'{name}/{motion}/{motion}.png',
                 **{k: meta[k] for k in ('frameCount', 'frameWidth', 'frameHeight', 'fps')}}
        for motion, meta in (info.get('animations') or {}).items()
    }
    return {
        'name': name,
        'status': info['status'],
        'reason': info.get('reason'),
        'detail': info.get('detail'),
        'elapsed': info.get('elapsed', 0.0),
        'source': f'{name}/{source.name}',
        'mask_overlay': f'{name}/{overlay.name}' if overlay else None,
        'texture': f'{name}/anno/texture.png' if (anno / 'texture.png').exists() else None,
        'joints': len(joints),
        'animations': animations,
    }


CSS = """
:root { color-scheme: light; }
body { margin:0; background:#f6f8fa; color:#1f2328;
       font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
header { position:sticky; top:0; z-index:9; background:#fff; border-bottom:1px solid #d1d9e0;
         padding:12px 20px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
header h1 { margin:0 0 4px; font-size:16px; }
header .sum { color:#59636e; font-variant-numeric:tabular-nums; }
main { padding:20px; display:flex; flex-direction:column; gap:16px; }
.card { background:#fff; border:1px solid #d1d9e0; border-radius:8px; overflow:hidden; }
.card > h2 { margin:0; padding:10px 14px; font-size:15px; background:#fbfcfd;
             border-bottom:1px solid #e6eaef; display:flex; align-items:center; gap:10px; }
.badge { color:#fff; border-radius:99px; padding:2px 10px; font-size:12px; font-weight:600; }
.ms { color:#59636e; font-size:12px; font-variant-numeric:tabular-nums; }
.detail { margin-left:auto; color:#59636e; font-size:12px; font-family:ui-monospace,monospace;
          text-align:right; max-width:52%; }
.group { padding:12px 14px; border-top:1px dashed #e6eaef; }
.group h4 { margin:0 0 8px; font-size:12px; color:#59636e; letter-spacing:.04em; }
.row { display:flex; flex-wrap:wrap; gap:14px; align-items:flex-end; }
figure { margin:0; text-align:center; }
figcaption { margin-top:4px; font-size:12px; color:#59636e; }
.thumb { height:200px; max-width:260px; object-fit:contain; display:block;
         border:1px solid #d1d9e0; border-radius:4px; }
.sheet { width:100%; display:block; border:1px solid #d1d9e0; border-radius:4px;
         image-rendering:pixelated; }
.missing { height:200px; width:150px; display:flex; align-items:center; justify-content:center;
           border:1px dashed #d1d9e0; border-radius:4px; background:#fbfcfd;
           color:#8c959f; font-size:12px; }
.alpha { background-color:#fff; background-size:16px 16px; background-position:0 0,8px 8px;
         background-image:
           linear-gradient(45deg,#e4e8ec 25%,transparent 25%,transparent 75%,#e4e8ec 75%),
           linear-gradient(45deg,#e4e8ec 25%,transparent 25%,transparent 75%,#e4e8ec 75%); }
.cap { font-size:12px; color:#59636e; margin:8px 0 4px; font-variant-numeric:tabular-nums; }
"""


def _figure(path: Optional[str], label: str) -> str:
    """缺图时输出占位块，绝不产生空 src（否则浏览器会去请求当前页面）。"""
    body = (f'<img class="thumb alpha" src="{escape(path, quote=True)}" alt="{escape(label)}">'
            if path else f'<div class="missing">无 {escape(label)}</div>')
    return f'<figure>{body}<figcaption>{escape(label)}</figcaption></figure>'


def _card(r: Dict) -> str:
    animations = r.get('animations') or {}
    label = r['status'] + (f' · {r["reason"]}' if r.get('reason') else '')
    detail = f'<span class="detail">{escape(str(r["detail"]))}</span>' if r.get('detail') else ''
    gifs = ''.join(_figure(animations.get(m, {}).get('gif'), f'{m}.gif') for m in ('run', 'jump'))
    sheets = ''.join(
        f'<div class="cap">{escape(m)}.png · {a["frameCount"]} 帧 · '
        f'{a["frameWidth"]}×{a["frameHeight"]} · {a["fps"]}fps</div>'
        f'<a href="{escape(a["sheet"], quote=True)}" target="_blank">'
        f'<img class="sheet alpha" src="{escape(a["sheet"], quote=True)}" '
        f'alt="{escape(m)} sprite sheet"></a>'
        for m, a in animations.items())
    return (
        f'<section class="card" id="card-{escape(r["name"], quote=True)}">'
        f'<h2>{escape(r["name"])}'
        f'<span class="badge" style="background:{STATUS_COLOR.get(r["status"], "#59636e")}">'
        f'{escape(label)}</span>'
        f'<span class="ms">{r.get("elapsed") or 0:.1f}s</span>{detail}</h2>'
        f'<div class="group"><h4>输入与动画</h4>'
        f'<div class="row">{_figure(r.get("source"), "原图")}{gifs}</div></div>'
        f'<div class="group"><h4>标注 · {r.get("joints", 0)} 关节'
        f'（红色 = mask 覆盖，留在原色的笔画会被丢弃；青点 = 吸附后关节）</h4>'
        f'<div class="row">{_figure(r.get("mask_overlay"), "mask 叠加 + 关节")}'
        f'{_figure(r.get("texture"), "texture")}</div></div>'
        + (f'<div class="group"><h4>Unity 精灵表（点击看原尺寸）</h4>{sheets}</div>' if sheets else '')
        + '</section>')


def build_html(results: List[Dict]) -> str:
    """结果列表 → 单页 HTML（纯函数，不碰磁盘）。问题样本排在前面，便于优先审查。"""
    ordered = sorted(results, key=lambda r: (STATUS_ORDER.get(r['status'], 9), r['name']))
    counts = Counter(r['status'] for r in results)
    total = sum(r.get('elapsed') or 0 for r in results)
    summary = (f'共 {len(results)} 张 · '
               + ' · '.join(f'{s} {counts.get(s, 0)}'
                            for s in ('ready', 'needs_correction', 'failed'))
               + f' · 总耗时 {total:.0f}s')
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>角色动画批量审查</title><style>{CSS}</style></head><body>'
            f'<header><h1>角色动画批量审查</h1>'
            f'<div class="sum">{escape(summary)}</div></header>'
            f'<main>{"".join(_card(r) for r in ordered)}</main></body></html>')


def main() -> int:
    ap = argparse.ArgumentParser(description='批量渲染涂鸦图并生成 HTML 审查页')
    ap.add_argument('input', nargs='?', default=str(SERVER_DIR.parent / 'testdata' / 'characters'),
                    help='图片目录或单个图片（默认 ../testdata/characters）')
    ap.add_argument('--out', default=str(SERVER_DIR / 'out' / 'review'), help='产物目录')
    ap.add_argument('--limit', type=int, help='只处理前 N 张（先小批量试）')
    ap.add_argument('--timeout', type=int, default=TIMEOUT_SEC, help=f'单张超时秒（默认 {TIMEOUT_SEC}）')
    ap.add_argument('--worker', nargs=2, metavar=('IMG', 'OUT'), help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:                      # 子进程模式，父进程内部调用
        return _worker(Path(args.worker[0]), Path(args.worker[1]))

    src = Path(args.input)
    images = ([src] if src.is_file()
              else sorted(p for p in src.glob('*') if p.suffix.lower() in SUFFIXES))
    if not images:
        print(f'{src} 下没有图片（支持 {", ".join(SUFFIXES)}）')
        return 1
    if args.limit:
        images = images[:args.limit]

    hint = preflight()
    if hint:
        print(hint)
        return 1

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    print(f'{len(images)} 张 → {out_root}（全量重渲染，单张约 40s）')
    results = []
    for i, img in enumerate(images, 1):
        r = run_one(img, out_root, args.timeout)
        results.append(r)
        note = r.get('reason') or (r.get('detail') or '')[:60]
        print(f'  [{i}/{len(images)}] {r["name"]:<10} {r["status"]:<17} '
              f'{r["elapsed"]:>6.1f}s  {note}')

    index = out_root / 'index.html'
    index.write_text(build_html(results), encoding='utf-8')
    counts = Counter(r['status'] for r in results)
    print(f'\nready {counts.get("ready", 0)}/{len(results)}    审查页已生成：\n  open {index}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
