import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.contracts import derive_job_id
from app.services.job_store import JobStore

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


def test_healthz(client):
    assert client.get('/healthz').json() == {'status': 'ok'}


def test_upload_valid_returns_202_and_enqueues(client, tmp_path):
    resp = _upload(client, PNG_1PX)
    assert resp.status_code == 202
    job_id = resp.json()['jobId']
    assert job_id == derive_job_id(PNG_1PX)
    assert (tmp_path / 'jobs' / job_id / 'input.png').read_bytes() == PNG_1PX
    assert client.app.state.store.r.llen('pq:characters') == 1
    status = client.get(f'/v1/characters/{job_id}').json()
    assert status['status'] == 'queued'


def test_upload_idempotent_no_double_enqueue(client):
    first = _upload(client, PNG_1PX).json()['jobId']
    second = _upload(client, PNG_1PX).json()['jobId']
    assert first == second
    assert client.app.state.store.r.llen('pq:characters') == 1


def test_upload_too_large(client):
    resp = _upload(client, b'\x89PNG' + b'x' * (10 * 1024 * 1024))
    assert resp.status_code == 400
    assert resp.json()['code'] == 'FILE_TOO_LARGE'


def test_upload_not_an_image(client):
    resp = _upload(client, b'hello world')
    assert resp.status_code == 400
    assert resp.json()['code'] == 'NOT_AN_IMAGE'


def test_upload_unsupported_format_gif(client):
    resp = _upload(client, b'GIF89a....')
    assert resp.status_code == 400
    assert resp.json()['code'] == 'UNSUPPORTED_FORMAT'


def test_get_unknown_job_404(client):
    resp = client.get('/v1/characters/char_nope')
    assert resp.status_code == 404
    assert resp.json()['code'] == 'JOB_NOT_FOUND'


def test_get_ready_returns_full_contract(client, tmp_path):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    store = client.app.state.store
    payload = {'status': 'ready', 'characterId': job_id, 'animations': {
        'run': {'spriteSheetUrl': f'/artifacts/{job_id}/run.png', 'frameCount': 13, 'fps': 12,
                'frameWidth': 481, 'frameHeight': 655, 'footAnchor': {'x': 240, 'y': 655}},
        'jump': {'spriteSheetUrl': f'/artifacts/{job_id}/jump.png', 'frameCount': 12, 'fps': 12,
                 'frameWidth': 481, 'frameHeight': 655, 'footAnchor': {'x': 240, 'y': 655}}}}
    store.set_status(job_id, 'ready', result=payload)
    resp = client.get(f'/v1/characters/{job_id}')
    assert resp.status_code == 200
    assert resp.json() == payload


def test_get_needs_correction_shape(client):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    joints = [{'name': f'j{i}', 'loc': [i, i], 'parent': None} for i in range(16)]
    payload = {'status': 'needs_correction', 'reason': 'NO_HUMANOID',
               'maskUrl': f'/artifacts/{job_id}/anno/mask.png', 'joints': joints}
    client.app.state.store.set_status(job_id, 'needs_correction', result=payload)
    body = client.get(f'/v1/characters/{job_id}').json()
    assert body['reason'] == 'NO_HUMANOID'
    assert len(body['joints']) == 16


def test_artifacts_static_serving(client, tmp_path):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    sheet = tmp_path / 'jobs' / job_id / 'run.png'
    sheet.write_bytes(PNG_1PX)
    resp = client.get(f'/artifacts/{job_id}/run.png')
    assert resp.status_code == 200
    assert resp.content == PNG_1PX
