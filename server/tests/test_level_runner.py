"""关卡 runner：业务终态、进度回写与原子快照。"""
import json
from pathlib import Path

from app.level_contracts import ALGORITHM_VERSION, SCHEMA_VERSION
from app.workers import level_runner


class FakeStore:
    def __init__(self):
        self.progress = []

    def set_progress(self, job_id, stage):
        self.progress.append((job_id, stage))


def _job(tmp_path):
    job_dir = tmp_path / 'level_test'
    job_dir.mkdir()
    (job_dir / 'input.png').write_bytes(b'png')
    return job_dir


def _review_payload(job_id):
    return {
        'jobId': job_id,
        'status': 'needs_review',
        'schemaVersion': SCHEMA_VERSION,
        'algorithmVersion': ALGORITHM_VERSION,
        'createdAt': '2026-09-10T00:00:00Z',
        'updatedAt': '2026-09-10T00:00:01Z',
        'review': {
            'reason': 'PAPER_NOT_FOUND',
            'message': '未检测到纸张。',
            'candidates': [{
                'id': 'paper_001',
                'type': 'paper',
                'region': {'x': 0, 'y': 0, 'width': 10, 'height': 10, 'confidence': 0.5},
                'confidence': 0.5,
            }],
            'suggestions': [],
        },
        'artifacts': {
            'rectifiedImageUrl': '/artifacts/{}/rectified.png'.format(job_id),
            'overlayImageUrl': '/artifacts/{}/overlay.png'.format(job_id),
        },
    }


def test_runner_writes_business_terminal_and_returns_zero(tmp_path, monkeypatch):
    job_dir = _job(tmp_path)
    payload = _review_payload(job_dir.name)
    monkeypatch.setattr(level_runner, 'parse', lambda *args, **kwargs: payload)

    assert level_runner.run(job_dir, store=FakeStore()) == 0
    assert json.loads((job_dir / 'result.json').read_text()) == payload


def test_runner_infrastructure_exception_returns_nonzero_without_snapshot(tmp_path, monkeypatch):
    job_dir = _job(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError('opencv unavailable')

    monkeypatch.setattr(level_runner, 'parse', fail)
    assert level_runner.run(job_dir, store=FakeStore()) == 2
    assert not (job_dir / 'result.json').exists()


def test_runner_progress_callback_updates_same_level_store(tmp_path, monkeypatch):
    job_dir = _job(tmp_path)
    payload = _review_payload(job_dir.name)
    store = FakeStore()

    def parse_with_progress(path, progress=None):
        progress('rectifying_paper')
        progress('detecting_platforms')
        return payload

    monkeypatch.setattr(level_runner, 'parse', parse_with_progress)
    assert level_runner.run(job_dir, store=store) == 0
    assert store.progress == [
        (job_dir.name, 'rectifying_paper'),
        (job_dir.name, 'detecting_platforms'),
    ]


def test_runner_refreshes_updated_at_and_validates_terminal_before_publishing(tmp_path, monkeypatch):
    job_dir = _job(tmp_path)
    payload = _review_payload(job_dir.name)
    payload['updatedAt'] = '2000-01-01T00:00:00Z'
    stale_updated_at = payload['updatedAt']
    monkeypatch.setattr(level_runner, 'parse', lambda *args, **kwargs: payload)

    assert level_runner.run(job_dir, store=FakeStore()) == 0
    published = json.loads((job_dir / 'result.json').read_text())
    assert published['createdAt'] == payload['createdAt']
    assert published['updatedAt'].endswith('Z')
    assert published['updatedAt'] != stale_updated_at


def test_runner_atomically_replaces_result_snapshot(tmp_path, monkeypatch):
    job_dir = _job(tmp_path)
    payload = _review_payload(job_dir.name)
    (job_dir / 'result.json').write_text('{"old":true}')
    replaced = []
    original_replace = Path.replace

    def recording_replace(path, target):
        replaced.append((path.name, Path(target).name))
        return original_replace(path, target)

    monkeypatch.setattr(level_runner, 'parse', lambda *args, **kwargs: payload)
    monkeypatch.setattr(Path, 'replace', recording_replace)
    assert level_runner.run(job_dir, store=FakeStore()) == 0
    assert ('result.json.tmp', 'result.json') in replaced
    assert not (job_dir / 'result.json.tmp').exists()
