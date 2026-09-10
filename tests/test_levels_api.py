import io
import json
import threading
import time
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageFile

from app.api.levels import LEVEL_RERUN_ARTIFACTS
from app.level_contracts import DEFAULT_PLAYABILITY_PROFILE, LevelNeedsFix, derive_level_job_id
from app.services.job_store import JobStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('OUT_ROOT', str(tmp_path))
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    from app.main import create_app

    app = create_app()
    app.state.level_store = JobStore(
        'redis://unused', tmp_path / 'jobs',
        client=fakeredis.FakeStrictRedis(decode_responses=True),
        queue_key='pq:levels',
        terminal_states={'ready', 'needs_fix', 'needs_review', 'failed'},
        rerun_artifacts=LEVEL_RERUN_ARTIFACTS,
    )
    return TestClient(app)


def image_bytes(size=(800, 800), fmt='PNG', **kwargs):
    image = Image.new('RGB', size, 'white')
    output = io.BytesIO()
    image.save(output, format=fmt, **kwargs)
    return output.getvalue()


@pytest.fixture
def png_800():
    return image_bytes()


def upload(client, content, profile=None, schema='1.0', force=False, filename='level.png', content_type='image/png'):
    data = {'schemaVersion': schema}
    if profile is not None:
        data['playabilityProfile'] = json.dumps(profile, ensure_ascii=False)
    return client.post(
        '/v1/levels', params={'force': str(force).lower()}, data=data,
        files={'file': (filename, content, content_type)},
    )


def test_upload_valid_returns_202_and_enqueues(client, png_800):
    resp = upload(client, png_800)
    assert resp.status_code == 202
    body = resp.json()
    assert body['jobId'].startswith('level_')
    assert body['status'] == 'queued'
    assert body['statusUrl'] == f"/v1/levels/{body['jobId']}"
    assert body['createdAt'].endswith('Z')
    assert client.app.state.level_store.r.llen('pq:levels') == 1


def test_upload_atomically_writes_request_with_profile_and_timestamps(client, png_800, tmp_path):
    profile = DEFAULT_PLAYABILITY_PROFILE.model_dump()
    profile['maxJumpDistancePixels'] += 1
    body = upload(client, png_800, profile=profile).json()
    job_dir = tmp_path / 'jobs' / body['jobId']
    request = json.loads((job_dir / 'request.json').read_text(encoding='utf-8'))
    assert request == {
        'playabilityProfile': profile,
        'createdAt': body['createdAt'],
        'updatedAt': body['createdAt'],
    }
    assert not (job_dir / 'request.json.tmp').exists()


def test_upload_atomically_writes_request_with_profile_and_timestamps(client, png_800, tmp_path):
    profile = DEFAULT_PLAYABILITY_PROFILE.model_dump()
    profile['maxJumpDistancePixels'] += 1

    body = upload(client, png_800, profile=profile).json()
    job_dir = tmp_path / 'jobs' / body['jobId']
    request = json.loads((job_dir / 'request.json').read_text(encoding='utf-8'))

    assert request == {
        'playabilityProfile': profile,
        'createdAt': body['createdAt'],
        'updatedAt': body['createdAt'],
    }
    assert not (job_dir / 'request.json.tmp').exists()


def test_upload_atomically_writes_request_with_profile_and_timestamps(client, png_800, tmp_path):
    profile = DEFAULT_PLAYABILITY_PROFILE.model_dump()
    profile['maxJumpDistancePixels'] += 1
    body = upload(client, png_800, profile=profile).json()
    job_dir = tmp_path / 'jobs' / body['jobId']
    request = json.loads((job_dir / 'request.json').read_text(encoding='utf-8'))
    assert request == {
        'playabilityProfile': profile,
        'createdAt': body['createdAt'],
        'updatedAt': body['createdAt'],
    }
    assert not (job_dir / 'request.json.tmp').exists()


def test_same_image_same_profile_is_idempotent(client, png_800):
    profile = DEFAULT_PLAYABILITY_PROFILE.model_dump()
    first = upload(client, png_800, profile=profile).json()
    second = upload(client, png_800, profile=profile).json()
    assert second['jobId'] == first['jobId']
    assert client.app.state.level_store.r.llen('pq:levels') == 1


def test_different_profile_changes_job_id(client, png_800):
    first = upload(client, png_800).json()['jobId']
    profile = DEFAULT_PLAYABILITY_PROFILE.model_dump()
    profile['maxJumpDistancePixels'] += 1
    second = upload(client, png_800, profile=profile).json()['jobId']
    assert second != first
    assert client.app.state.level_store.r.llen('pq:levels') == 2


