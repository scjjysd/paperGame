"""日志基础设施：中文格式 + 落盘 out/logs，按日期拆分。

api / worker / runner 子进程共用同一套配置，但各写各的文件（组件名即文件前缀），
避免多进程争抢同一个句柄：

    out/logs/api-2026-09-10.log
    out/logs/character-worker-2026-09-10.log
    out/logs/level-runner-2026-09-10.log

约定：
- 目录由 ``LOG_DIR`` 指定，未设置时为 ``OUT_ROOT/logs``（容器内 ``/data/out/logs``，
  已由 compose 的 ``./out:/data/out`` 挂到宿主机 ``out/logs``）；
- 文件名带本地日期，跨天自动切到新文件，不依赖进程重启；
- 同时保留控制台输出，``docker logs`` 与文件内容一致；
- 级别由 ``LOG_LEVEL`` 控制，默认 INFO；
- ``LOG_KEEP_DAYS`` > 0 时才清理过期日志，默认 0 = 永久保留。
"""
import copy
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

SERVER_DIR = Path(__file__).resolve().parents[1]
DATE_FORMAT = '%Y-%m-%d'
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'
DEFAULT_LEVEL = 'INFO'
ACCESS_LOGGER = 'uvicorn.access'
# uvicorn 的 logger 自带英文 handler 且不向 root 传播，必须接管后才能中文化并落盘
HIJACKED_LOGGERS = ('uvicorn', 'uvicorn.error', ACCESS_LOGGER)

# uvicorn 生命周期日志的中文对照，键即 record.msg 模板（逐条比对 uvicorn 0.39 源码得出）。
# 表里没有的一律保留英文原文：框架的错误分支常伴随堆栈，误译反而误导排查。
UVICORN_MESSAGES = {
    'Started server process [%d]': '服务进程已启动，PID %d',
    'Finished server process [%d]': '服务进程已退出，PID %d',
    'Shutting down': '正在关闭服务',
    'Waiting for application startup.': '等待应用启动',
    'Application startup complete.': '应用启动完成',
    'Waiting for application shutdown.': '等待应用关闭',
    'Application shutdown complete.': '应用关闭完成',
    'Application startup failed. Exiting.': '应用启动失败，正在退出',
    'Application shutdown failed. Exiting.': '应用关闭失败，正在退出',
    "ASGI 'lifespan' protocol appears unsupported.": '应用未实现 ASGI lifespan 协议，已跳过启动钩子',
    'Uvicorn running on %s://%s:%d (Press CTRL+C to quit)': '服务已监听 %s://%s:%d（按 CTRL+C 退出）',
    'Uvicorn running on %s://[%s]:%d (Press CTRL+C to quit)': '服务已监听 %s://[%s]:%d（按 CTRL+C 退出）',
    'Uvicorn running on socket %s (Press CTRL+C to quit)': '服务已监听套接字 %s（按 CTRL+C 退出）',
    'Uvicorn running on unix socket %s (Press CTRL+C to quit)': '服务已监听 Unix 套接字 %s（按 CTRL+C 退出）',
    'Will watch for changes in these directories: %s': '将监听以下目录的代码变更：%s',
    "Loading environment from '%s'": '正在从 %s 加载环境变量',
}

# 本模块装过的 handler，重复调用 setup_logging 时先摘掉，保证幂等且不重复写
_INSTALLED: List[logging.Handler] = []


def _today() -> str:
    """本地日期（模块级函数而非静态方法：单测换日时只需 patch 这一个入口）。"""
    return time.strftime(DATE_FORMAT)


class ChineseFormatter(logging.Formatter):
    """统一中文格式；uvicorn 的访问日志与生命周期日志额外改写成中文。"""

    def format(self, record: logging.LogRecord) -> str:
        if record.name == ACCESS_LOGGER:
            record = _rewrite_access(record)
        elif record.name in HIJACKED_LOGGERS:
            record = _rewrite_uvicorn(record)
        return super().format(record)


def _rewrite_uvicorn(record: logging.LogRecord) -> logging.LogRecord:
    """uvicorn 生命周期日志中文化；对照表里没有的保留英文原文。

    只换 msg 模板、不动 args，占位符个数因此必须与原文一致。
    """
    if not isinstance(record.msg, str):
        return record
    template = UVICORN_MESSAGES.get(record.msg)
    if template is None:
        return record
    rewritten = copy.copy(record)
    rewritten.msg = template
    return rewritten


def _rewrite_access(record: logging.LogRecord) -> logging.LogRecord:
    """把 uvicorn 的英文访问日志改写成中文一行。

    uvicorn 不提供额外字段，真实数据在 record.args 的 5 元组里：
    (客户端地址, 方法, 路径与查询串, HTTP 版本, 状态码)，h11 与 httptools 两种实现同形。
    复制后再改写：原 record 可能还被别的 handler 持有，不能就地改。
    """
    args = record.args if isinstance(record.args, tuple) else ()
    if len(args) != 5:
        # 形状对不上（uvicorn 改版）时保留英文原文：宁可英文，不能丢日志
        return record
    client, method, path, http_version, status = args
    rewritten = copy.copy(record)
    rewritten.msg = '访问日志：%s %s %s（HTTP/%s）-> %s'
    rewritten.args = (client, method, path, http_version, status)
    return rewritten


