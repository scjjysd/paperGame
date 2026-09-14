import hashlib
import json
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from app.level_contracts import DEFAULT_PLAYABILITY_PROFILE, Level, PlayabilityAnalysis
from app.services.level_detect import (
    DetectionResult, GoalCandidate, PlatformCandidate, PointCandidate, RegionCandidate)
from app.services.level_rectify import RectifyIssue, RectifyResult
from app.services.level_semantic import SemanticResult


STAGES = [
    'validating_upload', 'rectifying_paper', 'detecting_platforms',
    'detecting_goal', 'semantic_review', 'validating_geometry',
    'publishing_artifacts',
]


def _platform(candidate_id, x1, y1, x2, y2, confidence=.95):
    return PlatformCandidate(candidate_id, PointCandidate(x1, y1),
                             PointCandidate(x2, y2), confidence)


def _goal(candidate_id='goal_raw', confidence=.94):
    return GoalCandidate(candidate_id, RegionCandidate(330, 40, 30, 30, confidence), confidence)


@pytest.fixture
def parser_stubs(tmp_path, monkeypatch):
    from app.services import level_parser

    Image.new('RGB', (400, 220), 'white').save(tmp_path / 'input.png')
    request = {'playabilityProfile': DEFAULT_PLAYABILITY_PROFILE.model_dump(),
               'createdAt': '2026-09-09T00:00:00Z', 'updatedAt': '2026-09-09T00:00:01Z'}
    (tmp_path / 'request.json').write_text(json.dumps(request), encoding='utf-8')
    platforms = [_platform('raw-b', 130, 120, 240, 120),
                 _platform('raw-a', 10, 180, 100, 180),
                 _platform('raw-c', 300, 70, 390, 70)]
    detection = DetectionResult(platforms, [_goal()], tmp_path / 'ink-mask.png',
                                [PointCandidate(55, 160, .9)])
    state = {'detection': detection, 'semantic': SemanticResult('opencv', platforms, [_goal()]),
             'playability': 'playable', 'rectify_issue': None}
    stages = []

    def rectify(input_path, job_dir):
        if state['rectify_issue']:
            Image.open(input_path).save(job_dir / 'rectified.png')
            raise RectifyIssue(state['rectify_issue'],
                               [{'id': 'paper', 'x': 0, 'y': 0, 'width': 400,
                                 'height': 220, 'confidence': 0}])
        Image.open(input_path).save(job_dir / 'rectified.png')
        return RectifyResult(job_dir / 'rectified.png', 400, 220, [], [])

    def detect(rectified_path, job_dir):
        cv2.imwrite(str(job_dir / 'ink-mask.png'), np.full((220, 400), 255, np.uint8))
        return state['detection']

    def review(rectified_path, detection, client=None):
        state['semantic_client'] = client
        return state['semantic']

    def analyze(level, profile):
        pytest.fail('关卡生成不得再调用可玩性分析')

    monkeypatch.setattr(level_parser.level_rectify, 'rectify', rectify)
    monkeypatch.setattr(level_parser.level_detect, 'detect', detect)
    monkeypatch.setattr(level_parser.level_semantic, 'review', review)
    from app.services import playability
    monkeypatch.setattr(playability, 'analyze', analyze)
    state['stages'] = stages
    state['progress'] = stages.append
    return state


