"""render_runner 早期失败路径：NeedsCorrection（NO_HUMANOID）→ 退出码 0 + needs_correction result.json。

早期失败时标注产物不存在（char_cfg.yaml 缺失），joints 必须为空数组（契约放宽项）。
"""
import json

import pytest

from app.services.annotations import NeedsCorrection
from app.services.character_pipeline import CharacterPipeline
from app.workers import render_runner


@pytest.fixture
def early_failure_job(tmp_path, monkeypatch):
    """monkeypatch 管线入口抛 NO_HUMANOID，执行 run() 后返回 job_dir。"""
    def boom(self, input_path, motions=('run', 'jump')):
        raise NeedsCorrection('NO_HUMANOID', 'test')

    monkeypatch.setattr(CharacterPipeline, 'render_character', boom)
    job_dir = tmp_path / 'char_early'
    job_dir.mkdir()
    (job_dir / 'input.png').write_bytes(b'\x89PNG-fake')
    exit_code = render_runner.run(job_dir)
    assert exit_code == 0   # 业务终态已写入 result.json，非基础设施故障
    return job_dir


def test_early_failure_result_json_shape(early_failure_job):
    payload = json.loads((early_failure_job / 'result.json').read_text())
    assert payload['status'] == 'needs_correction'
    assert payload['reason'] == 'NO_HUMANOID'
    assert payload['joints'] == []   # 早期失败无可编辑标注


def test_early_failure_mask_url_contains_job_id(early_failure_job):
    payload = json.loads((early_failure_job / 'result.json').read_text())
    assert early_failure_job.name in payload['maskUrl']
    assert payload['maskUrl'] == '/artifacts/char_early/anno/mask.png'
