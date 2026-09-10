"""关卡 worker 主循环：独立队列、子进程重试及终态契约校验。"""
import json
import logging
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
from app.level_contracts import (LEVEL_ERROR_MESSAGES, LevelFailed, LevelNeedsFix,
                                 LevelNeedsReview, LevelReady)
from app.log import setup_logging
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)
SERVER_DIR = Path(__file__).resolve().parents[2]
QUEUE_KEY = 'pq:levels'
SUBPROCESS_TIMEOUT = 90  # 关卡解析规格锁定；超时后重试一次，再发布技术失败。
MAX_ATTEMPTS = 2
_SHUTDOWN = False
TERMINAL_MODELS: Dict[str, Type] = {
    'ready': LevelReady,
    'needs_fix': LevelNeedsFix,
    'needs_review': LevelNeedsReview,
    'failed': LevelFailed,
}
# 子进程结局 -> 中文说明，日志里直接看懂是超时还是崩了
OUTCOME_MESSAGES = {'done': '正常产出结果', 'crash': '子进程异常退出或结果不符合契约',
                    'timeout': '子进程执行超时'}


def _redis_target(store: JobStore) -> str:
    """只记 host/port/db：连接参数里可能带密码，不能进日志。"""
    kwargs = store.r.connection_pool.connection_kwargs
    return '{}:{}/{}'.format(kwargs.get('host'), kwargs.get('port'), kwargs.get('db'))


def _run_subprocess(job_dir: Path, timeout: int) -> str:
    """返回 done、crash 或 timeout。"""
    job_dir = Path(job_dir).resolve()
    try:
        process = subprocess.run(
            [sys.executable, '-m', 'app.workers.level_runner', str(job_dir)],
            timeout=timeout, cwd=str(SERVER_DIR))
    except subprocess.TimeoutExpired:
        logger.warning('关卡解析子进程超时（%d 秒）：%s', timeout, job_dir)
        return 'timeout'
    if process.returncode == 0 and (job_dir / 'result.json').exists():
        return 'done'
    logger.warning('关卡解析子进程未产出有效结果：退出码 %s，结果文件存在 %s（%s）',
                   process.returncode, (job_dir / 'result.json').exists(), job_dir)
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
    return {
        'jobId': job_id,
        'status': 'failed',
        'error': {
            'code': code,
            # 文案取自契约里的 LEVEL_ERROR_MESSAGES，与 API 即时报错保持同一句话
            'message': LEVEL_ERROR_MESSAGES.get(code, '关卡解析失败，请查看服务端日志。'),
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
    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info('关卡任务 %s 第 %d/%d 次解析尝试开始（超时 %d 秒）', job_id, attempt, MAX_ATTEMPTS, timeout)
        outcome = runner(job_dir, timeout)
        if outcome != 'done':
            logger.warning('关卡任务 %s 第 %d 次尝试失败：%s', job_id, attempt,
                           OUTCOME_MESSAGES.get(outcome, outcome))
            continue
        try:
            payload = _load_terminal(job_dir / 'result.json')
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
            logger.warning('关卡任务 %s 的结果不符合终态契约，按基础设施故障重试：%s', job_id, exc)
            outcome = 'crash'
            continue
        store.set_status(job_id, payload['status'], result=payload)
        logger.info('关卡任务 %s 解析完成，终态：%s', job_id, payload['status'])
        return payload['status']
    code = 'PROCESSING_TIMEOUT' if outcome == 'timeout' else 'PROCESSING_CRASHED'
    logger.error('关卡任务 %s 重试 %d 次仍失败，发布技术失败终态：%s（%s）',
                 job_id, MAX_ATTEMPTS, code, LEVEL_ERROR_MESSAGES.get(code, ''))
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
    log_dir = setup_logging('level-worker', out_root)
    store = JobStore(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), out_root / 'jobs',
                     queue_key=QUEUE_KEY, terminal_states=LEVEL_TERMINAL_STATES,
                     rerun_artifacts=LEVEL_RERUN_ARTIFACTS)
    logger.info('关卡解析 worker 已启动：队列 %s，Redis %s，产物根目录 %s，日志目录 %s',
                QUEUE_KEY, _redis_target(store), out_root, log_dir)
    while not _SHUTDOWN:
        job_id = store.dequeue(timeout=5)
        if job_id is None:
            continue
        process_job(store, job_id)
    logger.info('关卡解析 worker 收到停止信号，已退出主循环')


if __name__ == '__main__':
    main()