def test_parse_ready_writes_aligned_authoritative_artifacts(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    payload = parse(tmp_path, progress=parser_stubs['progress'])

    assert payload['status'] == 'ready'
    level = Level.model_validate(payload['result']['level'])
    with Image.open(tmp_path / 'rectified.png') as image:
        assert image.size == (level.canvas.width, level.canvas.height)
    assert json.loads((tmp_path / 'level.json').read_text()) == level.model_dump()
    assert (tmp_path / 'analysis.json').exists()
    assert (tmp_path / 'overlay.png').exists()
    assert parser_stubs['stages'] == STAGES
    assert [p.id for p in level.platforms] == ['platform_001', 'platform_002', 'platform_003']
    assert level.playerStart.model_dump() == {
        'x': 55, 'y': 160, 'source': 'detected', 'confidence': .9}
    assert level.background.sha256 == hashlib.sha256((tmp_path / 'rectified.png').read_bytes()).hexdigest()


def test_parse_passes_injected_client_to_semantic_review(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    semantic_client = object()
    parse(tmp_path, semantic_client=semantic_client)
    assert parser_stubs['semantic_client'] is semantic_client


def test_unplayable_is_ready_and_keeps_exact_geometry(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    parser_stubs['playability'] = 'unreachable'
    before = [(p.start.x, p.start.y, p.end.x, p.end.y) for p in parser_stubs['semantic'].platforms]
    payload = parse(tmp_path, progress=parser_stubs['progress'])

    assert payload['status'] == 'ready'
    assert payload['result']['analysis']['playability'] == 'not_checked'
    assert payload['result']['analysis']['warnings'] == []
    after = [(p['start']['x'], p['start']['y'], p['end']['x'], p['end']['y'])
             for p in payload['result']['level']['platforms']]
    assert after == [before[1], before[0], before[2]]
    assert (tmp_path / 'level.json').exists()


def test_ambiguous_goal_is_failed_without_level_json(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    goals = [_goal('goal-a', .64), _goal('goal-b', .61)]
    parser_stubs['semantic'] = SemanticResult('opencv', parser_stubs['semantic'].platforms, goals)
    payload = parse(tmp_path, progress=parser_stubs['progress'])

    assert payload['status'] == 'failed'
    assert payload['error']['code'] == 'AMBIGUOUS_GOAL'
    assert not (tmp_path / 'level.json').exists()
    assert (tmp_path / 'rectified.png').exists()
    assert (tmp_path / 'overlay.png').exists()
    assert (tmp_path / 'analysis.json').exists()
    assert parser_stubs['stages'][-1] == 'publishing_artifacts'
    assert 'analyzing_playability' not in parser_stubs['stages']


def test_review_removes_previous_authoritative_level(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    assert parse(tmp_path)['status'] == 'ready'
    assert (tmp_path / 'level.json').exists()
    parser_stubs['semantic'] = SemanticResult('opencv', [], [_goal()])

    payload = parse(tmp_path)

    assert payload['status'] == 'needs_review'
    assert not (tmp_path / 'level.json').exists()


@pytest.mark.parametrize('reason', [
    'PAPER_NOT_FOUND', 'PAPER_AMBIGUOUS', 'PAPER_OCCLUDED', 'ORIENTATION_AMBIGUOUS'])
def test_rectify_failures_fall_back_to_visible_canvas(tmp_path, parser_stubs, reason):
    from app.services.level_parser import parse

    parser_stubs['rectify_issue'] = reason
    payload = parse(tmp_path, progress=parser_stubs['progress'])

    assert payload['status'] == 'ready'
    assert (tmp_path / 'level.json').exists()
    assert (tmp_path / 'overlay.png').exists()
    assert (tmp_path / 'analysis.json').exists()
    assert parser_stubs['stages'][-1] == 'publishing_artifacts'


@pytest.mark.parametrize('case, reason', [
    ('no-platform', 'NO_PLATFORM_DETECTED'),
    ('low-confidence', 'LOW_CONFIDENCE'),
])
def test_detection_review_reasons_never_publish_level(tmp_path, parser_stubs, case, reason):
    from app.services.level_parser import parse

    platforms = parser_stubs['semantic'].platforms
    goals = parser_stubs['semantic'].goals
    if case == 'no-platform':
        platforms = []
    elif case == 'no-goal':
        goals = []
    elif case == 'low-confidence':
        platforms = [replace(platforms[0], confidence=.3)] + platforms[1:]
    else:
        platforms = [_platform('too-short', 10, 180, 30, 180)]
    parser_stubs['semantic'] = SemanticResult('opencv', platforms, goals)
    payload = parse(tmp_path, progress=parser_stubs['progress'])

    assert payload['status'] == 'needs_review'
    assert payload['review']['reason'] == reason
    assert not (tmp_path / 'level.json').exists()


def test_rejects_out_of_bounds_and_duplicate_geometry(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    out_of_bounds = _platform('outside', -1, 180, 100, 180)
    parser_stubs['semantic'] = SemanticResult(
        'opencv', [out_of_bounds] + parser_stubs['semantic'].platforms[1:], [_goal()])
    first = parse(tmp_path, progress=parser_stubs['progress'])
    assert first['review']['reason'] == 'PLATFORM_GEOMETRY_AMBIGUOUS'

    duplicate = _platform('duplicate', 12, 182, 98, 182)
    parser_stubs['semantic'] = SemanticResult(
        'opencv', [parser_stubs['detection'].platform_candidates[1], duplicate,
                   parser_stubs['detection'].platform_candidates[2]], [_goal()])
    second = parse(tmp_path)
    assert second['review']['reason'] == 'PLATFORM_GEOMETRY_AMBIGUOUS'


def test_overlay_preserves_rectified_and_json_is_byte_stable(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    parse(tmp_path)
    background = (tmp_path / 'rectified.png').read_bytes()
    first = ((tmp_path / 'level.json').read_bytes(), (tmp_path / 'analysis.json').read_bytes())
    parse(tmp_path)
    second = ((tmp_path / 'level.json').read_bytes(), (tmp_path / 'analysis.json').read_bytes())

    assert (tmp_path / 'rectified.png').read_bytes() == background
    assert (tmp_path / 'overlay.png').read_bytes() != background
    assert first == second
    assert b'\n' not in first[0] and b'\n' not in first[1]


def test_request_json_with_null_timestamps_does_not_break_payload(tmp_path):
    """存量脏数据兜底：修复前 levels API 曾把 null 写进 request.json（Redis 过期后 force 重跑）。

    这些文件已经在磁盘上了，解析层若把 None 透到终态 payload，会撞在
    LevelNeedsFix.createdAt 的 str 校验上，整条任务被误判为 PROCESSING_CRASHED。
    """
    from app.services.level_parser import EPOCH_CREATED, _request

    (tmp_path / 'request.json').write_text(
        json.dumps({'createdAt': None, 'updatedAt': None}), encoding='utf-8')

    profile, created, updated = _request(tmp_path)

    assert profile == DEFAULT_PLAYABILITY_PROFILE
    assert created == EPOCH_CREATED
    assert updated == EPOCH_CREATED


@pytest.mark.parametrize('missing,code', [('start', 'START_NOT_FOUND'), ('goal', 'GOAL_NOT_FOUND'),
                                         ('both', 'START_AND_GOAL_NOT_FOUND'), ('multiple', 'AMBIGUOUS_START')])
def test_missing_markers_fail_and_remove_previous_level(tmp_path, parser_stubs, missing, code):
    from app.services.level_parser import parse
    from app.level_contracts import LevelFailed
    assert parse(tmp_path)['status'] == 'ready'
    if missing in ('start', 'both'):
        parser_stubs['detection'] = replace(parser_stubs['detection'], start_candidates=[])
    if missing == 'multiple':
        parser_stubs['detection'] = replace(parser_stubs['detection'], start_candidates=[PointCandidate(20, 20), PointCandidate(50, 50)])
    if missing in ('goal', 'both'):
        parser_stubs['semantic'] = replace(parser_stubs['semantic'], goals=[])
    payload = parse(tmp_path)
    LevelFailed.model_validate(payload)
    assert payload['error']['code'] == code
    assert payload['error']['retryable'] is False
    assert not (tmp_path / 'level.json').exists()


def test_short_upper_platform_does_not_block_detected_start(tmp_path, parser_stubs):
    from app.services.level_parser import parse
    parser_stubs['semantic'] = replace(parser_stubs['semantic'], platforms=[_platform('short', 10, 20, 25, 20)])
    assert parse(tmp_path)['status'] == 'ready'
