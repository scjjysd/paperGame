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
from app.api.urls import absolutize
from app.level_contracts import (DEFAULT_PLAYABILITY_PROFILE, LEVEL_ERROR_MESSAGES,
                                 LEVEL_STATUS_MESSAGES, LevelNeedsFix, derive_level_job_id)
from app.services.job_store import JobStore

BASE = 'http://testserver'


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


def animated_png() -> bytes:
    first = Image.new('RGBA', (800, 800), 'white')
    second = Image.new('RGBA', (800, 800), 'black')
    output = io.BytesIO()
    first.save(output, format='PNG', save_all=True, append_images=[second], duration=10)
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
    # 完整地址 + 中文状态说明：客户端不必拼 Base URL，也能直接看懂到哪一步了
    assert body['statusUrl'] == f"{BASE}/v1/levels/{body['jobId']}"
    assert body['viewUrl'] == f"{BASE}/v1/levels/{body['jobId']}/view"
    assert body['message'] == LEVEL_STATUS_MESSAGES['queued']
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


def test_idempotent_replay_after_redis_expiry_keeps_created_at(client, png_800):
    """Redis TTL 过期后只剩磁盘快照，幂等返回也得带上真实创建时间。"""
    profile = DEFAULT_PLAYABILITY_PROFILE.model_dump()
    job_id = upload(client, png_800, profile=profile).json()['jobId']
    snapshot = client.app.state.level_store.jobs_root / job_id / 'result.json'
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps({'status': 'needs_fix',
                                    'createdAt': '2026-09-10T10:26:24Z',
                                    'updatedAt': '2026-09-10T10:26:25Z'}))
    client.app.state.level_store.r.flushall()      # 模拟 24h TTL 到期

    body = upload(client, png_800, profile=profile).json()

    assert body['status'] == 'needs_fix'
    assert body['createdAt'] == '2026-09-10T10:26:24Z'
    assert client.app.state.level_store.r.llen('pq:levels') == 0   # 未重复入队


def test_idempotent_replay_omits_missing_created_at(client, png_800):
    """旧快照没有时间戳时宁可不给字段，也不要给 null 让强类型客户端反序列化失败。"""
    profile = DEFAULT_PLAYABILITY_PROFILE.model_dump()
    job_id = upload(client, png_800, profile=profile).json()['jobId']
    snapshot = client.app.state.level_store.jobs_root / job_id / 'result.json'
    snapshot.write_text(json.dumps({'status': 'needs_fix'}))
    client.app.state.level_store.r.flushall()

    body = upload(client, png_800, profile=profile).json()

    assert 'createdAt' not in body
    assert None not in body.values()


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
    # updatedAt 由 force 刷新，不能断言它等于 createdAt：_now 只有秒级精度，用例正好跨秒时会随机失败。
    # 改与 reset 后的任务记录比对，既确定又能钉住「request.json 带的是本次重跑的时间戳」。
    assert request['updatedAt'] == store.get(job_id)['updatedAt']
    assert (job_dir / 'input.png').exists()
    assert 'stage' not in store.get(job_id)


def test_force_after_redis_expiry_rescues_created_at_from_snapshot(client, png_800, tmp_path):
    """Redis 过期后 force 重跑：createdAt 只能从磁盘快照救回。

    写进 request.json 的 null 会让关卡子进程的契约校验抛 ValidationError，
    任务被归成 PROCESSING_CRASHED（告诉客户端可重试，但重试永远好不了）。
    """
    job_id = upload(client, png_800).json()['jobId']
    store = client.app.state.level_store
    job_dir = tmp_path / 'jobs' / job_id
    snapshot = {'status': 'needs_fix', 'createdAt': '2026-09-10T10:26:24Z',
                'updatedAt': '2026-09-10T10:26:25Z'}
    (job_dir / 'result.json').write_text(json.dumps(snapshot))
    store.r.delete('job:' + job_id)          # 模拟 TTL 过期：只剩磁盘快照

    resp = upload(client, png_800, force=True)

    assert resp.status_code == 202
    assert resp.json()['createdAt'] == snapshot['createdAt']
    request = json.loads((job_dir / 'request.json').read_text())
    assert request['createdAt'] == snapshot['createdAt']
    # updatedAt 由 reset 刷新（重跑本身就是一次新的状态变更），这里只要求它是可用字符串
    assert request['updatedAt']
    assert store.get(job_id)['createdAt'] == snapshot['createdAt']


def test_force_with_legacy_snapshot_without_timestamps_still_emits_real_time(client, png_800, tmp_path):
    """早期快照可能根本没有时间戳字段：救不到就用当前时间，绝不能写 null。"""
    job_id = upload(client, png_800).json()['jobId']
    store = client.app.state.level_store
    job_dir = tmp_path / 'jobs' / job_id
    (job_dir / 'result.json').write_text(json.dumps({'status': 'needs_fix'}))
    store.r.delete('job:' + job_id)

    resp = upload(client, png_800, force=True)

    created = resp.json()['createdAt']
    assert created and created != 'null'
    assert json.loads((job_dir / 'request.json').read_text())['createdAt'] == created


