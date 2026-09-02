"""worker 主循环：BRPOP 消费 -> 子进程渲染 -> 终态写回。并发 = 1（规格锁定）。"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from app.services.job_store import JobStore

SERVER_DIR = Path(__file__).resolve().parents[2]
SUBPROCESS_TIMEOUT = 120   # 尖刺实测单任务 <=40s 的 3 倍余量
MAX_ATTEMPTS = 2           # 基础设施故障重试 1 次；业务失败不重试


def _run_subprocess(job_dir: Path, timeout: int) -> str:
    """返回 'done' | 'crash' | 'timeout'。"""
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'app.workers.render_runner', str(job_dir)],
            timeout=timeout, cwd=str(SERVER_DIR))
    except subprocess.TimeoutExpired:
        return 'timeout'
    if proc.returncode == 0 and (job_dir / 'result.json').exists():
        return 'done'
    return 'crash'


def process_job(store, job_id: str, runner: Callable = None, timeout: int = SUBPROCESS_TIMEOUT) -> str:
    runner = runner or _run_subprocess
    store.set_status(job_id, 'processing')
    job_dir = store.jobs_root / job_id
    outcome = 'crash'
    for _ in range(MAX_ATTEMPTS):
        outcome = runner(job_dir, timeout)
        if outcome == 'done':
            payload = json.loads((job_dir / 'result.json').read_text())
            store.set_status(job_id, payload['status'], result=payload)
            return payload['status']
    code = 'RENDER_TIMEOUT' if outcome == 'timeout' else 'RENDER_CRASHED'
    payload = {'status': 'failed', 'code': code}
    store.set_status(job_id, 'failed', result=payload)
    return 'failed'


def main() -> None:
    out_root = Path(os.environ.get('OUT_ROOT', str(SERVER_DIR / 'out'))).resolve()
    store = JobStore(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), out_root / 'jobs')
    print(f'worker listening on {store.r.connection_pool.connection_kwargs}', flush=True)
    while True:
        job_id = store.dequeue(timeout=5)
        if job_id is None:
            continue
        status = process_job(store, job_id)
        print(f'job {job_id} -> {status}', flush=True)


if __name__ == '__main__':
    main()
