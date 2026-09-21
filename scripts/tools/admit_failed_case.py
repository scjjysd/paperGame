#!/usr/bin/env python3
"""把一次失败的关卡识别样本收进真实回归集。

约定：用户拿任何一张失败图来提问，这张图就必须进 `testdata/levels/real/`，
并在之后的每次改动里参与 `tests/test_real_corpus.py` 的全目录快照回归。

用法
----
    # 从线上 job 拉（需要 NAS 可达；失败时自动回退到客户端缓存）
    python scripts/tools/admit_failed_case.py --job level_94a770f10ad8 --name goal-near-right-edge

    # 从本地文件入库
    python scripts/tools/admit_failed_case.py --file /tmp/pgjob4/input.png --name goal-near-right-edge

    # 只登记不写 README（例如同名样本已存在时）
    python scripts/tools/admit_failed_case.py --file x.png --name y --no-readme

参数
----
--job     线上 jobId，从 https://scjjysd.xyz:8443/artifacts/<job>/input.png 拉原图
--file    本地原图路径
--name    入库文件名（不含扩展名），建议描述画面特征，如 faint-pen-closed-rectangles
--note    写进 README 的一句话说明（画面特征 + 当初的失败现象 + jobId）
--dry-run 只打印将要做的事，不落盘

脚本会：拷贝原图 → 跑一次端到端 parse → 打印结果摘要 → 追加 README 条目。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2]
REAL = SERVER_ROOT / 'testdata' / 'levels' / 'real'
NAS = 'https://scjjysd.xyz:8443'
CACHE_GLOB = ('~/Library/Application Support/DefaultCompany/PaperGame/'
              'Levels/v1/*')


def fetch_from_job(job: str, dest_dir: Path) -> Path | None:
    """优先拉线上产物，失败则回退到客户端本地缓存的 background/source。"""
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    target = dest_dir / 'input.bin'
    try:
        url = f'{NAS}/artifacts/{job}/input.png'
        with urllib.request.urlopen(url, timeout=20, context=ctx) as resp:
            target.write_bytes(resp.read())
        print(f'  线上拉取成功：{url}')
        return target
    except Exception as exc:  # noqa: BLE001
        print(f'  线上拉取失败（{exc}），回退客户端缓存')
    cache_root = Path.home() / 'Library/Application Support/DefaultCompany/PaperGame/Levels/v1'
    if cache_root.exists():
        cands = sorted(cache_root.glob('*/source'), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        if cands:
            print(f'  使用缓存：{cands[0]}')
            return cands[0]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--job')
    ap.add_argument('--file')
    ap.add_argument('--name', required=True)
    ap.add_argument('--note', default='')
    ap.add_argument('--no-readme', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not args.job and not args.file:
        ap.error('需要 --job 或 --file')

    src: Path | None = None
    tmp = Path('/tmp/admit_case')
    tmp.mkdir(exist_ok=True, parents=True)
    if args.file:
        src = Path(args.file)
        if not src.exists():
            print(f'文件不存在：{src}')
            return 1
    else:
        src = fetch_from_job(args.job, tmp)

    if src is None:
        print('拿不到原图')
        return 1

    suffix = src.suffix.lower()
    if suffix not in ('.jpg', '.jpeg', '.png'):
        # 客户端缓存的 source 没有扩展名，用内容探测
        import cv2
        img = cv2.imread(str(src))
        if img is None:
            print(f'不是图片：{src}')
            return 1
        suffix = '.jpg'
    dest = REAL / f'{args.name}{suffix}'

    print(f'入库：{src} -> {dest}')
    if dest.exists():
        print(f'  目标已存在，将被覆盖：{dest}')
    if args.dry_run:
        return 0

    shutil.copy(src, dest)

    # 端到端跑一次，给出结果摘要
    sys.path.insert(0, str(SERVER_ROOT))
    from app.services import level_parser as lp  # noqa: E402
    work = Path('/tmp/admit_case') / args.name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    shutil.copy(dest, work / 'input.png')
    try:
        lp.parse(work)
    except Exception as exc:  # noqa: BLE001
        print(f'  parse 异常：{type(exc).__name__}: {exc}')

    summary = '（无产物）'
    lvl_p = work / 'level.json'
    ana_p = work / 'analysis.json'
    status = '?'
    if ana_p.exists():
        status = json.loads(ana_p.read_text()).get('reviewReason') or 'ready'
    if lvl_p.exists():
        lvl = json.loads(lvl_p.read_text())
        g = lvl.get('goalRegion') or {}
        s = lvl.get('playerStart') or {}
        summary = (f"{status} | P{len(lvl.get('platforms') or [])} "
                   f"W{len(lvl.get('walls') or [])} "
                   f"B{len(lvl.get('blocks') or [])} | "
                   f"终点 {g.get('width')}x{g.get('height')}@{g.get('x')},{g.get('y')} | "
                   f"起点 {s.get('x')},{s.get('y')}")
    print(f'  端到端结果：{summary}')

    if not args.no_readme:
        note = args.note or (f'job {args.job}' if args.job else '用户提供的失败样本')
        entry = (f"\n## {args.name}{suffix}\n\n{note}\n\n入库时结果：`{summary}`\n")
        with (REAL / 'README.md').open('a', encoding='utf-8') as fh:
            fh.write(entry)
        print('  README 已追加条目')

    print('\n下一步：')
    print(f'  1. 用 PG_UPDATE_BASELINE=1 重跑 pytest tests/test_real_corpus.py 写入快照')
    print(f'  2. git add {dest.relative_to(SERVER_ROOT)} testdata/levels/real/README.md '
          'tests/snapshots/real_corpus.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
