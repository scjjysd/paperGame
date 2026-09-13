"""FastAPI 应用工厂：跨域 + 中文日志 + 路由 + /artifacts、/webgl 静态伺服 + /healthz。"""
import logging
import os
import time
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import errors
from app.api.character_view import router as character_view_router
from app.api.characters import router as characters_router
from app.api.level_view import router as level_view_router
from app.api.levels import LEVEL_RERUN_ARTIFACTS, LEVEL_TERMINAL_STATES, router as levels_router
from app.api.webgl import WebGLStaticFiles
from app.log import setup_logging
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)
SERVER_DIR = Path(__file__).resolve().parents[1]
# 健康探针每 10s 一次、产物下载频繁，按 INFO 记会淹没业务日志，降到 DEBUG
QUIET_PATH_PREFIXES = ('/healthz', '/artifacts/', '/webgl/')
# HTTP 状态码 -> 框架级中文错误码
FRAMEWORK_CODES: Dict[int, str] = {
    400: errors.INVALID_REQUEST,
    404: errors.NOT_FOUND,
    405: errors.METHOD_NOT_ALLOWED,
    413: 'PAYLOAD_TOO_LARGE',
    422: errors.INVALID_REQUEST,
    503: 'SERVICE_UNAVAILABLE',
}


def _split_env(name: str, default: str) -> List[str]:
    values = [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]
    return values or [default]


def cors_options() -> Dict[str, object]:
    """跨域配置全部走环境变量，默认放开所有来源（本地与内网联调用）。

    注意：`allow_origins=['*']` 时浏览器规范禁止携带凭证，故 credentials 自动关闭；
    要带 Cookie/Authorization 就必须把 CORS_ALLOW_ORIGINS 配成具体来源。
    """
    origins = _split_env('CORS_ALLOW_ORIGINS', '*')
    credentials = os.environ.get('CORS_ALLOW_CREDENTIALS', '').strip().lower() in ('1', 'true', 'yes', 'on')
    if credentials and '*' in origins:
        logger.warning('跨域配置冲突：来源为 * 时浏览器不允许携带凭证，已自动关闭 allow_credentials')
        credentials = False
    try:
        max_age = int(os.environ.get('CORS_MAX_AGE', '600'))
    except ValueError:
        max_age = 600
    return {
        'allow_origins': origins,
        'allow_origin_regex': os.environ.get('CORS_ALLOW_ORIGIN_REGEX') or None,
        'allow_methods': _split_env('CORS_ALLOW_METHODS', '*'),
        'allow_headers': _split_env('CORS_ALLOW_HEADERS', '*'),
        'expose_headers': _split_env('CORS_EXPOSE_HEADERS', 'Content-Length,Content-Type'),
        'allow_credentials': credentials,
        'max_age': max_age,
    }


def create_app() -> FastAPI:
    app = FastAPI(title='paper-game server', version='v1')
    out_root = Path(os.environ.get('OUT_ROOT', str(SERVER_DIR / 'out'))).resolve()
    log_dir = setup_logging('api', out_root)
    jobs_root = out_root / 'jobs'
    jobs_root.mkdir(parents=True, exist_ok=True)
    app.state.out_root = out_root
    app.state.log_dir = log_dir
    redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    app.state.store = JobStore(redis_url, jobs_root)
    app.state.level_store = JobStore(redis_url, jobs_root, queue_key='pq:levels',
                                     terminal_states=LEVEL_TERMINAL_STATES,
                                     rerun_artifacts=LEVEL_RERUN_ARTIFACTS)

    @app.middleware('http')
    async def log_request(request: Request, call_next):
        """每个请求两条中文日志（进出各一条），异常时带堆栈，便于按时间点对照排查。"""
        started = time.perf_counter()
        emit = logger.debug if request.url.path.startswith(QUIET_PATH_PREFIXES) else logger.info
        client = request.client.host if request.client else '-'
        emit('收到请求：%s %s（来源 %s）', request.method, request.url.path, client)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception('请求处理失败：%s %s，耗时 %.1f 毫秒', request.method, request.url.path,
                             (time.perf_counter() - started) * 1000)
            raise
        emit('请求完成：%s %s -> %d，耗时 %.1f 毫秒', request.method, request.url.path,
             response.status_code, (time.perf_counter() - started) * 1000)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException):
        """框架级 HTTP 错误也回中文：默认的 {"detail": "Not Found"} 对排查毫无帮助。"""
        code = FRAMEWORK_CODES.get(exc.status_code, 'HTTP_{}'.format(exc.status_code))
        return errors.framework_error(exc.status_code, code, headers=getattr(exc, 'headers', None))

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        return errors.framework_error(422, errors.INVALID_REQUEST,
                                      message='请求参数校验失败，请检查字段名、类型与取值。',
                                      details={'errors': jsonable_encoder(exc.errors())})

    @app.exception_handler(Exception)
    async def on_unhandled_exception(request: Request, exc: Exception):
        logger.exception('未处理异常：%s %s', request.method, request.url.path)
        return errors.framework_error(500, errors.INTERNAL)

    app.mount('/artifacts', StaticFiles(directory=str(jobs_root)), name='artifacts')
    webgl_root = SERVER_DIR / 'webgl'
    if webgl_root.is_dir():
        app.mount('/webgl', WebGLStaticFiles(directory=str(webgl_root), html=True), name='webgl')
    else:
        # worker 共用服务端镜像，但不挂载 WebGL；缺少构建产物不影响 API 启动。
        logger.warning('WebGL 目录不存在，未启用 /webgl/：%s', webgl_root)
    app.include_router(characters_router)
    app.include_router(levels_router)
    app.include_router(character_view_router)   # 审查端点：/detail + /view
    app.include_router(level_view_router)       # 关卡审查端点：/detail + /view

    @app.get('/healthz')
    def healthz():
        return {'status': 'ok'}

    # CORS 最后注册 = 位于中间件栈最外层：预检 OPTIONS 不必进业务逻辑就能被应答
    options = cors_options()
    app.add_middleware(CORSMiddleware, **options)
    logger.info('服务端初始化完成：产物根目录 %s，日志目录 %s，Redis %s', out_root, log_dir, redis_url)
    logger.info('跨域配置：允许来源 %s，方法 %s，请求头 %s，携带凭证 %s，预检缓存 %s 秒',
                options['allow_origins'], options['allow_methods'], options['allow_headers'],
                options['allow_credentials'], options['max_age'])
    return app


app = create_app()
