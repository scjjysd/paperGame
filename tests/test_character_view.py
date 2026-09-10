"""审查视图：detail 收集 + HTML 生成。纯逻辑，离线可跑（无需 TorchServe / OpenGL）。"""
import json

import fakeredis
import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.character_view import build_detail_html, collect_detail
from app.contracts import ERROR_MESSAGES
from app.services.job_store import JobStore

BASE = 'http://testserver'

READY_RESULT = {
    'status': 'ready',
    'characterId': 'char_t',
    'animations': {
        'run': {'spriteSheetUrl': '/artifacts/char_t/run.png', 'frameCount': 10, 'fps': 15,
                'frameWidth': 241, 'frameHeight': 275, 'footAnchor': {'x': 120, 'y': 275}},
        'jump': {'spriteSheetUrl': '/artifacts/char_t/jump.png', 'frameCount': 7, 'fps': 15,
                 'frameWidth': 241, 'frameHeight': 275, 'footAnchor': {'x': 120, 'y': 275}},
    },
}
CHAR_CFG = {
    'width': 517, 'height': 595,
    'skeleton': [{'name': 'root', 'loc': [289, 422], 'parent': None},
                 {'name': 'hip', 'loc': [289, 422], 'parent': 'root'}],
}


@pytest.fixture
def ready_job(tmp_path):
    d = tmp_path / 'char_t'
    (d / 'anno').mkdir(parents=True)
    (d / 'input.png').write_bytes(b'png')
    for m in ('run', 'jump'):
        (d / f'{m}.png').write_bytes(b'png')
        (d / f'{m}.gif').write_bytes(b'gif')
    for f in ('mask.png', 'texture.png'):
        (d / 'anno' / f).write_bytes(b'x')
    (d / 'anno' / 'char_cfg.yaml').write_text(yaml.safe_dump(CHAR_CFG))
    (d / 'result.json').write_text(json.dumps(READY_RESULT))
    return d


def test_ready_detail_lists_every_artifact(ready_job):
    d = collect_detail('char_t', ready_job, 'ready', READY_RESULT)
    assert d['status'] == 'ready'
    assert d['inputUrl'] == '/artifacts/char_t/input.png'
    assert d['viewUrl'] == '/v1/characters/char_t/view'
    assert d['annotation']['maskUrl'] == '/artifacts/char_t/anno/mask.png'
    assert d['annotation']['textureUrl'] == '/artifacts/char_t/anno/texture.png'
    assert (d['annotation']['width'], d['annotation']['height']) == (517, 595)
    assert [j['name'] for j in d['annotation']['joints']] == ['root', 'hip']
    assert d['animations']['run']['gifUrl'] == '/artifacts/char_t/run.gif'
    assert d['animations']['run']['frameCount'] == 10
    assert d['renderedAt'] is not None


def test_missing_gif_degrades_to_none(ready_job):
    """GIF 保留是本次才加的，历史 job 目录没有：必须降级为 None，不能给出坏链接。"""
    (ready_job / 'run.gif').unlink()
    d = collect_detail('char_t', ready_job, 'ready', READY_RESULT)
    assert d['animations']['run']['gifUrl'] is None
    assert d['animations']['jump']['gifUrl'] == '/artifacts/char_t/jump.gif'


def test_early_failure_annotation_urls_are_none(tmp_path):
    """早期失败时 result.json 里的 maskUrl 指向并不存在的文件（render_runner 无条件写死），
    所以 detail 必须按磁盘实际存在与否判定，不能照抄。"""
    d = tmp_path / 'char_e'
    (d / 'anno').mkdir(parents=True)
    (d / 'input.png').write_bytes(b'png')
    (d / 'anno' / 'image.png').write_bytes(b'x')   # vendor 存的原图副本，唯一存在的产物
    result = {'status': 'needs_correction', 'reason': 'NO_HUMANOID',
              'maskUrl': '/artifacts/char_e/anno/mask.png', 'joints': []}
    detail = collect_detail('char_e', d, 'needs_correction', result)
    assert detail['reason'] == 'NO_HUMANOID'
    assert detail['annotation']['maskUrl'] is None
    assert detail['annotation']['joints'] == []
    assert detail['animations'] == {}


def test_queued_detail_has_input_but_no_products(tmp_path):
    d = tmp_path / 'char_q'
    d.mkdir()
    (d / 'input.png').write_bytes(b'png')
    detail = collect_detail('char_q', d, 'queued', None)
    assert detail['status'] == 'queued'
    assert detail['inputUrl'] == '/artifacts/char_q/input.png'
    assert detail['animations'] == {}
    assert detail['renderedAt'] is None


def test_failed_detail_carries_error_code(tmp_path):
    d = tmp_path / 'char_f'
    d.mkdir()
    (d / 'input.png').write_bytes(b'png')
    detail = collect_detail('char_f', d, 'failed', {'status': 'failed', 'code': 'ASSET_MISSING'})
    assert detail['status'] == 'failed'
    assert detail['code'] == 'ASSET_MISSING'


def test_html_sprite_animation_matches_frame_metadata(ready_job):
    """CSS steps 数与终点位移必须由 frameCount/frameWidth 算出：错一处播放就会撕帧或漂移。"""
    html = build_detail_html(collect_detail('char_t', ready_job, 'ready', READY_RESULT))
    assert 'steps(10)' in html and '-2410px' in html    # run: 10 帧 x 241px
    assert 'steps(7)' in html and '-1687px' in html     # jump: 7 帧 x 241px


