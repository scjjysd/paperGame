import json

import fakeredis
import pytest

from app.services.job_store import QUEUE_KEY, JobStore


@pytest.fixture
def store(tmp_path):
    return JobStore('redis://unused', tmp_path, client=fakeredis.FakeStrictRedis(decode_responses=True))


def test_create_sets_queued_with_ttl(store):
    store.create('char_a')
    data = store.get('char_a')
    assert data['status'] == 'queued'
    assert 'updatedAt' in data
    assert store.r.ttl('job:char_a') > 0


def test_enqueue_dequeue_roundtrip(store):
    store.enqueue('char_a')
    store.enqueue('char_b')
    assert store.r.llen(QUEUE_KEY) == 2
    assert store.dequeue(timeout=1) == 'char_a'   # FIFO：LPUSH + BRPOP
    assert store.dequeue(timeout=1) == 'char_b'
    assert store.dequeue(timeout=1) is None


def test_set_status_stores_result_json(store):
    store.create('char_a')
    payload = {'status': 'ready', 'characterId': 'char_a', 'animations': {}}
    store.set_status('char_a', 'ready', result=payload)
    data = store.get('char_a')
    assert data['status'] == 'ready'
    assert json.loads(data['result']) == payload


def test_get_falls_back_to_result_json_snapshot(store, tmp_path):
    # Redis 无记录（TTL 过期模拟），但 runner 落盘的 result.json 存在
    job_dir = tmp_path / 'char_snap'
    job_dir.mkdir()
    payload = {'status': 'needs_correction', 'reason': 'NO_HUMANOID', 'maskUrl': '/m.png', 'joints': []}
    (job_dir / 'result.json').write_text(json.dumps(payload))
    data = store.get('char_snap')
    assert data['status'] == 'needs_correction'
    assert json.loads(data['result']) == payload


def test_get_corrupt_result_json_snapshot_returns_none(store, tmp_path):
    # 截断的 result.json（如 worker 被 SIGKILL 打断落盘）→ 损坏视同不存在，API 层 404 可重传
    job_dir = tmp_path / 'char_corrupt'
    job_dir.mkdir()
    (job_dir / 'result.json').write_text('{"status": ')
    assert store.get('char_corrupt') is None


def test_get_unknown_returns_none(store):
    assert store.get('char_missing') is None


def test_terminal_state_is_immutable(store):
    store.create('char_a')
    store.set_status('char_a', 'ready', result={'status': 'ready'})
    store.set_status('char_a', 'failed', result={'status': 'failed', 'code': 'INTERNAL'})
    data = store.get('char_a')
    assert data['status'] == 'ready'   # 终态不被覆写


def test_custom_queue_terminal_states_and_rerun_artifacts(tmp_path):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    store = JobStore('redis://unused', tmp_path, client=r,
                     queue_key='pq:levels',
                     terminal_states=frozenset({'ready', 'needs_fix', 'needs_review', 'failed'}),
                     rerun_artifacts=('result.json', 'rectified.png'))
    store.create('level_a')
    store.enqueue('level_a')
    assert r.llen('pq:characters') == 0
    assert store.dequeue(timeout=1) == 'level_a'
    store.set_status('level_a', 'needs_fix', result={'status': 'needs_fix'})
    store.set_status('level_a', 'processing')
    assert store.get('level_a')['status'] == 'needs_fix'

    job_dir = tmp_path / 'level_a'
    job_dir.mkdir()
    (job_dir / 'result.json').write_text('{}')
    (job_dir / 'rectified.png').write_bytes(b'png')
    (job_dir / 'input.png').write_bytes(b'input')
    store.reset('level_a')
    assert not (job_dir / 'result.json').exists()
    assert not (job_dir / 'rectified.png').exists()
    assert (job_dir / 'input.png').exists()


def test_set_progress_preserves_status(tmp_path):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    store = JobStore('redis://unused', tmp_path, client=r)
    store.create('level_a')
    original = store.get('level_a')

    store.set_progress('level_a', 'rectifying_paper')

    updated = store.get('level_a')
    assert updated['status'] == 'queued'
    assert updated['stage'] == 'rectifying_paper'
    assert updated['updatedAt'] >= original['updatedAt']
