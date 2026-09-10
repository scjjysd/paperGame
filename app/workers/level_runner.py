"""关卡解析子进程入口：回写进度并原子发布业务终态快照。

日志写 out/logs/level-runner-<日期>.log；进度阶段名是契约值（英文），
日志里额外给出中文说明，便于对着日志判断卡在哪一步。
"""
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

from app.api.levels import LEVEL_RERUN_ARTIFACTS, LEVEL_TERMINAL_STATES
from app.level_contracts import (LEVEL_STAGE_MESSAGES, LevelFailed, LevelNeedsFix,
                                 LevelNeedsReview, LevelReady)
from app.log import out_root_for_job_dir, setup_logging
from app.services.job_store import JobStore
from app.services.level_parser import parse

logger = logging.getLogger(__name__)
SERVER_DIR = Path(__file__).resolve().parents[2]
TERMINAL_MODELS = {
    'ready': LevelReady, 'needs_fix': LevelNeedsFix,
    'needs_review': LevelNeedsReview, 'failed': LevelFailed,
}


def _level_store(jobs_root: Path) -> JobStore:
    return JobStore(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), jobs_root,
                    queue_key='pq:levels', terminal_states=LEVEL_TERMINAL_STATES,
                    rerun_artifacts=LEVEL_RERUN_ARTIFACTS)


def _progress_reporter(store, job_id: str) -> Callable[[str], None]:
    def report(stage: str) -> None:
        store.set_progress(job_id, stage)
        logger.info('关卡任务 %s 进入阶段：%s（%s）', job_id,
                    LEVEL_STAGE_MESSAGES.get(stage, stage), stage)
    return report


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def run(job_dir: Path, store=None) -> int:
    """执行解析；业务终态返回 0，未处理的基础设施异常返回 2。"""
    job_dir = Path(job_dir).resolve()
    job_id = job_dir.name
    setup_logging('level-runner', out_root_for_job_dir(job_dir))
    store = store or _level_store(job_dir.parent)
    logger.info('关卡任务 %s 开始解析，输入 %s', job_id, job_dir / 'input.png')
    try:
        payload = parse(job_dir, progress=_progress_reporter(store, job_id))
        payload['updatedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        TERMINAL_MODELS[payload['status']].model_validate(payload)
        _atomic_json(job_dir / 'result.json', payload)
    except Exception:
        logger.exception('关卡任务 %s 解析出现未处理异常，交回主循环按基础设施故障重试', job_id)
        return 2
    logger.info('关卡任务 %s 解析结束，终态：%s', job_id, payload['status'])
    return 0


if __name__ == '__main__':
    sys.exit(run(Path(sys.argv[1])))