def test_html_omits_src_for_missing_artifacts(tmp_path):
    """缺图必须输出占位块：空 src 会让浏览器把当前页面当图片重新请求一遍。"""
    d = tmp_path / 'char_q'
    d.mkdir()
    html = build_detail_html(collect_detail('char_q', d, 'queued', None))
    assert 'src=""' not in html and "src=''" not in html


def test_html_draws_one_circle_per_joint(ready_job):
    html = build_detail_html(collect_detail('char_t', ready_job, 'ready', READY_RESULT))
    assert html.count('<circle') == len(CHAR_CFG['skeleton'])


def test_html_escapes_job_id(tmp_path):
    """job_id 直接来自 URL 路径段，未经格式校验，必须转义后再进 HTML。"""
    d = tmp_path / 'x'
    d.mkdir()
    html = build_detail_html(collect_detail('<script>alert(1)</script>', d, 'queued', None))
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('OUT_ROOT', str(tmp_path))
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    from app.main import create_app
    app = create_app()
    app.state.store = JobStore('redis://unused', tmp_path / 'jobs',
                               client=fakeredis.FakeStrictRedis(decode_responses=True))
    return TestClient(app)


def _seed(tmp_path, job_id='char_t'):
    """只落磁盘产物、不写 Redis：job_store.get 会用 result.json 快照重建，顺带覆盖 TTL 过期路径。"""
    d = tmp_path / 'jobs' / job_id
    (d / 'anno').mkdir(parents=True)
    (d / 'input.png').write_bytes(b'png')
    for m in ('run', 'jump'):
        (d / f'{m}.png').write_bytes(b'png')
        (d / f'{m}.gif').write_bytes(b'gif')
    for f in ('mask.png', 'texture.png'):
        (d / 'anno' / f).write_bytes(b'x')
    (d / 'anno' / 'char_cfg.yaml').write_text(yaml.safe_dump(CHAR_CFG))
    (d / 'result.json').write_text(json.dumps(READY_RESULT))
    return d


def test_detail_endpoint_returns_all_artifact_urls(client, tmp_path):
    _seed(tmp_path)
    body = client.get('/v1/characters/char_t/detail').json()
    # 端点给完整地址，浏览器里直接可点
    assert body['inputUrl'] == f'{BASE}/artifacts/char_t/input.png'
    assert body['animations']['run']['gifUrl'] == f'{BASE}/artifacts/char_t/run.gif'
    assert body['viewUrl'] == f'{BASE}/v1/characters/char_t/view'
    assert len(body['annotation']['joints']) == len(CHAR_CFG['skeleton'])


def test_detail_without_base_url_keeps_relative_paths(ready_job):
    """collect_detail 不传 base_url 时保持相对路径：离线自检与历史调用方不受影响。"""
    detail = collect_detail('char_t', ready_job, 'ready', READY_RESULT)
    assert detail['inputUrl'] == '/artifacts/char_t/input.png'
    detail = collect_detail('char_t', ready_job, 'ready', READY_RESULT, 'https://cdn.example')
    assert detail['inputUrl'] == 'https://cdn.example/artifacts/char_t/input.png'


def test_detail_carries_chinese_reason(tmp_path):
    """审查页要直接把中文原因展给人看，不能只给错误码。"""
    d = tmp_path / 'char_e'
    d.mkdir()
    detail = collect_detail('char_e', d, 'needs_correction',
                            {'status': 'needs_correction', 'reason': 'NO_HUMANOID', 'joints': []})
    assert detail['message']
    assert '人形' in detail['message']


def test_detail_unknown_job_is_404(client):
    resp = client.get('/v1/characters/char_nope/detail')
    assert resp.status_code == 404
    assert resp.json() == {'code': 'JOB_NOT_FOUND', 'message': ERROR_MESSAGES['JOB_NOT_FOUND']}


def test_view_endpoint_serves_html(client, tmp_path):
    _seed(tmp_path)
    resp = client.get('/v1/characters/char_t/view')
    assert resp.status_code == 200
    assert resp.headers['content-type'].startswith('text/html')
    assert 'steps(10)' in resp.text
    # 页面里的产物链接也是完整地址，拷到别的机器上打开照样能看图
    assert f'{BASE}/artifacts/char_t/run.png' in resp.text


def test_view_unknown_job_is_404(client):
    resp = client.get('/v1/characters/char_nope/view')
    assert resp.status_code == 404
    # 断言错误体而非只看状态码：路由不存在时也是 404，只看码就是个永远通不了假的测试
    assert resp.json() == {'code': 'JOB_NOT_FOUND', 'message': ERROR_MESSAGES['JOB_NOT_FOUND']}


def test_contract_endpoint_stays_unpolluted(client, tmp_path):
    """/v1/characters/{id} 是 Unity 契约：只能多出中文说明与完整地址，不得掺审查字段。"""
    _seed(tmp_path)
    body = client.get('/v1/characters/char_t').json()
    assert body['characterId'] == 'char_t'
    assert body['animations']['run']['spriteSheetUrl'] == f'{BASE}/artifacts/char_t/run.png'
    assert body['animations']['run']['frameCount'] == READY_RESULT['animations']['run']['frameCount']
    for review_only in ('annotation', 'inputUrl', 'renderedAt', 'detailUrl'):
        assert review_only not in body
