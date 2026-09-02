"""任务状态存储：Redis Hash（TTL 24h）+ 队列 List；result.json 为 Redis 过期后的兜底。"""
import json
import time
from pathlib import Path
from typing import Optional

import redis

QUEUE_KEY = 'pq:characters'
JOB_KEY = 'job:{}'
TTL_SECONDS = 24 * 3600


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())


class JobStore:
    def __init__(self, redis_url: str, jobs_root, client=None):
        self.r = client if client is not None else redis.Redis.from_url(redis_url, decode_responses=True)
        self.jobs_root = Path(jobs_root)

    # -- 队列 --
    def enqueue(self, job_id: str) -> None:
        self.r.lpush(QUEUE_KEY, job_id)

    def dequeue(self, timeout: int = 5) -> Optional[str]:
        item = self.r.brpop(QUEUE_KEY, timeout=timeout)
        return item[1] if item else None

    # -- 状态 --
    def create(self, job_id: str) -> None:
        key = JOB_KEY.format(job_id)
        self.r.hset(key, mapping={'status': 'queued', 'updatedAt': _now()})
        self.r.expire(key, TTL_SECONDS)

    def set_status(self, job_id: str, status: str, result: Optional[dict] = None) -> None:
        key = JOB_KEY.format(job_id)
        mapping = {'status': status, 'updatedAt': _now()}
        if result is not None:
            mapping['result'] = json.dumps(result, ensure_ascii=False)
        self.r.hset(key, mapping=mapping)
        self.r.expire(key, TTL_SECONDS)

    def get(self, job_id: str) -> Optional[dict]:
        data = self.r.hgetall(JOB_KEY.format(job_id))
        if data:
            return data
        snapshot = self.jobs_root / job_id / 'result.json'
        if snapshot.exists():
            payload = json.loads(snapshot.read_text())
            return {'status': payload['status'], 'result': json.dumps(payload, ensure_ascii=False)}
        return None
