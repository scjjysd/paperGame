"""日志基础设施：中文格式、落盘 out/logs、按日期拆分、幂等重装。"""
import logging
import re
import time

import pytest

from app import log as log_setup


def _today_file(log_dir, component, day=None):
    return log_dir / '{}-{}.log'.format(component, day or time.strftime('%Y-%m-%d'))


@pytest.fixture
def logger(tmp_path):
    """独立 logger（不向 root 传播），断言完自动清干净，避免污染其他用例。"""
    instance = logging.getLogger('pg-log-test')
    instance.handlers.clear()
    instance.propagate = False
    instance.setLevel(logging.INFO)
    yield instance
    for handler in list(instance.handlers):
        handler.close()
    instance.handlers.clear()


def test_logs_land_in_out_logs_with_date_suffix(tmp_path, logger):
    log_dir = log_setup.setup_logging('api', tmp_path, console=False)
    handler = log_setup.DailyFileHandler(log_dir, 'api')
    handler.setFormatter(log_setup.ChineseFormatter(log_setup.LOG_FORMAT, datefmt=log_setup.LOG_DATEFMT))
    logger.addHandler(handler)

    logger.info('关卡任务 level_x 解析完成，终态：ready')

    assert log_dir == (tmp_path / 'logs').resolve()
    content = _today_file(log_dir, 'api').read_text(encoding='utf-8')
    assert '关卡任务 level_x 解析完成，终态：ready' in content
    assert '[INFO]' in content          # 中文格式仍保留级别，便于 grep
    assert 'pg-log-test' in content     # 带 logger 名，能定位到模块
    assert content.startswith(time.strftime('%Y-%m-%d'))   # 行首是日期，按时间对得上


def test_default_log_dir_follows_out_root_env(tmp_path, monkeypatch):
    monkeypatch.setenv('OUT_ROOT', str(tmp_path))
    monkeypatch.delenv('LOG_DIR', raising=False)
    assert log_setup.resolve_log_dir() == (tmp_path / 'logs').resolve()


def test_log_dir_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv('LOG_DIR', str(tmp_path / 'custom'))
    assert log_setup.resolve_log_dir(tmp_path) == (tmp_path / 'custom').resolve()


def test_handler_switches_file_when_date_changes(tmp_path, logger, monkeypatch):
    """跨天必须切到新文件：排查历史问题时按日期就能定位。"""
    handler = log_setup.DailyFileHandler(tmp_path, 'api')
    logger.addHandler(handler)
    logger.info('第一天的日志')

    monkeypatch.setattr(log_setup, '_today', lambda: '2026-09-11')
    logger.info('第二天的日志')

    first = _today_file(tmp_path, 'api', time.strftime('%Y-%m-%d')).read_text(encoding='utf-8')
    second = _today_file(tmp_path, 'api', '2026-09-11').read_text(encoding='utf-8')
    assert '第一天的日志' in first and '第二天的日志' not in first
    assert '第二天的日志' in second and '第一天的日志' not in second


def test_keep_days_cleanup_only_touches_own_logs(tmp_path, logger, monkeypatch):
    handler = log_setup.DailyFileHandler(tmp_path, 'api', keep_days=1)
    logger.addHandler(handler)
    stale = tmp_path / 'api-2020-01-01.log'
    stale.write_text('旧日志', encoding='utf-8')
    other = tmp_path / 'level-worker-2020-01-01.log'
    other.write_text('别的组件', encoding='utf-8')
    unrelated = tmp_path / 'api-not-a-date.log'
    unrelated.write_text('命名不规范', encoding='utf-8')

    monkeypatch.setattr(log_setup, '_today', lambda: '2026-09-11')
    logger.info('触发换日清理')

    assert not stale.exists()            # 过期且属于本组件 → 清理
    assert other.exists()                # 别的组件的日志不能动
    assert unrelated.exists()            # 命名不符合日期规范的不删


def test_keep_days_zero_retains_everything(tmp_path, logger, monkeypatch):
    handler = log_setup.DailyFileHandler(tmp_path, 'api')   # keep_days 默认 0
    logger.addHandler(handler)
    stale = tmp_path / 'api-2020-01-01.log'
    stale.write_text('旧日志', encoding='utf-8')

    monkeypatch.setattr(log_setup, '_today', lambda: '2026-09-11')
    logger.info('触发换日')

    assert stale.exists()