def test_force_resets_level_artifacts_and_requeues(client, png_800, tmp_path):
    accepted = upload(client, png_800).json()
    job_id = accepted['jobId']
    store = client.app.state.level_store
    job_dir = tmp_path / 'jobs' / job_id
    (job_dir / 'result.json').write_text('{"old": true}')
    (job_dir / 'rectified.png').write_bytes(b'old')
    (job_dir / 'paper-mask.png').write_bytes(b'old')
    (job_dir / 'ink-mask.png').write_bytes(b'old')
    (job_dir / 'overlay.png').write_bytes(b'old')
    (job_dir / 'level.json').write_text('{"old": true}')
    (job_dir / 'analysis.json').write_text('{"old": true}')
    (job_dir / 'transform.json').write_text('{"old": true}')
    (job_dir / 'llm-audit.json').write_text('{"old": true}')
    (job_dir / 'request.json').write_text('{"old": true}')
    (job_dir / 'rectified.tmp.png').write_bytes(b'old')
    (job_dir / 'transform.tmp.json').write_text('{"old": true}')
    (job_dir / 'input.png').write_bytes(b'normalized')
    store.set_status(job_id, 'ready', result={'status': 'ready'})

    resp = upload(client, png_800, force=True)
    assert resp.status_code == 202
    assert resp.json()['jobId'] == job_id
    assert store.r.llen('pq:levels') == 2
    assert store.get(job_id)['status'] == 'queued'
    assert 'result' not in store.get(job_id)
    assert not (job_dir / 'result.json').exists()
    assert not (job_dir / 'rectified.png').exists()
    assert not (job_dir / 'paper-mask.png').exists()
    assert not (job_dir / 'ink-mask.png').exists()
    assert not (job_dir / 'overlay.png').exists()
    assert not (job_dir / 'level.json').exists()
    assert not (job_dir / 'analysis.json').exists()
    assert not (job_dir / 'transform.json').exists()
    assert not (job_dir / 'llm-audit.json').exists()
    assert not list(job_dir.glob('*.tmp*'))
    request = json.loads((job_dir / 'request.json').read_text())
    assert request['createdAt'] == accepted['createdAt']
    assert request['updatedAt'] == accepted['createdAt']
    assert (job_dir / 'input.png').exists()
    assert 'stage' not in store.get(job_id)


def test_get_processing_includes_progress_stage(client, png_800):
    job_id = upload(client, png_800).json()['jobId']
    store = client.app.state.level_store
    store.set_status(job_id, 'processing')
    store.set_progress(job_id, 'detecting_platforms')
    body = client.get(f'/v1/levels/{job_id}').json()
    assert body['jobId'] == job_id
    assert body['status'] == 'processing'
    assert body['progress']['stage'] == 'detecting_platforms'
    assert body['progress']['percent'] == 55
    assert 'createdAt' in body and 'updatedAt' in body


def test_get_terminal_returns_saved_envelope(client, png_800):
    job_id = upload(client, png_800).json()['jobId']
    envelope = json.loads((Path(__file__).parents[1] / 'testdata' / 'levels' / 'contracts' / 'needs-fix.json').read_text())
    envelope['jobId'] = job_id
    client.app.state.level_store.set_status(job_id, 'needs_fix', result=envelope)
    assert client.get(f'/v1/levels/{job_id}').json() == LevelNeedsFix.model_validate(envelope).model_dump()


@pytest.mark.parametrize('content, expected', [
    (b'not image', (415, 'UNSUPPORTED_IMAGE_FORMAT')),
    (b'GIF89a' + b'0' * 100, (415, 'UNSUPPORTED_IMAGE_FORMAT')),
    (b'\x89PNG\r\n\x1a\n' + b'broken', (422, 'IMAGE_DECODE_FAILED')),
])
def test_upload_rejects_invalid_format_or_decode(client, content, expected):
    resp = upload(client, content)
    assert (resp.status_code, resp.json()['error']['code']) == expected


def test_upload_rejects_too_large(client):
    resp = upload(client, b'\x89PNG' + b'x' * (10 * 1024 * 1024))
    assert resp.status_code == 413
    assert resp.json()['error']['code'] == 'FILE_TOO_LARGE'


@pytest.mark.parametrize('size', [(799, 800), (12001, 800)])
def test_upload_rejects_dimension_boundaries(client, size):
    resp = upload(client, image_bytes(size))
    assert resp.status_code == 422
    assert resp.json()['error']['code'] == 'IMAGE_DECODE_FAILED'


def test_upload_rejects_over_40m_pixels(client):
    resp = upload(client, image_bytes((6325, 6325)))
    assert resp.status_code == 422
    assert resp.json()['error']['code'] == 'IMAGE_DECODE_FAILED'


def test_upload_rejects_animated_png(client):
    first = Image.new('RGBA', (800, 800), 'white')
    second = Image.new('RGBA', (800, 800), 'black')
    output = io.BytesIO()
    first.save(output, format='PNG', save_all=True, append_images=[second], duration=10)
    resp = upload(client, output.getvalue())
    assert resp.status_code == 422
    assert resp.json()['error']['code'] == 'IMAGE_DECODE_FAILED'


