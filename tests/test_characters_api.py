import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.contracts import ERROR_MESSAGES, REASON_MESSAGES, STATUS_MESSAGES, derive_job_id
from app.services.job_store import JobStore

BASE = 'http://testserver'

PNG_1PX = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4'
    '890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('OUT_ROOT', str(tmp_path))
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    from app.main import create_app
    app = create_app()
    app.state.store = JobStore('redis://unused', tmp_path / 'jobs',
                               client=fakeredis.FakeStrictRedis(decode_responses=True))
    return TestClient(app)


def _upload(client, content: bytes, filename='c.png', content_type='image/png'):
    return client.post('/v1/characters',
                       files={'file': (filename, content, content_type)})


def _upload_force(client, content: bytes = PNG_1PX):
    return client.post('/v1/characters', params={'force': 'true'},
                       files={'file': ('c.png', content, 'image/png')})


def test_healthz(client):
    assert client.get('/healthz').json() == {'status': 'ok'}


def test_upload_valid_returns_202_and_enqueues(client, tmp_path):
    resp = _upload(client, PNG_1PX)
    assert resp.status_code == 202
    job_id = resp.json()['jobId']
    assert job_id == derive_job_id(PNG_1PX)
    # 入队响应直接给完整地址，客户端与浏览器都不必再拼 Base URL
    assert resp.json()['statusUrl'] == f'{BASE}/v1/characters/{job_id}'
    assert resp.json()['viewUrl'] == f'{BASE}/v1/characters/{job_id}/view'
    assert (tmp_path / 'jobs' / job_id / 'input.png').read_bytes() == PNG_1PX
    assert client.app.state.store.r.llen('pq:characters') == 1
    status = client.get(f'/v1/characters/{job_id}').json()
    assert status['status'] == 'queued'
    assert status['message'] == STATUS_MESSAGES['queued']


def test_upload_idempotent_no_double_enqueue(client):
    first = _upload(client, PNG_1PX).json()['jobId']
    second = _upload(client, PNG_1PX).json()['jobId']
    assert first == second
    assert client.app.state.store.r.llen('pq:characters') == 1


def test_upload_force_re_enqueues_same_job(client):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    store = client.app.state.store
    store.set_status(job_id, 'needs_correction',
                     result={'status': 'needs_correction', 'reason': 'NO_HUMANOID'})
    resp = _upload_force(client)
    assert resp.status_code == 202
    assert resp.json()['jobId'] == job_id          # 同图同 jobId
    assert store.r.llen('pq:characters') == 2      # force 绕过幂等短路重新入队
    data = store.get(job_id)
    assert data['status'] == 'queued'              # 终态被重置
    assert 'result' not in data                    # 旧结果已丢弃


def test_upload_force_clears_stale_artifacts(client, tmp_path):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    store = client.app.state.store
    job_dir = tmp_path / 'jobs' / job_id
    (job_dir / 'result.json').write_text('{"status":"ready"}')
    (job_dir / 'run.png').write_bytes(PNG_1PX)
    (job_dir / 'anno').mkdir()
    (job_dir / 'anno' / 'mask.png').write_bytes(PNG_1PX)
    store.set_status(job_id, 'ready', result={'status': 'ready'})
    assert _upload_force(client).status_code == 202
    assert not (job_dir / 'result.json').exists()
    assert not (job_dir / 'run.png').exists()
    assert not (job_dir / 'anno').exists()
    assert (job_dir / 'input.png').exists()        # 入图保留并被覆写


def test_upload_too_large(client):
    resp = _upload(client, b'\x89PNG' + b'x' * (10 * 1024 * 1024))
    assert resp.status_code == 400
    assert resp.json()['code'] == 'FILE_TOO_LARGE'
    assert resp.json()['message'] == ERROR_MESSAGES['FILE_TOO_LARGE']


def test_upload_not_an_image(client):
    resp = _upload(client, b'hello world')
    assert resp.status_code == 400
    assert resp.json()['code'] == 'NOT_AN_IMAGE'
    assert resp.json()['message'] == ERROR_MESSAGES['NOT_AN_IMAGE']


def test_upload_unsupported_format_gif(client):
    resp = _upload(client, b'GIF89a....')
    assert resp.status_code == 400
    assert resp.json()['code'] == 'UNSUPPORTED_FORMAT'
    assert resp.json()['message'] == ERROR_MESSAGES['UNSUPPORTED_FORMAT']


