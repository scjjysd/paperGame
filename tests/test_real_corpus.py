"""真实回归集全目录快照回归。

约定：任何一张用户拿来提问的失败图，都必须进 `testdata/levels/real/`，
并在这里自动参与回归——本测试遍历目录下每张图跑端到端 `parse()`，
把「状态 / 平台·墙·块数量 / 终点框 / 起点」与快照比对。

因此新增样本**不需要改本文件**；只有在有意改变行为时才需要刷新快照：

    PG_UPDATE_BASELINE=1 pytest tests/test_real_corpus.py -q --basetemp=.pytest-tmp

刷快照前请确认差异确实来自本次改动（`git diff tests/snapshots/real_corpus.json`）。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

REAL = Path(__file__).resolve().parents[1] / 'testdata/levels/real'
SNAPSHOT = Path(__file__).resolve().parent / 'snapshots' / 'real_corpus.json'

SAMPLES = sorted(p.name for p in REAL.iterdir()
                 if p.suffix.lower() in ('.jpg', '.jpeg', '.png'))


def _run(sample: str, work: Path) -> dict:
    """端到端跑一张图，返回可比较的摘要。"""
    from app.services.level_parser import parse

    work.mkdir(parents=True, exist_ok=True)
    shutil.copy(REAL / sample, work / 'input.png')
    try:
        parse(work)
    except Exception as exc:  # noqa: BLE001 - 失败本身要成为快照的一部分
        return {'error': f'{type(exc).__name__}: {exc}'}

    out: dict = {}
    ana_p = work / 'analysis.json'
    if ana_p.exists():
        out['status'] = json.loads(ana_p.read_text()).get('reviewReason') or 'ready'
    lvl_p = work / 'level.json'
    if lvl_p.exists():
        lvl = json.loads(lvl_p.read_text())
        out['platforms'] = len(lvl.get('platforms') or [])
        out['walls'] = len(lvl.get('walls') or [])
        out['blocks'] = len(lvl.get('blocks') or [])
        g = lvl.get('goalRegion') or {}
        out['goal'] = [g.get('x'), g.get('y'), g.get('width'), g.get('height')]
        s = lvl.get('playerStart') or {}
        out['start'] = [s.get('x'), s.get('y')]
    return out


@pytest.mark.parametrize('sample', SAMPLES, ids=SAMPLES)
def test_real_corpus_snapshot(tmp_path, monkeypatch, sample):
    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)

    got = _run(sample, tmp_path / sample.replace('.', '_'))

    baseline = {}
    if SNAPSHOT.exists():
        baseline = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    if os.environ.get('PG_UPDATE_BASELINE'):
        baseline[sample] = got
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(baseline, ensure_ascii=False, indent=1,
                                       sort_keys=True) + '\n', encoding='utf-8')
        return

    assert sample in baseline, (
        f'{sample} 没有快照。这是新入库的样本——先跑一次端到端确认结果合理，'
        '再用 PG_UPDATE_BASELINE=1 写入快照。'
    )
    assert got == baseline[sample], (
        f'{sample} 结果与快照不一致：\n'
        f'  快照：{baseline[sample]}\n'
        f'  本次：{got}\n'
        '如果这是本次改动的预期效果，用 PG_UPDATE_BASELINE=1 刷新快照；'
        '否则说明引入了回归。'
    )


def test_every_real_sample_has_a_snapshot():
    """防止样本入库后忘了写快照，导致回归实际是空跑。"""
    baseline = json.loads(SNAPSHOT.read_text(encoding='utf-8')) if SNAPSHOT.exists() else {}
    missing = [s for s in SAMPLES if s not in baseline]
    extra = [s for s in baseline if s not in SAMPLES]
    assert not missing, f'以下样本缺快照：{missing}'
    assert not extra, f'快照里有已删除的样本：{extra}'
