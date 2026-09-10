"""框架级错误的中文响应体：路径不存在、方法不允许、参数校验失败、未捕获异常。

业务错误（JOB_NOT_FOUND / FILE_TOO_LARGE 等）在各自的接口测试里断言，这里只管
FastAPI/Starlette 自己抛出来的那部分 —— 它们过去只回英文 {"detail": "Not Found"}。
"""
import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.services.job_store import JobStore


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('OUT_ROOT', str(tmp_path))
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    from app.main import create_app
    instance = create_app()
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    instance.state.store = JobStore('redis://unused', tmp_path / 'jobs', client=fake)
    instance.state.level_store = JobStore('redis://unused', tmp_path / 'jobs', client=fake,
                                          queue_key='pq:levels')
    return instance


@pytest.fixture
def client(app):
    return TestClient(app)


def test_unknown_path_returns_chinese_error(client):
    resp = client.get('/v1/not-a-route')
    assert resp.status_code == 404
    body = resp.json()
    # 两套键都给：角色客户端读 code，关卡客户端读 error.code
    assert body['code'] == 'NOT_FOUND'
    assert body['error']['code'] == 'NOT_FOUND'
    assert body['message'] == body['error']['message']
    assert '不存在' in body['message']
    assert body['error']['requestId'].startswith('req_')


def test_missing_artifact_returns_chinese_error(client):
    resp = client.get('/artifacts/char_nope/run.png')
    assert resp.status_code == 404
    assert resp.json()['code'] == 'NOT_FOUND'
    assert resp.headers['content-type'].startswith('application/json')


def test_method_not_allowed_returns_chinese_error(client):
    resp = client.delete('/v1/characters')
    assert resp.status_code == 405
    body = resp.json()
    assert body['code'] == 'METHOD_NOT_ALLOWED'
    assert '方法' in body['message']


def test_missing_upload_field_returns_chinese_validation_error(client):
    resp = client.post('/v1/characters')
    assert resp.status_code == 422
    body = resp.json()
    assert body['code'] == 'INVALID_REQUEST'
    assert '参数' in body['message']
    assert body['details']['errors']            # 保留框架给出的字段级细节，便于定位


def test_unhandled_exception_returns_chinese_internal_error(app, monkeypatch):
    def boom(job_id):
        raise RuntimeError('unexpected')

    monkeypatch.setattr(app.state.store, 'get', boom)
    # raise_server_exceptions=False：让 ServerErrorMiddleware 走我们注册的 Exception 处理器
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get('/v1/characters/char_x')
    assert resp.status_code == 500
    body = resp.json()
    assert body['code'] == 'INTERNAL'
    assert 'out/logs' in body['message']        # 直接把人引到当天日志
    assert 'unexpected' not in body['message']  # 内部细节不外泄


def test_business_error_shape_is_untouched_by_framework_handler(client):
    """角色链路仍是扁平体、关卡链路仍是信封体：框架处理器不能改业务契约。"""
    character = client.get('/v1/characters/char_nope')
    assert set(character.json()) == {'code', 'message'}
    level = client.get('/v1/levels/level_nope')
    assert set(level.json()) == {'error'}
    assert level.json()['error']['code'] == 'JOB_NOT_FOUND'
