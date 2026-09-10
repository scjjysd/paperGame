"""worker 主循环：BRPOP 消费 -> 子进程渲染 -> 终态写回。并发 = 1（规格锁定）。"""
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable

from app.log import setup_logging
from app.services.job_store import QUEUE_KEY, JobStore

logger = logging.getLogger(__name__)
SERVER_DIR = Path(__file__).resolve().parents[2]
SUBPROCESS_TIMEOUT = 120   # 尖刺实测单任务 <=40s 的 3 倍余量
MAX_ATTEMPTS = 2           # 基础设施故障重试 1 次；业务失败不重试
_SHUTDOWN = False
# 子进程结局 -> 中文说明，日志里直接看懂是超时还是崩了
OUTCOME_MESSAGES = {'done': '正常产出结果', 'crash': '子进程异常退出或结果文件缺失',
                    'timeout': '子进程执行超时'}


def _redis_target(store: JobStore) -> str:
    """只记 host/port/db：连接参数里可能带密码，不能进日志。"""
    kwargs = store.r.connection_pool.connection_kwargs
    return '{}:{}/{}'.format(kwargs.get('host'), kwargs.get('port'), kwargs.get('db'))


def _run_subprocess(job_dir: Path, timeout: int) -> str:
    """返回 'done' | 'crash' | 'timeout'。"""
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'app.workers.render_runner', str(job_dir)],
            timeout=timeout, cwd=str(SERVER_DIR))
    except subprocess.TimeoutExpired:
        logger.warning('角色渲染子进程超时（%d 秒）：%s', timeout, job_dir)
        return 'timeout'
    if proc.returncode == 0 and (job_dir / 'result.json').exists():
        return 'done'
    logger.warning('角色渲染子进程未产出有效结果：退出码 %s，结果文件存在 %s（%s）',
                   proc.returncode, (job_dir / 'result.json').exists(), job_dir)
    return 'crash'


def process_job(store, job_id: str, runner: Callable = None, timeout: int = SUBPROCESS_TIMEOUT) -> str:
    runner = runner or _run_subprocess
    store.set_status(job_id, 'processing')
    job_dir = store.jobs_root / job_id
    outcome = 'crash'
    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info('角色任务 %s 第 %d/%d 次渲染尝试开始（超时 %d 秒）', job_id, attempt, MAX_ATTEMPTS, timeout)
        outcome = runner(job_dir, timeout)
        if outcome == 'done':
            try:
                payload = json.loads((job_dir / 'result.json').read_text())
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                logger.warning('角色任务 %s 的结果文件不可读，按基础设施故障重试：%s', job_id, exc)
                outcome = 'crash'
                continue
            store.set_status(job_id, payload['status'], result=payload)
            logger.info('角色任务 %s 渲染完成，终态：%s', job_id, payload['status'])
            return payload['status']
        logger.warning('角色任务 %s 第 %d 次尝试失败：%s', job_id, attempt,
                       OUTCOME_MESSAGES.get(outcome, outcome))
    code = 'RENDER_TIMEOUT' if outcome == 'timeout' else 'RENDER_CRASHED'
    logger.error('角色任务 %s 重试 %d 次仍失败，发布技术失败终态：%s', job_id, MAX_ATTEMPTS, code)
    payload = {'status': 'failed', 'code': code}
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
    log_dir = setup_logging('character-worker', out_root)
    store = JobStore(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), out_root / 'jobs')
    logger.info('角色渲染 worker 已启动：队列 %s，Redis %s，产物根目录 %s，日志目录 %s',
                QUEUE_KEY, _redis_target(store), out_root, log_dir)
    while not _SHUTDOWN:
        job_id = store.dequeue(timeout=5)
        if job_id is None:
            continue
        process_job(store, job_id)
    logger.info('角色渲染 worker 收到停止信号，已退出主循环')


if __name__ == '__main__':
    main()
