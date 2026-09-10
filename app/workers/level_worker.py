"""关卡 worker 主循环：独立队列、子进程重试及终态契约校验。"""
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Type

from pydantic import ValidationError

from app.api.levels import LEVEL_RERUN_ARTIFACTS, LEVEL_TERMINAL_STATES
from app.level_contracts import LevelFailed, LevelNeedsFix, LevelNeedsReview, LevelReady
from app.services.job_store import JobStore

SERVER_DIR = Path(__file__).resolve().parents[2]
SUBPROCESS_TIMEOUT = 90  # 关卡解析规格锁定；超时后重试一次，再发布技术失败。
MAX_ATTEMPTS = 2
_SHUTDOWN = False
TERMINAL_MODELS: Dict[str, Type] = {
    'ready': LevelReady,
    'needs_fix': LevelNeedsFix,
    'needs_review': LevelNeedsReview,
    'failed': LevelFailed,
}


def _run_subprocess(job_dir: Path, timeout: int) -> str:
    """返回 done、crash 或 timeout。"""
    job_dir = Path(job_dir).resolve()
    try:
        process = subprocess.run(
            [sys.executable, '-m', 'app.workers.level_runner', str(job_dir)],
            timeout=timeout, cwd=str(SERVER_DIR))
    except subprocess.TimeoutExpired:
        return 'timeout'
    if process.returncode == 0 and (job_dir / 'result.json').exists():
        return 'done'
    return 'crash'


def _load_terminal(path: Path):
    payload = json.loads(path.read_text(encoding='utf-8'))
    model = TERMINAL_MODELS.get(payload.get('status'))
    if model is None:
        raise ValueError('unknown terminal status')
    model.model_validate(payload)
    return payload


def _failure(job_id: str, code: str) -> dict:
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    message = '关卡解析超时。' if code == 'PROCESSING_TIMEOUT' else '关卡解析进程异常退出。'
    return {
        'jobId': job_id,
        'status': 'failed',
        'error': {
            'code': code,
            'message': message,
            'retryable': True,
            'requestId': 'req_' + uuid.uuid4().hex[:24],
        },
        'createdAt': now,
        'updatedAt': now,
    }


def process_job(store, job_id: str, runner: Callable = None,
                timeout: int = SUBPROCESS_TIMEOUT) -> str:
    runner = runner or _run_subprocess
    store.set_status(job_id, 'processing')
    job_dir = (store.jobs_root / job_id).resolve()
    outcome = 'crash'
    for _ in range(MAX_ATTEMPTS):
        outcome = runner(job_dir, timeout)
        if outcome != 'done':
            continue
        try:
            payload = _load_terminal(job_dir / 'result.json')
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError, ValidationError):
            outcome = 'crash'
            continue
        store.set_status(job_id, payload['status'], result=payload)
        return payload['status']
    code = 'PROCESSING_TIMEOUT' if outcome == 'timeout' else 'PROCESSING_CRASHED'
    payload = _failure(job_id, code)
    LevelFailed.model_validate(payload)
    store.set_status(job_id, 'failed', result=payload)
    return 'failed'


def _sigterm_handler(signum, frame):
    global _SHUTDOWN
    _SHUTDOWN = True


def main() -> None:
    global _SHUTDOWN
    _SHUTDOWN = False
    signal.signal(signal.SIGTERM, _sigterm_handler)
    out_root = Path(os.environ.get('OUT_ROOT', str(SERVER_DIR / 'out'))).resolve()
    store = JobStore(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), out_root / 'jobs',
                     queue_key='pq:levels', terminal_states=LEVEL_TERMINAL_STATES,
                     rerun_artifacts=LEVEL_RERUN_ARTIFACTS)
    print('level worker listening on {}'.format(store.r.connection_pool.connection_kwargs), flush=True)
    while not _SHUTDOWN:
        job_id = store.dequeue(timeout=5)
        if job_id is None:
            continue
        status = process_job(store, job_id)
        print('level job {} -> {}'.format(job_id, status), flush=True)
    print('level worker shutting down', flush=True)


if __name__ == '__main__':
    main()
