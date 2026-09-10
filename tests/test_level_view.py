"""关卡审查视图：detail 收集 + 单页 HTML。纯逻辑，离线可跑（不需要真实解析产物）。"""
import json
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.api.level_view import build_level_html, collect_level_detail
from app.level_contracts import LEVEL_ERROR_MESSAGES, LEVEL_STATUS_MESSAGES
from app.services.job_store import JobStore

BASE = 'http://testserver'
FIXTURES = Path(__file__).parents[1] / 'testdata' / 'levels' / 'contracts'
IMAGE_ARTIFACTS = ('input.png', 'rectified.png', 'overlay.png', 'paper-mask.png', 'ink-mask.png')


def _envelope(job_id, fixture):
    payload = json.loads((FIXTURES / fixture).read_text(encoding='utf-8'))
    payload['jobId'] = job_id
    return payload


def _seed(tmp_path, job_id, fixture, with_json_artifacts=True):
    """按真实 job 目录布局落产物；Redis 留空，走 result.json 快照重建路径。"""
    job_dir = tmp_path / 'jobs' / job_id
    job_dir.mkdir(parents=True)
    for name in IMAGE_ARTIFACTS:
        (job_dir / name).write_bytes(b'png')
    envelope = _envelope(job_id, fixture)
    (job_dir / 'result.json').write_text(json.dumps(envelope, ensure_ascii=False), encoding='utf-8')
    if with_json_artifacts and 'result' in envelope:
        (job_dir / 'level.json').write_text(json.dumps(envelope['result']['level']), encoding='utf-8')
        (job_dir / 'analysis.json').write_text(json.dumps(envelope['result']['analysis']), encoding='utf-8')
    return job_dir, envelope


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('OUT_ROOT', str(tmp_path))
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    from app.main import create_app
    app = create_app()
    app.state.level_store = JobStore(
        'redis://unused', tmp_path / 'jobs',
        client=fakeredis.FakeStrictRedis(decode_responses=True), queue_key='pq:levels')
    return TestClient(app)


def test_detail_lists_every_artifact_with_absolute_url(client, tmp_path):
    _seed(tmp_path, 'level_t', 'needs-fix.json')
    body = client.get('/v1/levels/level_t/detail').json()
    assert body['jobId'] == 'level_t'
    assert body['status'] == 'needs_fix'
    assert body['artifacts']['inputUrl'] == f'{BASE}/artifacts/level_t/input.png'
    assert body['artifacts']['rectifiedImageUrl'] == f'{BASE}/artifacts/level_t/rectified.png'
    assert body['artifacts']['levelJsonUrl'] == f'{BASE}/artifacts/level_t/level.json'
    assert body['viewUrl'] == f'{BASE}/v1/levels/level_t/view'
    assert body['statusUrl'] == f'{BASE}/v1/levels/level_t'


def test_detail_carries_geometry_and_playability(client, tmp_path):
    _seed(tmp_path, 'level_t', 'needs-fix.json')
    body = client.get('/v1/levels/level_t/detail').json()
    assert body['canvas'] == {'width': 1245, 'height': 810}
    assert [p['id'] for p in body['platforms']] == ['platform_001', 'platform_002']
    assert body['platforms'][0]['onPath'] is False      # needs-fix 的 path 为空
    assert body['playability'] == 'unreachable'
    assert body['warnings'][0]['code'] == 'JUMP_GAP_TOO_HIGH'
    assert '可玩性' in body['message']                   # 中文原因里点出告警


def test_detail_for_needs_review_exposes_candidates(client, tmp_path):
    _seed(tmp_path, 'level_r', 'needs-review.json', with_json_artifacts=False)
    body = client.get('/v1/levels/level_r/detail').json()
    assert body['status'] == 'needs_review'
    assert body['review']['reason'] == 'AMBIGUOUS_GOAL'
    assert len(body['platforms']) == 0
    assert body['reviewReason'] == 'AMBIGUOUS_GOAL'
    assert body['message'].startswith('[AMBIGUOUS_GOAL]')


def test_detail_for_failed_exposes_error(client, tmp_path):
    _seed(tmp_path, 'level_f', 'failed.json', with_json_artifacts=False)
    body = client.get('/v1/levels/level_f/detail').json()
    assert body['status'] == 'failed'
    assert body['error']['code']
    assert body['message'].startswith('[{}]'.format(body['error']['code']))


def test_detail_missing_artifacts_degrade_to_none(tmp_path):
    """产物没落盘时必须是 None，不能给出坏链接。"""
    job_dir = tmp_path / 'jobs' / 'level_q'
    job_dir.mkdir(parents=True)
    detail = collect_level_detail('level_q', job_dir, {'status': 'queued', 'stage': 'waiting'})
    assert detail['status'] == 'queued'
    assert detail['progress'] == {'stage': 'waiting', 'stageLabel': '等待中', 'percent': 0}
    assert detail['statusMessage'] == LEVEL_STATUS_MESSAGES['queued']
    assert detail['artifacts']['rectifiedImageUrl'] is None
    assert detail['level'] is None and detail['platforms'] == []
    assert detail['canvas'] == {'width': None, 'height': None}