def test_setup_logging_is_idempotent(tmp_path):
    """create_app 会被重复调用（含 import 时那次），日志不能重复写。"""
    log_setup.setup_logging('api', tmp_path, console=False)
    log_setup.setup_logging('api', tmp_path, console=False)
    logging.getLogger('pg-idempotent').info('只应出现一次')

    content = _today_file(tmp_path / 'logs', 'api').read_text(encoding='utf-8')
    assert content.count('只应出现一次') == 1


def test_uvicorn_access_log_is_rewritten_in_chinese(tmp_path):
    log_setup.setup_logging('api', tmp_path, console=False)
    # 参数形状必须与 uvicorn 真实发送的一致：客户端、方法、路径与查询串、HTTP 版本、状态码
    record = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
                               '%s - "%s %s HTTP/%s" %d',
                               ('127.0.0.1:52344', 'GET', '/v1/levels/abc/view?x=1', '1.1', 200), None)

    logging.getLogger('uvicorn.access').handle(record)

    content = _today_file(tmp_path / 'logs', 'api').read_text(encoding='utf-8')
    assert '访问日志：127.0.0.1:52344 GET /v1/levels/abc/view?x=1（HTTP/1.1）-> 200' in content
    assert 'HTTP/1.1" 200' not in content      # 英文原文不再落盘
    assert ' - ' not in content                # 不留英文分隔符


def test_unexpected_access_log_shape_keeps_original(tmp_path):
    """uvicorn 改版导致参数形状不同时，保留英文原文而不是写出一行破折号。"""
    log_setup.setup_logging('api', tmp_path, console=False)
    record = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
                               '%s - "%s" %d', ('127.0.0.1', 'GET /healthz', 200), None)

    logging.getLogger('uvicorn.access').handle(record)

    content = _today_file(tmp_path / 'logs', 'api').read_text(encoding='utf-8')
    assert '127.0.0.1 - "GET /healthz" 200' in content
    assert '访问日志' not in content


def test_uvicorn_lifecycle_logs_are_translated(tmp_path):
    """启动与关闭的框架日志也要中文，否则 docker logs 里仍是英文一片。"""
    log_setup.setup_logging('api', tmp_path, console=False)
    logger = logging.getLogger('uvicorn.error')
    logger.info('Started server process [%d]', 71642)
    logger.info('Waiting for application startup.')
    logger.info('Application startup complete.')
    logger.info('Uvicorn running on %s://%s:%d (Press CTRL+C to quit)', 'http', '127.0.0.1', 8123)
    logger.info('Shutting down')

    content = _today_file(tmp_path / 'logs', 'api').read_text(encoding='utf-8')
    assert '服务进程已启动，PID 71642' in content
    assert '等待应用启动' in content
    assert '应用启动完成' in content
    assert '服务已监听 http://127.0.0.1:8123（按 CTRL+C 退出）' in content
    assert '正在关闭服务' in content
    assert 'Started server process' not in content   # 英文原文不再落盘


def test_unknown_uvicorn_message_keeps_english(tmp_path):
    """对照表外的框架日志原样输出：宁可英文，不可错译误导排查。"""
    log_setup.setup_logging('api', tmp_path, console=False)
    logging.getLogger('uvicorn.error').warning('Some brand new uvicorn notice %s', 'x')

    content = _today_file(tmp_path / 'logs', 'api').read_text(encoding='utf-8')
    assert 'Some brand new uvicorn notice x' in content


# %-风格的占位符（排除 %% 转义），用来卡住中英模板的参数个数一致
_PLACEHOLDER = re.compile(r'%(?!%)[-+ #0]*[\d*]*(?:\.[\d*]+)?[hlL]?[diouxXeEfFgGcrsa]')


@pytest.mark.parametrize('english', sorted(log_setup.UVICORN_MESSAGES))
def test_uvicorn_message_table_placeholders_match(english):
    """中文模板只换文案不换 args，占位符个数对不上会在格式化时直接报错。"""
    chinese = log_setup.UVICORN_MESSAGES[english]
    specs = _PLACEHOLDER.findall(english)
    assert len(_PLACEHOLDER.findall(chinese)) == len(specs), english

    probe = tuple(1 if spec.endswith('d') else 'x' for spec in specs)
    chinese % probe      # 不抛 TypeError / 不缺参即可


def test_unwritable_log_dir_degrades_to_console(tmp_path, monkeypatch):
    """日志目录不可写只能降级，不能因为写不了日志就把服务打死。"""
    blocked = tmp_path / 'blocked'
    blocked.write_text('我不是目录', encoding='utf-8')
    monkeypatch.setenv('LOG_CONSOLE', '0')
    log_setup.setup_logging('api', blocked, console=False)
    logging.getLogger('pg-degraded').info('服务仍在运行')
