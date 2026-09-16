"""WebGL 浏览器加载契约：入口、预压缩资源、缓存校验和静态目录边界。"""
import gzip
import json
from pathlib import Path
import re
import shutil
import subprocess

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
    portrait_css = stylesheet.split('@media (orientation: portrait)', 1)[1]
    assert 'width: 100dvh' in portrait_css
    assert 'height: 100dvw' in portrait_css
    assert 'left: 100dvw' in portrait_css


@pytest.mark.parametrize('template', [False, True], ids=['published', 'unity-template'])
@pytest.mark.parametrize('dpr', [1, 2, 3])
def test_landscape_canvas_resolution_and_rotated_input(template, dpr):
    """执行真实模板脚本，覆盖旋转前分辨率和旋转后鼠标、多点触摸坐标。"""
    node = shutil.which('node')
    if node is None:
        pytest.skip('模板脚本行为回归需要 Node.js')
    root = Path(__file__).resolve().parents[1]
    html_path = root / 'webgl/index.html'
    if template:
        html_path = root.parent / 'PaperGame/Assets/WebGLTemplates/PaperGameMobile/index.html'
        if not html_path.exists():
            pytest.skip('独立服务端检出不包含 Unity 模板')
    html = html_path.read_text()
    source = re.search(r'<script>([\s\S]*?)</script>', html).group(1)
    source = re.sub(r'^#.*$', '', source, flags=re.MULTILINE)
    source = re.sub(r'\{\{\{(.*?)\}\}\}',
                    lambda m: '"test"' if 'JSON.stringify' in m[1] else 'test', source)
    harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let portrait = true, writes = 0, observerCallback;
let width = 300, height = 150;
let rect = {left: 10, top: 20, right: 400, width: 390, height: 844};
const canvas = {
  clientWidth: 844, clientHeight: 390, style: {},
  get width() { return width; }, set width(v) { width = v; writes++; },
  get height() { return height; }, set height(v) { height = v; writes++; },
  getBoundingClientRect() { return rect; }
};
const listeners = {};
function addEventListener(type, callback) {
  (listeners[type] ||= []).push(callback);
}
const element = () => ({style: {}, appendChild() {}, remove() {}});
const window = {
  devicePixelRatio: payload.dpr, scrollX: 7, scrollY: 9,
  navigator: {}, addEventListener,
  matchMedia: query => ({matches: query.includes('orientation') && portrait})
};
const document = {
  querySelector: id => id === '#unity-canvas' ? canvas : element(),
  getElementById: element, createElement: element,
  addEventListener, body: element(), documentElement: element()
};
const context = {window, document, console, setTimeout,
  requestAnimationFrame: (callback) => callback(),
  ResizeObserver: class {
    constructor(callback) { observerCallback = callback; }
    observe(target) { assert.equal(target, canvas); }
  }
};
vm.runInNewContext(payload.source, context);
assert.equal(context.config.matchWebGLToCanvasSize, false,
             '必须禁用 Unity 按旋转后的包围盒自动改写缓冲分辨率');
assert.equal(canvas.width, 844 * payload.dpr, '首次加载必须使用横向布局宽度');
assert.equal(canvas.height, 390 * payload.dpr);
assert.equal(typeof observerCallback, 'function', '必须跟随布局变化');
const initialWrites = writes;
observerCallback();
assert.equal(writes, initialWrites, '相同尺寸不应重设并清空 WebGL 缓冲');
function dispatch(type, event) {
  event.type = type;
  for (const callback of listeners[type] || []) callback(event);
  return event;
}
function point(x, y, id = 0) {
  return {target: canvas, identifier: id, clientX: x, clientY: y,
          pageX: x + 7, pageY: y + 9, screenX: x, screenY: y};
}
function near(actual, expected) { assert.ok(Math.abs(actual - expected) < 0.001,
  `坐标错误：${actual} != ${expected}`); }
// 页面中横向逻辑坐标 (25%, 75%)，顺时针旋转后落在 (25%, 25%)。
const x = rect.left + 390 * .25, y = rect.top + 844 * .25;
const mouse = dispatch('mousedown', point(x, y));
near(mouse.clientX, rect.left + 390 * .25);
near(mouse.clientY, rect.top + 844 * .75);
near(mouse.pageX, mouse.clientX + 7);
near(mouse.pageY, mouse.clientY + 9);
const release = point(x, y);
release.target = document;
dispatch('mouseup', release);
near(release.clientY, rect.top + 844 * .75);
const outside = point(x, y);
outside.target = document;
dispatch('mousemove', outside);
assert.equal(outside.clientY, y, '结束拖动后不得改写其他 DOM 的坐标');
for (const type of ['touchstart', 'touchmove', 'touchend', 'touchcancel']) {
  const p1 = point(rect.right, rect.top, 4);
  const p2 = point(rect.left, rect.top + rect.height, 9);
  const active = type === 'touchend' || type === 'touchcancel' ? [] : [p1, p2];
  const event = dispatch(type, {target: canvas, touches: active,
    targetTouches: active, changedTouches: [p1, p2]});
  near(event.changedTouches[0].clientX, rect.left);
  near(event.changedTouches[0].clientY, rect.top);
  near(event.changedTouches[1].clientX, rect.right);
  near(event.changedTouches[1].clientY, rect.top + rect.height);
  assert.equal(event.changedTouches[0].identifier, 4);
  assert.equal(event.changedTouches[1].identifier, 9);
  assert.equal(event.changedTouches[0].target, canvas);
  assert.equal(event.touches.length, active.length);
}
// 横屏、地址栏引起的尺寸变化、DPR 变化及再次竖屏均保持比例。
portrait = false;
canvas.clientWidth = 844; canvas.clientHeight = 370;
observerCallback();
assert.equal(canvas.width, 844 * payload.dpr);
assert.equal(canvas.height, 370 * payload.dpr);
const landscape = dispatch('mousedown', point(123, 234));
assert.equal(landscape.clientX, 123);
assert.equal(landscape.clientY, 234);
dispatch('mouseup', point(123, 234));
window.devicePixelRatio = 1.5;
dispatch('resize', {});
assert.equal(canvas.width, 1266);
assert.equal(canvas.height, 555);
portrait = true;
canvas.clientWidth = 932; canvas.clientHeight = 430;
observerCallback();
assert.equal(canvas.width, 1398);
assert.equal(canvas.height, 645);
console.log('横竖屏分辨率与输入回归通过');
'''
    result = subprocess.run([node, '-e', harness],
                            input=json.dumps({'source': source, 'dpr': dpr}),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
