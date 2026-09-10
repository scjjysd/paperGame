"""任务状态存储：Redis Hash（TTL 24h）+ 队列 List；result.json 为 Redis 过期后的兜底。"""
import json
import shutil
import time
from pathlib import Path
from typing import Optional

import redis

QUEUE_KEY = 'pq:characters'
JOB_KEY = 'job:{}'
TTL_SECONDS = 24 * 3600
TERMINAL_STATES = frozenset({'ready', 'needs_correction', 'failed'})
# force 重跑时需清掉的旧产物（input.png 由 API 覆写，不在清理之列）
RERUN_ARTIFACTS = ('result.json', 'run.png', 'run.gif', 'jump.png', 'jump.gif', 'anno')


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class JobStore:
    def __init__(self, redis_url: str, jobs_root, client=None,
                 queue_key: str = QUEUE_KEY,
                 terminal_states=frozenset(TERMINAL_STATES),
                 rerun_artifacts=RERUN_ARTIFACTS):
        self.r = client if client is not None else redis.Redis.from_url(redis_url, decode_responses=True)
        self.jobs_root = Path(jobs_root)
        self.queue_key = queue_key
        self.terminal_states = frozenset(terminal_states)
        self.rerun_artifacts = tuple(rerun_artifacts)

    # -- 队列 --
    def enqueue(self, job_id: str) -> None:
        self.r.lpush(self.queue_key, job_id)

    def dequeue(self, timeout: int = 5) -> Optional[str]:
        item = self.r.brpop(self.queue_key, timeout=timeout)
        return item[1] if item else None

    # -- 状态 --
    def create(self, job_id: str) -> None:
        key = JOB_KEY.format(job_id)
        self.r.hset(key, mapping={'status': 'queued', 'createdAt': _now(), 'updatedAt': _now()})
        self.r.expire(key, TTL_SECONDS)

    def set_status(self, job_id: str, status: str, result: Optional[dict] = None) -> None:
        key = JOB_KEY.format(job_id)
        current = self.r.hget(key, 'status')
        if current in self.terminal_states:
            return
        mapping = {'status': status, 'updatedAt': _now()}
        if result is not None:
            mapping['result'] = json.dumps(result, ensure_ascii=False)
        self.r.hset(key, mapping=mapping)
        self.r.expire(key, TTL_SECONDS)

    def set_progress(self, job_id: str, stage: str) -> None:
        key = JOB_KEY.format(job_id)
        self.r.hset(key, mapping={'stage': stage, 'updatedAt': _now()})
        self.r.expire(key, TTL_SECONDS)

    def reset(self, job_id: str) -> None:
        """force 重跑：丢弃旧终态结果与磁盘产物，状态回到 queued 并刷新 TTL。

        必须先于 enqueue 调用；状态离开终态后 worker 的 set_status 才能再次写入。
        """
        key = JOB_KEY.format(job_id)
        self.r.hdel(key, 'result', 'stage')
        self.r.hset(key, mapping={'status': 'queued', 'updatedAt': _now()})
        self.r.expire(key, TTL_SECONDS)
        job_dir = self.jobs_root / job_id
        for name in self.rerun_artifacts:
            p = job_dir / name
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        for p in job_dir.glob('*.tmp*'):
            if p.is_file():
                p.unlink()

    def get(self, job_id: str) -> Optional[dict]:
        data = self.r.hgetall(JOB_KEY.format(job_id))
        if data:
            return data
        snapshot = self.jobs_root / job_id / 'result.json'
        if snapshot.exists():
            try:
                payload = json.loads(snapshot.read_text())
                return {'status': payload['status'], 'result': json.dumps(payload, ensure_ascii=False)}
            except (json.JSONDecodeError, KeyError, OSError):
                # 损坏快照视同不存在：API 返回 404，客户端可重传（同图同 jobId 幂等重新入队）
                return None
        return None
