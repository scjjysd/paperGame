"""跨域（CORS）配置：默认放开所有来源，可显式收窄；预检请求不必进业务逻辑。"""
import pytest
from fastapi.testclient import TestClient

from app.main import cors_options


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def build(**env):
        monkeypatch.setenv('OUT_ROOT', str(tmp_path))
        monkeypatch.setenv('REDIS_URL', 'redis://unused')
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from app.main import create_app
        return TestClient(create_app())
    return build


PREFLIGHT = {'Origin': 'https://unity.webgl.example', 'Access-Control-Request-Method': 'POST'}


def test_preflight_is_answered_with_wildcard_by_default(make_client):
    client = make_client()
    resp = client.options('/v1/characters', headers=PREFLIGHT)
    assert resp.status_code == 200
    assert resp.headers['access-control-allow-origin'] == '*'
    assert 'POST' in resp.headers['access-control-allow-methods']
    assert resp.headers['access-control-max-age'] == '600'


def test_simple_response_carries_cors_header(make_client):
    client = make_client()
    resp = client.get('/healthz', headers={'Origin': 'https://unity.webgl.example'})
    assert resp.headers['access-control-allow-origin'] == '*'


def test_static_artifacts_are_cross_origin_readable(make_client, tmp_path):
    """Unity WebGL 要跨源下载精灵表：/artifacts 也必须带 CORS 头。"""
    client = make_client()
    (tmp_path / 'jobs').mkdir(parents=True, exist_ok=True)
    sheet = tmp_path / 'jobs' / 'char_cors'
    sheet.mkdir()
    (sheet / 'run.png').write_bytes(b'png')
    resp = client.get('/artifacts/char_cors/run.png', headers={'Origin': 'https://unity.webgl.example'})
    assert resp.status_code == 200
    assert resp.headers['access-control-allow-origin'] == '*'


def test_explicit_origin_whitelist_is_echoed(make_client):
    client = make_client(CORS_ALLOW_ORIGINS='https://a.example, https://b.example',
                         CORS_ALLOW_CREDENTIALS='true')
    allowed = client.get('/healthz', headers={'Origin': 'https://b.example'})
    assert allowed.headers['access-control-allow-origin'] == 'https://b.example'
    assert allowed.headers['access-control-allow-credentials'] == 'true'
    # 不在白名单里的来源拿不到任何 CORS 头，浏览器自然拦下
    denied = client.get('/healthz', headers={'Origin': 'https://evil.example'})
    assert 'access-control-allow-origin' not in denied.headers


def test_methods_and_headers_can_be_narrowed(make_client):
    client = make_client(CORS_ALLOW_METHODS='GET,POST', CORS_ALLOW_HEADERS='Content-Type,X-Trace-Id')
    resp = client.options('/v1/levels', headers={'Origin': 'https://a.example',
                                                 'Access-Control-Request-Method': 'POST'})
    allow_methods = resp.headers['access-control-allow-methods']
    assert 'POST' in allow_methods and 'DELETE' not in allow_methods
    assert 'X-Trace-Id' in resp.headers['access-control-allow-headers']


def test_wildcard_origin_never_allows_credentials(monkeypatch):
    """浏览器规范：Allow-Origin 为 * 时带凭证会被整个响应拒收，必须自动关掉。"""
    monkeypatch.delenv('CORS_ALLOW_ORIGINS', raising=False)
    monkeypatch.setenv('CORS_ALLOW_CREDENTIALS', 'true')
    options = cors_options()
    assert options['allow_origins'] == ['*']
    assert options['allow_credentials'] is False


def test_explicit_origins_keep_credentials(monkeypatch):
    monkeypatch.setenv('CORS_ALLOW_ORIGINS', 'https://a.example')
    monkeypatch.setenv('CORS_ALLOW_CREDENTIALS', 'true')
    assert cors_options()['allow_credentials'] is True


def test_bad_max_age_falls_back_to_default(monkeypatch):
    monkeypatch.setenv('CORS_MAX_AGE', 'not-a-number')
    assert cors_options()['max_age'] == 600