def test_get_processing_includes_progress_stage(client, png_800):
    job_id = upload(client, png_800).json()['jobId']
    store = client.app.state.level_store
    store.set_status(job_id, 'processing')
    store.set_progress(job_id, 'detecting_platforms')
    body = client.get(f'/v1/levels/{job_id}').json()
    assert body['jobId'] == job_id
    assert body['status'] == 'processing'
    assert body['progress']['stage'] == 'detecting_platforms'
    assert body['progress']['stageLabel'] == '识别平台'
    assert body['progress']['percent'] == 55
    assert 'createdAt' in body and 'updatedAt' in body


def test_get_terminal_returns_saved_envelope(client, png_800):
    job_id = upload(client, png_800).json()['jobId']
    envelope = json.loads((Path(__file__).parents[1] / 'testdata' / 'levels' / 'contracts' / 'needs-fix.json').read_text())
    envelope['jobId'] = job_id
    client.app.state.level_store.set_status(job_id, 'needs_fix', result=envelope)

    body = client.get(f'/v1/levels/{job_id}').json()

    # 契约字段逐字保留，只在产物地址上补全基址，并多出中文说明与预览链接
    expected = absolutize(LevelNeedsFix.model_validate(envelope).model_dump(), BASE)
    assert body['result'] == expected['result']
    assert body['schemaVersion'] == expected['schemaVersion']
    assert body['algorithmVersion'] == expected['algorithmVersion']
    assert body['createdAt'] == expected['createdAt']
    assert body['status'] == 'needs_fix'
    assert body['message'] == LEVEL_STATUS_MESSAGES['needs_fix']
    assert body['statusUrl'] == f'{BASE}/v1/levels/{job_id}'
    assert body['viewUrl'] == f'{BASE}/v1/levels/{job_id}/view'
    assert body['result']['artifacts']['levelJsonUrl'].startswith(BASE + '/artifacts/')


def test_stored_envelope_keeps_relative_urls(client, png_800):
    """完整地址只在响应出口补：存储里写绝对地址会让历史任务跟着部署地址一起失效。"""
    job_id = upload(client, png_800).json()['jobId']
    envelope = json.loads((Path(__file__).parents[1] / 'testdata' / 'levels' / 'contracts' / 'ready.json').read_text())
    envelope['jobId'] = job_id
    store = client.app.state.level_store
    store.set_status(job_id, 'ready', result=envelope)
    client.get(f'/v1/levels/{job_id}')
    assert json.loads(store.get(job_id)['result']) == envelope


@pytest.mark.parametrize('content, expected', [
    (b'not image', (415, 'UNSUPPORTED_IMAGE_FORMAT')),
    (b'GIF89a' + b'0' * 100, (415, 'UNSUPPORTED_IMAGE_FORMAT')),
    (b'\x89PNG\r\n\x1a\n' + b'broken', (422, 'IMAGE_DECODE_FAILED')),
])
def test_upload_rejects_invalid_format_or_decode(client, content, expected):
    resp = upload(client, content)
    assert (resp.status_code, resp.json()['error']['code']) == expected
    # 错误体除了码还要给中文原因，否则只能靠猜
    assert resp.json()['error']['message']


@pytest.mark.parametrize('make, reason, keyword', [
    (lambda: image_bytes((799, 800)), 'invalid dimensions', '边长'),
    (lambda: image_bytes((12001, 800)), 'invalid dimensions', '边长'),
    # 超过 4000 万像素时 Pillow 的解压炸弹门禁先触发，不能笼统归为“文件损坏”
    (lambda: image_bytes((6325, 6325)), 'decompression bomb', '像素'),
    (lambda: animated_png(), 'animated image', '动图'),
])
def test_upload_rejects_undecodable_image_with_precise_chinese_reason(client, make, reason, keyword):
    """边界、超大、动图不能笼统归为“无法安全解码”：details 给内部原因，message 给中文说明。"""
    resp = upload(client, make())
    assert resp.status_code == 422
    body = resp.json()['error']
    assert body['code'] == 'IMAGE_DECODE_FAILED'
    assert body['details'] == {'reason': reason}
    assert keyword in body['message']


def test_upload_rejects_too_large(client):
    resp = upload(client, b'\x89PNG' + b'x' * (10 * 1024 * 1024))
    assert resp.status_code == 413
    assert resp.json()['error']['code'] == 'FILE_TOO_LARGE'
    assert resp.json()['error']['message'] == LEVEL_ERROR_MESSAGES['FILE_TOO_LARGE']


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
