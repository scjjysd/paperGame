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


def test_get_unknown_returns_none(store):
    assert store.get('char_missing') is None


def test_terminal_state_is_immutable(store):
    store.create('char_a')
    store.set_status('char_a', 'ready', result={'status': 'ready'})
    store.set_status('char_a', 'failed', result={'status': 'failed', 'code': 'INTERNAL'})
    data = store.get('char_a')
    assert data['status'] == 'ready'   # 终态不被覆写