def test_upload_exif_orientation_is_transposed_and_saved_as_png(client, tmp_path):
    image = Image.new('RGB', (800, 1200), 'white')
    exif = image.getexif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, format='JPEG', exif=exif.tobytes())
    resp = upload(client, output.getvalue(), filename='level.jpg', content_type='image/jpeg')
    assert resp.status_code == 202
    normalized = Image.open(tmp_path / 'jobs' / resp.json()['jobId'] / 'input.png')
    assert normalized.size == (1200, 800)
    assert normalized.format == 'PNG'
    assert getattr(normalized, 'n_frames', 1) == 1


@pytest.mark.parametrize('profile', [
    '{bad json',
    json.dumps({'profileVersion': 'x'}),
    json.dumps({**DEFAULT_PLAYABILITY_PROFILE.model_dump(), 'landingTolerancePixels': -1}),
])
def test_upload_rejects_bad_profile(client, png_800, profile):
    resp = client.post('/v1/levels', data={'playabilityProfile': profile}, files={'file': ('x.png', png_800, 'image/png')})
    assert resp.status_code == 400
    assert resp.json()['error']['code'] == 'INVALID_PLAYABILITY_PROFILE'


def test_upload_rejects_unsupported_schema(client, png_800):
    resp = upload(client, png_800, schema='2.0')
    assert resp.status_code == 400
    assert resp.json()['error']['code'] == 'UNSUPPORTED_SCHEMA_VERSION'


def test_get_unknown_job_404(client):
    resp = client.get('/v1/levels/level_missing')
    assert resp.status_code == 404
    assert resp.json()['error']['code'] == 'JOB_NOT_FOUND'


@pytest.mark.parametrize('status', ['queued', 'processing'])
def test_force_rejects_inflight_job_without_mutating_directory_or_queue(client, png_800, tmp_path, status):
    job_id = upload(client, png_800).json()['jobId']
    store = client.app.state.level_store
    job_dir = tmp_path / 'jobs' / job_id
    (job_dir / 'input.png').write_bytes(b'in-flight-input')
    (job_dir / 'request.json').write_text('{"inFlight":true}')
    (job_dir / 'overlay.png').write_bytes(b'in-flight-artifact')
    if status == 'processing':
        store.set_status(job_id, status)
    queue_size = store.r.llen('pq:levels')

    response = upload(client, png_800, force=True)

    assert response.status_code == 409
    assert response.json()['error']['code'] == 'JOB_IN_PROGRESS'
    assert (job_dir / 'input.png').read_bytes() == b'in-flight-input'
    assert (job_dir / 'request.json').read_text() == '{"inFlight":true}'
    assert (job_dir / 'overlay.png').read_bytes() == b'in-flight-artifact'
    assert store.r.llen('pq:levels') == queue_size
    assert store.get(job_id)['status'] == status


def test_get_rejects_invalid_terminal_snapshot_after_redis_expiry(client, png_800, tmp_path):
    job_id = upload(client, png_800).json()['jobId']
    store = client.app.state.level_store
    store.r.delete('job:' + job_id)
    (tmp_path / 'jobs' / job_id / 'result.json').write_text('{"status":"ready"}')

    response = client.get('/v1/levels/' + job_id)

    assert response.status_code == 404
    assert response.json()['error']['code'] == 'JOB_NOT_FOUND'


def test_normalize_image_serializes_global_pillow_settings(monkeypatch):
    from app.api import levels

    entered = threading.Event()
    release = threading.Event()
    original_open = Image.open
    observed = []

    def blocking_open(*args, **kwargs):
        observed.append((Image.MAX_IMAGE_PIXELS, ImageFile.LOAD_TRUNCATED_IMAGES))
        if len(observed) == 1:
            entered.set()
            assert release.wait(2)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(levels.Image, 'open', blocking_open)
    workers = [threading.Thread(target=levels._normalize_image, args=(image_bytes(),)) for _ in range(2)]
    workers[0].start()
    assert entered.wait(2)
    workers[1].start()
    time.sleep(.05)
    assert len(observed) == 1
    release.set()
    for worker in workers:
        worker.join(2)
    assert not any(worker.is_alive() for worker in workers)
    assert all(limit == levels.MAX_PIXELS and not truncated for limit, truncated in observed)


def test_upload_redis_failure_returns_503(client, png_800, monkeypatch):
    import redis.exceptions

    def boom(job_id):
        raise redis.exceptions.ConnectionError('redis down')

    monkeypatch.setattr(client.app.state.level_store, 'get', boom)
    resp = upload(client, png_800)
    assert resp.status_code == 503
    assert resp.json()['error']['code'] == 'QUEUE_UNAVAILABLE'
