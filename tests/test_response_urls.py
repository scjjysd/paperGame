"""响应地址补全：PUBLIC_BASE_URL 优先，其次按请求 Host 推导；存储里始终保持相对路径。"""
import io

import fakeredis
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.urls import absolutize, absolute_url, public_base_url, site_url
from app.services.job_store import JobStore


def test_absolutize_only_touches_site_paths():
    payload = {'spriteSheetUrl': '/artifacts/c1/run.png',
               'statusUrl': '/v1/characters/c1',
               'homepage': 'https://example.com/a.png',
               'note': '相对路径以 /artifacts/ 开头',
               'frameCount': 10,
               'footAnchor': {'x': 1, 'y': None},
               'warnings': [{'url': '/artifacts/c1/overlay.png'}]}
    result = absolutize(payload, 'http://nas.local:8000')
    assert result['spriteSheetUrl'] == 'http://nas.local:8000/artifacts/c1/run.png'
    assert result['statusUrl'] == 'http://nas.local:8000/v1/characters/c1'
    assert result['homepage'] == 'https://example.com/a.png'      # 外部链接不动
    assert result['note'] == '相对路径以 /artifacts/ 开头'          # 普通文案不动
    assert result['frameCount'] == 10 and result['footAnchor'] == {'x': 1, 'y': None}
    assert result['warnings'][0]['url'] == 'http://nas.local:8000/artifacts/c1/overlay.png'
    assert payload['spriteSheetUrl'] == '/artifacts/c1/run.png'    # 不改原对象


def test_empty_base_keeps_relative_paths():
    assert absolutize({'u': '/artifacts/c1/run.png'}, '') == {'u': '/artifacts/c1/run.png'}
    assert absolute_url('', '/artifacts/x.png') == '/artifacts/x.png'
    assert absolute_url('http://h', None) is None


def test_site_url_prefixes():
    assert site_url('/artifacts/a/b.png') and site_url('/v1/levels/x')
    assert not site_url('/healthz') and not site_url('http://x/artifacts/a') and not site_url(None)


def test_public_base_url_prefers_env(monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://game.example/api/')
    assert public_base_url(None) == 'https://game.example/api'      # 结尾斜杠被吃掉


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def build(proxy_headers=False, **env):
        monkeypatch.setenv('OUT_ROOT', str(tmp_path))
        monkeypatch.setenv('REDIS_URL', 'redis://unused')
        monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from app.main import create_app
        app = create_app()
        app.state.store = JobStore('redis://unused', tmp_path / 'jobs',
                                   client=fakeredis.FakeStrictRedis(decode_responses=True))
        if proxy_headers:
            app = ProxyHeadersMiddleware(app, trusted_hosts='*')
        return TestClient(app)
    return build


def _png() -> bytes:
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(output, format='PNG')
    return output.getvalue()


def test_endpoints_use_configured_public_base_url(make_client):
    client = make_client(PUBLIC_BASE_URL='http://nas.local:9000')
    body = client.post('/v1/characters', files={'file': ('c.png', _png(), 'image/png')}).json()
    assert body['statusUrl'] == 'http://nas.local:9000/v1/characters/' + body['jobId']
    assert body['viewUrl'].startswith('http://nas.local:9000/v1/characters/')


def test_endpoints_fall_back_to_request_host(make_client):
    client = make_client()
    body = client.post('/v1/characters', files={'file': ('c.png', _png(), 'image/png')}).json()
    assert body['viewUrl'] == 'http://testserver/v1/characters/{}/view'.format(body['jobId'])


def test_view_page_embeds_absolute_urls(make_client):
    """审查页要能整页拷走：页面里的图与链接都必须是完整地址。"""
    client = make_client(PUBLIC_BASE_URL='http://nas.local:9000')
    job_id = client.post('/v1/characters', files={'file': ('c.png', _png(), 'image/png')}).json()['jobId']
    html = client.get(f'/v1/characters/{job_id}/view').text
    assert 'http://nas.local:9000/artifacts/{}/input.png'.format(job_id) in html
    assert 'href="http://nas.local:9000/v1/characters/{}/detail"'.format(job_id) in html


@pytest.mark.parametrize('scheme,host', [
    ('http', 'scjjysd.xyz:8000'),
    ('https', 'scjjysd.xyz:8443'),
    ('http', '[2001:db8::1]:8000'),
])
@pytest.mark.parametrize('configured', [None, ''])
def test_proxy_preserves_scheme_host_port_and_redirects(make_client, scheme, host, configured):
    """复用 Uvicorn 真实代理头中间件，模拟 Nginx 覆盖后的请求头。"""
    env = {} if configured is None else {'PUBLIC_BASE_URL': configured}
    client = make_client(proxy_headers=True, **env)
    headers = {'host': host, 'x-forwarded-proto': scheme,
               'x-forwarded-for': '2001:db8::2'}
    base = scheme + '://' + host
    body = client.post('/v1/characters', headers=headers,
                       files={'file': ('c.png', _png(), 'image/png')}).json()
    job_id = body['jobId']
    assert body['statusUrl'] == base + '/v1/characters/' + job_id
    assert body['viewUrl'] == base + '/v1/characters/' + job_id + '/view'
    html = client.get('/v1/characters/' + job_id + '/view', headers=headers).text
    assert base + '/artifacts/' + job_id + '/input.png' in html
    redirect = client.post('/v1/characters/', headers=headers, follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers['location'] == base + '/v1/characters'


def test_explicit_base_still_overrides_proxy_inference(make_client):
    client = make_client(proxy_headers=True, PUBLIC_BASE_URL='https://fixed.example')
    body = client.post('/v1/characters',
                       headers={'host': 'scjjysd.xyz:8000', 'x-forwarded-proto': 'http'},
                       files={'file': ('c.png', _png(), 'image/png')}).json()
    assert body['viewUrl'].startswith('https://fixed.example/v1/characters/')
