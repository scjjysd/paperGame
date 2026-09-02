"""FastAPI 应用工厂：路由 + /artifacts 静态伺服 + /healthz。"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.characters import router
from app.services.job_store import JobStore

SERVER_DIR = Path(__file__).resolve().parents[1]


def create_app() -> FastAPI:
    app = FastAPI(title='paper-game server', version='v1')
    out_root = Path(os.environ.get('OUT_ROOT', str(SERVER_DIR / 'out'))).resolve()
    jobs_root = out_root / 'jobs'
    jobs_root.mkdir(parents=True, exist_ok=True)
    app.state.out_root = out_root
    app.state.store = JobStore(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), jobs_root)
    app.mount('/artifacts', StaticFiles(directory=str(jobs_root)), name='artifacts')
    app.include_router(router)

    @app.get('/healthz')
    def healthz():
        return {'status': 'ok'}

    return app


app = create_app()
