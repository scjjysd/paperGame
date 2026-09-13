"""关卡 worker：终态校验、重试、超时与独立队列。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.workers import level_worker

FIXTURES = Path(__file__).parents[1] / 'testdata' / 'levels' / 'contracts'


class FakeStore:
    def __init__(self, tmp_path):
        self.jobs_root = tmp_path
        self.calls = []

    def set_status(self, job_id, status, result=None):
        self.calls.append((job_id, status, result))


def _job(tmp_path, job_id='level_test'):
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    return job_dir


def _failed_payload(job_id='level_test', code='UPSTREAM_FAILED'):
    return {
        'jobId': job_id,
        'status': 'failed',
        'error': {'code': code, 'message': '依赖失败。', 'retryable': True, 'requestId': 'req_test'},
        'createdAt': '2026-09-10T00:00:00Z',
        'updatedAt': '2026-09-10T00:00:01Z',
    }


@pytest.mark.parametrize('status', ['ready', 'needs_fix', 'needs_review'])
def test_business_terminal_is_completed_without_retry(tmp_path, status):
    job_dir = _job(tmp_path)
    fixture = {'ready': 'ready.json', 'needs_fix': 'needs-fix.json',
               'needs_review': 'needs-review.json'}[status]
    payload = json.loads((FIXTURES / fixture).read_text())
    (job_dir / 'result.json').write_text(json.dumps(payload))
    attempts = []

    result = level_worker.process_job(
        FakeStore(tmp_path), job_dir.name,
        runner=lambda path, timeout: attempts.append(path) or 'done', timeout=5)

    assert result == status
    assert len(attempts) == 1


@pytest.mark.parametrize('code,retryable', [('UPSTREAM_FAILED', True), ('START_NOT_FOUND', False),
                                         ('GOAL_NOT_FOUND', False), ('START_AND_GOAL_NOT_FOUND', False)])
def test_failed_business_terminal_is_completed_without_retry(tmp_path, code, retryable):
    job_dir = _job(tmp_path)
    payload = _failed_payload(code=code)
    payload['error']['retryable'] = retryable
    (job_dir / 'result.json').write_text(json.dumps(payload))
    attempts = []
    store = FakeStore(tmp_path)
    assert level_worker.process_job(store, job_dir.name,
                                    runner=lambda path, timeout: attempts.append(path) or 'done') == 'failed'
    assert len(attempts) == 1
    assert store.calls[-1] == (job_dir.name, 'failed', payload)


@pytest.mark.parametrize('contents', ['{"status":', '{"status":"mystery"}'])
def test_invalid_result_retries_once_then_processing_crashed(tmp_path, contents):
    job_dir = _job(tmp_path)
    (job_dir / 'result.json').write_text(contents)
    attempts = []
    store = FakeStore(tmp_path)

    assert level_worker.process_job(store, job_dir.name,
                                    runner=lambda path, timeout: attempts.append(1) or 'done') == 'failed'
    assert len(attempts) == 2
    payload = store.calls[-1][2]
    assert payload['error']['code'] == 'PROCESSING_CRASHED'
    assert payload['error']['retryable'] is True
    assert payload['error']['requestId'].startswith('req_')


def test_timeout_retries_once_then_processing_timeout(tmp_path):
    job_dir = _job(tmp_path)
    store = FakeStore(tmp_path)
    attempts = []
    assert level_worker.process_job(store, job_dir.name,
                                    runner=lambda path, timeout: attempts.append(1) or 'timeout') == 'failed'
    assert len(attempts) == 2
    assert store.calls[-1][2]['error']['code'] == 'PROCESSING_TIMEOUT'


@pytest.mark.parametrize('outcome,code', [
    ('crash', 'PROCESSING_CRASHED'),
    ('timeout', 'PROCESSING_TIMEOUT'),
])
def test_infrastructure_failure_is_validated_before_store(tmp_path, monkeypatch, outcome, code):
    job_dir = _job(tmp_path)
    store = FakeStore(tmp_path)
    events = []
    original_validate = level_worker.LevelFailed.model_validate
    original_set_status = store.set_status

    def validate(payload):
        events.append(('validate', payload['error']['code']))
        return original_validate(payload)

    def set_status(job_id, status, result=None):
        if status == 'failed':
            events.append(('failed-store', result['error']['code']))
        return original_set_status(job_id, status, result)

    monkeypatch.setattr(level_worker.LevelFailed, 'model_validate', validate)
    monkeypatch.setattr(store, 'set_status', set_status)
    assert level_worker.process_job(store, job_dir.name,
                                    runner=lambda path, timeout: outcome) == 'failed'
    assert events == [('validate', code), ('failed-store', code)]


def test_process_job_sets_processing_before_runner(tmp_path):
    job_dir = _job(tmp_path)
    payload = _failed_payload()
    (job_dir / 'result.json').write_text(json.dumps(payload))
    store = FakeStore(tmp_path)

    level_worker.process_job(store, job_dir.name, runner=lambda path, timeout: 'done')
    assert store.calls[0] == (job_dir.name, 'processing', None)


def test_run_subprocess_uses_level_runner_absolute_job_dir_and_default_timeout(tmp_path, monkeypatch):
    job_dir = _job(tmp_path)
    captured = {}

    def run(command, timeout, cwd):
        captured.update(command=command, timeout=timeout, cwd=cwd)
        (job_dir / 'result.json').write_text('{}')
        return type('Process', (), {'returncode': 0})()

    monkeypatch.setattr(subprocess, 'run', run)
    assert level_worker._run_subprocess(job_dir, level_worker.SUBPROCESS_TIMEOUT) == 'done'
    assert captured['command'] == [sys.executable, '-m', 'app.workers.level_runner', str(job_dir.resolve())]
    assert captured['timeout'] == 90


def test_run_subprocess_reports_timeout(tmp_path, monkeypatch):
    job_dir = _job(tmp_path)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('level', 90)

    monkeypatch.setattr(subprocess, 'run', timeout)
    assert level_worker._run_subprocess(job_dir, 90) == 'timeout'


def test_main_constructs_parameterized_level_store(monkeypatch, tmp_path):
    captured = {}

    class Store:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.r = type('Redis', (), {'connection_pool': type('Pool', (), {'connection_kwargs': {}})()})()

        def dequeue(self, timeout=5):
            level_worker._SHUTDOWN = True
            return None

    monkeypatch.setattr(level_worker, 'JobStore', Store)
    monkeypatch.setenv('OUT_ROOT', str(tmp_path))
    monkeypatch.setattr(level_worker.signal, 'signal', lambda *args: None)
    level_worker.main()
    assert captured['queue_key'] == 'pq:levels'
    assert captured['terminal_states'] == frozenset({'ready', 'needs_fix', 'needs_review', 'failed'})
    assert 'level.json' in captured['rerun_artifacts']
