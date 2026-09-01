#!/usr/bin/env python3
"""BVH 抽稀工具：每 k 帧取 1 帧，Frame Time 乘以 k。

用于将高采样率动捕数据（如 CMU 120fps）抽稀为 AnimatedDrawings 精灵表
所需的小帧数资产。可复用于未来更换动作。

用法：
    python decimate_bvh.py <input.bvh> <output.bvh> --every 4 [--start 30] [--end 90]

- --every k   每 k 帧保留 1 帧（含首帧），输出帧数 = ceil((end-start+1)/k)
- --start/--end  可选，先按原始帧号截取区间（含两端），再抽稀
- --in-place  可选，冻结根节点水平位移（Xposition/Zposition 取首帧值），
              把位移动作转成原地循环（跑步机），保留 Y 方向起伏用于跳跃
- Frame Time 自动乘以 k，保持播放速度不变
"""
from __future__ import annotations

import argparse
import sys


def decimate(src: str, dst: str, every: int, start: int = 0, end: int | None = None,
             in_place: bool = False) -> None:
    if every < 1:
        raise ValueError(f'every must be >= 1, got {every}')
    with open(src, encoding='utf-8') as f:
        lines = f.read().splitlines()

    try:
        motion_idx = next(i for i, ln in enumerate(lines) if ln.strip() == 'MOTION')
    except StopIteration:
        raise ValueError(f'no MOTION section found in {src}')

    header = lines[:motion_idx + 1]
    frames_line = lines[motion_idx + 1]
    frametime_line = lines[motion_idx + 2]
    if not frames_line.strip().startswith('Frames:'):
        raise ValueError(f'unexpected line after MOTION: {frames_line!r}')
    if not frametime_line.strip().startswith('Frame Time:'):
        raise ValueError(f'unexpected Frame Time line: {frametime_line!r}')

    total = int(frames_line.split(':')[1].strip())
    frame_time = float(frametime_line.split(':')[1].strip())
    data = lines[motion_idx + 3:motion_idx + 3 + total]
    if len(data) != total:
        raise ValueError(f'declared {total} frames but found {len(data)} data lines in {src}')

    end = total - 1 if end is None else min(end, total - 1)
    if not 0 <= start <= end:
        raise ValueError(f'invalid range start={start} end={end} (total={total})')

    kept = data[start:end + 1:every]
    new_time = frame_time * every

    if in_place:
        # 根节点通道顺序为 Xposition Yposition Zposition ...，冻结 X/Z 为首帧值（原地循环）
        first = kept[0].split()
        fx, fz = first[0], first[2]
        kept = [
            ' '.join([fx] + ln.split()[1:2] + [fz] + ln.split()[3:])
            for ln in kept
        ]

    out = header + [
        f'Frames: {len(kept)}',
        f'Frame Time: {new_time:.7f}',
    ] + kept
    with open(dst, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    print(f'{src}: frames {start}..{end} (of {total}) every {every} -> {dst}: '
          f'{len(kept)} frames @ {new_time:.6f}s ({1 / new_time:.1f} fps)')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('input')
    p.add_argument('output')
    p.add_argument('--every', type=int, required=True, help='keep 1 frame every k')
    p.add_argument('--start', type=int, default=0)
    p.add_argument('--end', type=int, default=None)
    p.add_argument('--in-place', action='store_true',
                   help='freeze root X/Z position to first frame (treadmill loop)')
    a = p.parse_args()
    try:
        decimate(a.input, a.output, a.every, a.start, a.end, a.in_place)
    except (ValueError, OSError) as e:
        sys.exit(f'error: {e}')


if __name__ == '__main__':
    main()
