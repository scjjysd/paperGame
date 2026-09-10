"""关卡解析子进程入口：回写进度并原子发布业务终态快照。"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

from app.api.levels import LEVEL_RERUN_ARTIFACTS, LEVEL_TERMINAL_STATES
from app.level_contracts import LevelFailed, LevelNeedsFix, LevelNeedsReview, LevelReady
from app.services.job_store import JobStore
from app.services.level_parser import parse

SERVER_DIR = Path(__file__).resolve().parents[2]
TERMINAL_MODELS = {
    'ready': LevelReady, 'needs_fix': LevelNeedsFix,
    'needs_review': LevelNeedsReview, 'failed': LevelFailed,
}


def _level_store(jobs_root: Path) -> JobStore:
    return JobStore(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), jobs_root,
                    queue_key='pq:levels', terminal_states=LEVEL_TERMINAL_STATES,
                    rerun_artifacts=LEVEL_RERUN_ARTIFACTS)


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def run(job_dir: Path, store=None) -> int:
    """执行解析；业务终态返回 0，未处理的基础设施异常返回 2。"""
    job_dir = Path(job_dir).resolve()
    store = store or _level_store(job_dir.parent)
    try:
        payload = parse(job_dir, progress=lambda stage: store.set_progress(job_dir.name, stage))
        payload['updatedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        TERMINAL_MODELS[payload['status']].model_validate(payload)
        _atomic_json(job_dir / 'result.json', payload)
    except Exception:
        traceback.print_exc()
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(run(Path(sys.argv[1])))
