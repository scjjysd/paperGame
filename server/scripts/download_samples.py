"""下载 AnimatedDrawings 官方示例涂鸦到 testdata/characters/，作为验收样本的起点。
真实验收需补充儿童实画样本至 20 张（授权后放入同目录，命名 s01.png-s20.png）。"""
import urllib.request
from pathlib import Path

BASE = 'https://raw.githubusercontent.com/facebookresearch/AnimatedDrawings/main/examples/drawings/'
NAMES = ['garlic.png']

out = Path(__file__).resolve().parents[2] / 'testdata' / 'characters'
out.mkdir(parents=True, exist_ok=True)
for n in NAMES:
    urllib.request.urlretrieve(BASE + n, out / n)
    print('downloaded', out / n)
