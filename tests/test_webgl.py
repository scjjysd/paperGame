"""WebGL 浏览器加载契约：入口、预压缩资源、缓存校验和静态目录边界。"""
import gzip
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def webgl_client(tmp_path, monkeypatch):
    from app import main

    root = tmp_path / 'webgl'
    (root / 'Build').mkdir(parents=True)
    (root / 'index.html').write_text('<html>Unity</html>')
    monkeypatch.setattr(main, 'SERVER_DIR', tmp_path)
    monkeypatch.setenv('OUT_ROOT', str(tmp_path / 'out'))
    with TestClient(main.create_app()) as client:
        yield client, root


def test_entry_redirect_and_cache_revalidation(webgl_client):
    client, _ = webgl_client
    redirect = client.get('/webgl', follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers['location'] == 'http://testserver/webgl/'
    response = client.get('/webgl/')
    assert response.status_code == 200
    assert response.text == '<html>Unity</html>'
    assert response.headers['content-type'].startswith('text/html')
    assert response.headers['cache-control'] == 'no-cache'
    cached = client.get('/webgl/', headers={'If-None-Match': response.headers['etag']})
    assert cached.status_code == 304
    assert cached.headers['cache-control'] == 'no-cache'


@pytest.mark.parametrize('name,media_type,payload', [
    ('game.wasm', 'application/wasm', b'\x00asm\x01\x00\x00\x00'),
    ('game.framework.js', 'application/javascript', b'window.unityLoaded = true;'),
    ('game.data', 'application/octet-stream', b'Unity data'),
])
@pytest.mark.parametrize('compressed', [False, True])
def test_build_download_decodes_to_original_bytes(webgl_client, name, media_type, payload, compressed):
    client, root = webgl_client
    name += '.gz' if compressed else ''
    raw = gzip.compress(payload) if compressed else payload
    (root / 'Build' / name).write_bytes(raw)
    response = client.get('/webgl/Build/' + name)
    assert response.status_code == 200
    assert response.content == payload  # httpx 与浏览器一样，根据响应头执行 gzip 解压。
    assert response.headers['content-type'] == media_type
    assert response.headers.get('content-encoding') == ('gzip' if compressed else None)
    assert response.headers['cache-control'] == 'no-cache'
    head = client.head('/webgl/Build/' + name)
    assert head.status_code == 200
    assert head.content == b''
    assert int(head.headers['content-length']) == len(raw)


def test_unityweb_is_left_for_loader_to_decompress(webgl_client):
    client, root = webgl_client
    raw = gzip.compress(b'Unity fallback payload')
    (root / 'Build' / 'game.data.unityweb').write_bytes(raw)
    response = client.get('/webgl/Build/game.data.unityweb')
    assert response.content == raw
    assert 'content-encoding' not in response.headers
    assert response.headers['content-type'] == 'application/octet-stream'


def test_missing_assets_and_directory_escape_are_rejected(webgl_client):
    client, root = webgl_client
    (root.parent / 'private.txt').write_text('private')
    (root / 'outside.txt').symlink_to(root.parent / 'private.txt')
    for path in ['Build/missing.wasm.gz', '%2e%2e/private.txt', 'outside.txt']:
        response = client.get('/webgl/' + path)
        assert response.status_code == 404
        assert response.json()['code'] == 'NOT_FOUND'
        assert 'content-encoding' not in response.headers
    assert client.get('/healthz').json() == {'status': 'ok'}


def test_api_starts_without_webgl_build(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, 'SERVER_DIR', tmp_path)
    monkeypatch.setenv('OUT_ROOT', str(tmp_path / 'out'))
    with TestClient(main.create_app()) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/webgl/').status_code == 404


def test_mobile_template_keeps_unity_visible_in_portrait_orientation():
    server_root = Path(__file__).resolve().parents[1]
    index_html = (server_root / 'webgl' / 'index.html').read_text()
    stylesheet = (server_root / 'webgl' / 'TemplateData' / 'style.css').read_text()

    assert '#rotate-overlay' not in index_html
    assert 'unityContainer.style.display = "none"' not in index_html
    assert '@media (orientation: portrait)' in stylesheet
    assert 'transform: rotate(90deg)' in stylesheet