class DailyFileHandler(logging.FileHandler):
    """按本地日期写 ``{prefix}-{YYYY-MM-DD}.log``，跨天自动换新文件。

    不用 TimedRotatingFileHandler：它的当天文件永远叫 base 名，日期只出现在归档名上，
    排查问题时无法按日期直接定位当天日志。
    """

    def __init__(self, log_dir, prefix: str, encoding: str = 'utf-8', keep_days: int = 0):
        self.log_dir = Path(log_dir)
        self.prefix = prefix
        self.keep_days = max(0, int(keep_days))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.day = _today()
        super().__init__(str(self._path(self.day)), mode='a', encoding=encoding)

    def _path(self, day: str) -> Path:
        return self.log_dir / '{}-{}.log'.format(self.prefix, day)

    def emit(self, record: logging.LogRecord) -> None:
        today = _today()
        if today != self.day:
            self._switch_to(today)
        super().emit(record)

    def _switch_to(self, today: str) -> None:
        """换日：关掉旧句柄并把 baseFilename 指向新文件，下一次 emit 惰性重开。

        不调用 self.close()：那会触发 Handler.close 的注销逻辑，而本 handler 仍在服务。
        """
        if self.stream:
            try:
                self.flush()
            finally:
                self.stream.close()
                self.stream = None
        self.day = today
        self.baseFilename = os.path.abspath(str(self._path(today)))
        self._cleanup()

    def _cleanup(self) -> None:
        """仅在显式配置保留天数时清理，且只删自己命名规范的日志文件。"""
        if not self.keep_days:
            return
        deadline = datetime.now() - timedelta(days=self.keep_days)
        for path in self.log_dir.glob('{}-*.log'.format(self.prefix)):
            try:
                day = datetime.strptime(path.name[len(self.prefix) + 1:-len('.log')], DATE_FORMAT)
            except ValueError:
                continue
            if day < deadline:
                try:
                    path.unlink()
                except OSError:
                    pass


def resolve_out_root(out_root=None) -> Path:
    return Path(out_root or os.environ.get('OUT_ROOT') or SERVER_DIR / 'out').resolve()


def resolve_log_dir(out_root=None) -> Path:
    configured = os.environ.get('LOG_DIR', '').strip()
    return Path(configured).resolve() if configured else resolve_out_root(out_root) / 'logs'


def out_root_for_job_dir(job_dir) -> Path:
    """runner 子进程只知道 job_dir：按标准布局 OUT_ROOT/jobs/{jobId} 反推 OUT_ROOT。

    布局不匹配时（单测直接传 tmp 目录）退而用其父目录，日志不会跑到仓库里。
    """
    job_dir = Path(job_dir)
    if job_dir.parent.name == 'jobs' and len(job_dir.parents) >= 2:
        return job_dir.parents[1]
    return job_dir.parent


def resolve_level(level: Optional[str] = None) -> int:
    name = (level or os.environ.get('LOG_LEVEL') or DEFAULT_LEVEL).strip().upper()
    value = getattr(logging, name, None)
    return value if isinstance(value, int) else logging.INFO


def resolve_console(console: Optional[bool] = None) -> bool:
    """控制台输出开关：默认开（docker logs 与文件一致），LOG_CONSOLE=0 可关掉。"""
    if console is not None:
        return console
    return os.environ.get('LOG_CONSOLE', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def _detach(handler: logging.Handler) -> None:
    loggers = [logging.getLogger()] + [logging.getLogger(name) for name in HIJACKED_LOGGERS]
    for logger in loggers:
        if handler in logger.handlers:
            logger.removeHandler(handler)


def setup_logging(component: str = 'api', out_root=None, level: Optional[str] = None,
                  console: Optional[bool] = None) -> Path:
    """装载日志配置，返回日志目录。重复调用幂等。"""
    log_dir = resolve_log_dir(out_root)
    resolved_level = resolve_level(level)
    formatter = ChineseFormatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    for handler in list(_INSTALLED):
        _detach(handler)
        handler.close()
    _INSTALLED.clear()

    handlers: List[logging.Handler] = []
    try:
        handlers.append(DailyFileHandler(log_dir, component,
                                         keep_days=os.environ.get('LOG_KEEP_DAYS', '0')))
    except OSError:
        # 日志目录不可写时降级为纯控制台：日志故障不能拖垮服务
        print('日志目录 {} 不可写，本次仅输出到控制台'.format(log_dir), flush=True)
    if resolve_console(console):
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.setLevel(resolved_level)
        logging.getLogger().addHandler(handler)
        _INSTALLED.append(handler)

    root = logging.getLogger()
    root.setLevel(resolved_level)
    for name in HIJACKED_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(resolved_level)
    return log_dir
