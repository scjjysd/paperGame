"""FastAPI 应用工厂：路由 + /artifacts 静态伺服 + /healthz。"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.character_view import router as view_router
from app.api.levels import router as levels_router, LEVEL_TERMINAL_STATES, LEVEL_RERUN_ARTIFACTS
from app.api.characters import router
from app.services.job_store import JobStore

SERVER_DIR = Path(__file__).resolve().parents[1]


def create_app() -> FastAPI:
    app = FastAPI(title='paper-game server', version='v1')
    out_root = Path(os.environ.get('OUT_ROOT', str(SERVER_DIR / 'out'))).resolve()
    jobs_root = out_root / 'jobs'
    jobs_root.mkdir(parents=True, exist_ok=True)
    app.state.out_root = out_root
    redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    app.state.store = JobStore(redis_url, jobs_root)
    app.state.level_store = JobStore(redis_url, jobs_root, queue_key='pq:levels',
                                     terminal_states=LEVEL_TERMINAL_STATES,
                                     rerun_artifacts=LEVEL_RERUN_ARTIFACTS)
    app.mount('/artifacts', StaticFiles(directory=str(jobs_root)), name='artifacts')
    app.include_router(router)
    app.include_router(levels_router)
    app.include_router(view_router)   # 审查端点：/detail + /view

    @app.get('/healthz')
    def healthz():
        return {'status': 'ok'}

    return app


app = create_app()
