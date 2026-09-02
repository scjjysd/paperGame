import json

import pytest

from app.workers.character_worker import process_job
from app.workers.render_runner import relocate_artifacts


class FakeStore:
    def __init__(self, tmp_path):
        self.jobs_root = tmp_path
        self.calls = []

    def set_status(self, job_id, status, result=None):
        self.calls.append((job_id, status, result))


def _job_dir(tmp_path, job_id='char_t'):
    d = tmp_path / job_id
    d.mkdir()
    return d


def _write_result(job_dir, payload):
    (job_dir / 'result.json').write_text(json.dumps(payload))


def test_done_ready_sets_status_from_result(tmp_path):
    job_dir = _job_dir(tmp_path)
    payload = {'status': 'ready', 'characterId': 'char_t', 'animations': {'run': {}, 'jump': {}}}
    _write_result(job_dir, payload)
    store = FakeStore(tmp_path)
    status = process_job(store, 'char_t', runner=lambda d, t: 'done', timeout=5)
    assert status == 'ready'
    assert store.calls[-1] == ('char_t', 'ready', payload)


def test_crash_retries_once_then_render_crashed(tmp_path):
    job_dir = _job_dir(tmp_path)
    store = FakeStore(tmp_path)
    attempts = []

    def runner(d, t):
        attempts.append(1)
        return 'crash'

    status = process_job(store, 'char_t', runner=runner, timeout=5)
    assert status == 'failed'
    assert len(attempts) == 2
    assert store.calls[-1][2] == {'status': 'failed', 'code': 'RENDER_CRASHED'}


def test_timeout_then_success_no_failed_state(tmp_path):
    job_dir = _job_dir(tmp_path)
    payload = {'status': 'ready', 'characterId': 'char_t', 'animations': {}}
    _write_result(job_dir, payload)   # 第二次尝试前 result 已落盘
    outcomes = iter(['timeout', 'done'])
    store = FakeStore(tmp_path)
    status = process_job(store, 'char_t', runner=lambda d, t: next(outcomes), timeout=5)
    assert status == 'ready'
    assert all(c[1] != 'failed' for c in store.calls)   # 重试成功后不应出现 failed 终态


def test_needs_correction_is_business_terminal_no_retry(tmp_path):
    job_dir = _job_dir(tmp_path)
    payload = {'status': 'needs_correction', 'reason': 'NO_HUMANOID', 'maskUrl': '/m.png', 'joints': []}
    _write_result(job_dir, payload)
    store = FakeStore(tmp_path)
    attempts = []

    def runner(d, t):
        attempts.append(1)
        return 'done'

    status = process_job(store, 'char_t', runner=runner, timeout=5)
    assert status == 'needs_correction'
    assert len(attempts) == 1   # 业务失败不重试


def test_corrupt_result_json_retries_then_render_crashed(tmp_path):
    job_dir = _job_dir(tmp_path)
    (job_dir / 'result.json').write_text('{"status": "ready", ')   # 截断的 JSON
    store = FakeStore(tmp_path)
    attempts = []

    def runner(d, t):
        attempts.append(1)
        return 'done'

    status = process_job(store, 'char_t', runner=runner, timeout=5)
    assert status == 'failed'
    assert len(attempts) == 2   # 损坏按基础设施故障重试
    assert store.calls[-1][2] == {'status': 'failed', 'code': 'RENDER_CRASHED'}


def test_relocate_artifacts_moves_pngs_and_anno(tmp_path):
    # 模拟 render_character 的产物结构：work/input/{run,jump}/x.png + work/input/anno
    job_dir = _job_dir(tmp_path)
    work = job_dir / 'work' / 'input'
    for m in ('run', 'jump'):
        (work / m).mkdir(parents=True)
        (work / m / f'{m}.png').write_bytes(b'png')
    (work / 'anno').mkdir(parents=True)
    (work / 'anno' / 'mask.png').write_bytes(b'mask')
    relocate_artifacts(job_dir)
    assert (job_dir / 'run.png').read_bytes() == b'png'
    assert (job_dir / 'jump.png').read_bytes() == b'png'
    assert (job_dir / 'anno' / 'mask.png').read_bytes() == b'mask'
    assert not (job_dir / 'work').exists()