def test_get_unknown_job_404(client):
    resp = client.get('/v1/characters/char_nope')
    assert resp.status_code == 404
    assert resp.json()['code'] == 'JOB_NOT_FOUND'
    assert resp.json()['message'] == ERROR_MESSAGES['JOB_NOT_FOUND']


def test_get_ready_returns_full_contract(client, tmp_path):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    store = client.app.state.store
    payload = {'status': 'ready', 'characterId': job_id, 'animations': {
        'run': {'spriteSheetUrl': f'/artifacts/{job_id}/run.png', 'frameCount': 13, 'fps': 12,
                'frameWidth': 481, 'frameHeight': 655, 'footAnchor': {'x': 240, 'y': 655}},
        'jump': {'spriteSheetUrl': f'/artifacts/{job_id}/jump.png', 'frameCount': 12, 'fps': 12,
                 'frameWidth': 481, 'frameHeight': 655, 'footAnchor': {'x': 240, 'y': 655}}}}
    store.set_status(job_id, 'ready', result=payload)
    body = client.get(f'/v1/characters/{job_id}').json()
    assert body['status'] == 'ready'
    assert body['characterId'] == job_id
    assert body['message'] == STATUS_MESSAGES['ready']
    # 存储里的终态载荷仍存相对路径，只有响应出口才补成完整地址
    assert json.loads(store.get(job_id)['result']) == payload
    for motion in ('run', 'jump'):
        expected = payload['animations'][motion]
        assert body['animations'][motion] == {
            **expected, 'spriteSheetUrl': BASE + expected['spriteSheetUrl']}


def test_get_needs_correction_shape(client):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    joints = [{'name': f'j{i}', 'loc': [i, i], 'parent': None} for i in range(16)]
    payload = {'status': 'needs_correction', 'reason': 'NO_HUMANOID',
               'maskUrl': f'/artifacts/{job_id}/anno/mask.png', 'joints': joints}
    client.app.state.store.set_status(job_id, 'needs_correction', result=payload)
    body = client.get(f'/v1/characters/{job_id}').json()
    assert body['reason'] == 'NO_HUMANOID'
    assert body['message'] == REASON_MESSAGES['NO_HUMANOID']
    assert body['maskUrl'] == f'{BASE}/artifacts/{job_id}/anno/mask.png'
    assert len(body['joints']) == 16


def test_get_failed_carries_chinese_reason(client):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    client.app.state.store.set_status(job_id, 'failed', result={'status': 'failed', 'code': 'RENDER_CRASHED'})
    body = client.get(f'/v1/characters/{job_id}').json()
    assert body['code'] == 'RENDER_CRASHED'
    assert body['message'] == ERROR_MESSAGES['RENDER_CRASHED']


def test_artifacts_static_serving(client, tmp_path):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    sheet = tmp_path / 'jobs' / job_id / 'run.png'
    sheet.write_bytes(PNG_1PX)
    resp = client.get(f'/artifacts/{job_id}/run.png')
    assert resp.status_code == 200
    assert resp.content == PNG_1PX


def test_upload_after_expiry_re_enqueues_same_job_id(client):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    store = client.app.state.store
    # 模拟 TTL 过期：删除 Redis 记录（result.json 由 worker 落盘，此处从未生成）
    store.r.delete('job:' + job_id)
    assert store.get(job_id) is None
    second = _upload(client, PNG_1PX).json()['jobId']
    assert second == job_id                       # 同图 → 同 jobId
    assert store.r.llen('pq:characters') == 2     # 按新任务重新入队


def test_upload_redis_failure_returns_503(client, monkeypatch):
    import redis.exceptions

    def boom(job_id):
        raise redis.exceptions.ConnectionError('redis down')

    monkeypatch.setattr(client.app.state.store, 'get', boom)
    resp = _upload(client, PNG_1PX)
    assert resp.status_code == 503
    assert resp.json()['code'] == 'QUEUE_UNAVAILABLE'
    assert resp.json()['message'] == ERROR_MESSAGES['QUEUE_UNAVAILABLE']


def test_get_redis_failure_returns_503(client, monkeypatch):
    import redis.exceptions

    def boom(job_id):
        raise redis.exceptions.ConnectionError('redis down')

    monkeypatch.setattr(client.app.state.store, 'get', boom)
    resp = client.get('/v1/characters/char_x')
    assert resp.status_code == 503
    assert resp.json()['code'] == 'QUEUE_UNAVAILABLE'