def test_detail_falls_back_to_disk_when_redis_expired(tmp_path):
    """Redis 过期后只剩磁盘产物：审查页仍要能画出平台，不能一片空白。"""
    job_dir = tmp_path / 'jobs' / 'level_d'
    job_dir.mkdir(parents=True)
    envelope = _envelope('level_d', 'ready.json')
    (job_dir / 'rectified.png').write_bytes(b'png')
    (job_dir / 'level.json').write_text(json.dumps(envelope['result']['level']), encoding='utf-8')
    (job_dir / 'analysis.json').write_text(json.dumps(envelope['result']['analysis']), encoding='utf-8')

    detail = collect_level_detail('level_d', job_dir, {'status': 'ready', 'stage': 'publishing_artifacts'})

    assert detail['level']['canvas'] == envelope['result']['level']['canvas']
    assert len(detail['platforms']) == len(envelope['result']['level']['platforms'])
    assert detail['playability'] == envelope['result']['analysis']['playability']


def test_detail_keeps_relative_paths_without_base_url(tmp_path):
    job_dir = tmp_path / 'jobs' / 'level_q'
    job_dir.mkdir(parents=True)
    (job_dir / 'rectified.png').write_bytes(b'png')
    detail = collect_level_detail('level_q', job_dir, {'status': 'processing'})
    assert detail['artifacts']['rectifiedImageUrl'] == '/artifacts/level_q/rectified.png'
    assert detail['viewUrl'] == '/v1/levels/level_q/view'


def test_html_draws_one_line_per_platform(client, tmp_path):
    _seed(tmp_path, 'level_t', 'needs-fix.json')
    resp = client.get('/v1/levels/level_t/view')
    assert resp.headers['content-type'].startswith('text/html')
    html = resp.text
    assert html.count('<line ') == 2
    assert 'platform_001' in html and 'platform_002' in html
    assert '终点' in html and '出生点' in html


def test_html_draws_review_candidates(client, tmp_path):
    _seed(tmp_path, 'level_r', 'needs-review.json', with_json_artifacts=False)
    html = client.get('/v1/levels/level_r/view').text
    assert 'goal_candidate_01' in html and 'goal_candidate_02' in html
    assert '人工复核' in html
    assert '只保留一个终点旗帜' in html          # suggestions 直接展示


def test_html_embeds_absolute_artifact_urls(client, tmp_path):
    _seed(tmp_path, 'level_t', 'needs-fix.json')
    html = client.get('/v1/levels/level_t/view').text
    assert f'{BASE}/artifacts/level_t/rectified.png' in html
    assert f'href="{BASE}/artifacts/level_t/level.json"' in html


def test_html_never_emits_empty_src(tmp_path):
    """空 src 会让浏览器把当前页面当图片再请求一遍。"""
    job_dir = tmp_path / 'jobs' / 'level_q'
    job_dir.mkdir(parents=True)
    html = build_level_html(collect_level_detail('level_q', job_dir, {'status': 'queued'}))
    assert 'src=""' not in html and "src=''" not in html
    assert '暂无产物落盘' in html


def test_html_escapes_job_id(tmp_path):
    """job_id 来自 URL 路径段，未经格式校验，必须转义后再进 HTML。"""
    job_dir = tmp_path / 'jobs' / 'x'
    job_dir.mkdir(parents=True)
    html = build_level_html(collect_level_detail('<script>alert(1)</script>', job_dir, {'status': 'queued'}))
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_html_marks_unreachable_and_lists_warnings(client, tmp_path):
    _seed(tmp_path, 'level_t', 'needs-fix.json')
    html = client.get('/v1/levels/level_t/view').text
    assert '不可达' in html
    assert 'JUMP_GAP_TOO_HIGH' in html


def test_view_unknown_job_is_404_with_chinese_reason(client):
    resp = client.get('/v1/levels/level_nope/view')
    assert resp.status_code == 404
    # 断言错误体而非只看状态码：路由不存在时也是 404，只看码就是个永远通不了假的测试
    assert resp.json()['error']['code'] == 'JOB_NOT_FOUND'
    assert resp.json()['error']['message'] == LEVEL_ERROR_MESSAGES['JOB_NOT_FOUND']


def test_detail_unknown_job_is_404(client):
    resp = client.get('/v1/levels/level_nope/detail')
    assert resp.status_code == 404
    assert resp.json()['error']['code'] == 'JOB_NOT_FOUND'


def test_view_does_not_pollute_contract_endpoint(client, tmp_path):
    """/v1/levels/{id} 是客户端契约：审查页新增的字段不得渗进去。"""
    _seed(tmp_path, 'level_t', 'needs-fix.json')
    body = client.get('/v1/levels/level_t').json()
    for review_only in ('platforms', 'canvas', 'artifacts', 'progress'):
        assert review_only not in body
    assert body['result']['artifacts']['levelJsonUrl'].startswith(f'{BASE}/artifacts/')
