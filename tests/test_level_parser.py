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
    'analyzing_playability', 'publishing_artifacts',
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
    detection = DetectionResult(platforms, [_goal()], tmp_path / 'ink-mask.png')
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
        return PlayabilityAnalysis(
            playability=state['playability'], profile=profile,
            startPlatformId='platform_001', goalPlatformId='platform_003',
            path=['platform_001', 'platform_002', 'platform_003'] if state['playability'] == 'playable' else [],
            warnings=[])

    monkeypatch.setattr(level_parser.level_rectify, 'rectify', rectify)
    monkeypatch.setattr(level_parser.level_detect, 'detect', detect)
    monkeypatch.setattr(level_parser.level_semantic, 'review', review)
    monkeypatch.setattr(level_parser.playability, 'analyze', analyze)
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
        'x': 26, 'y': 180, 'source': 'inferred', 'confidence': .95}
    assert level.background.sha256 == hashlib.sha256((tmp_path / 'rectified.png').read_bytes()).hexdigest()


def test_parse_passes_injected_client_to_semantic_review(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    semantic_client = object()
    parse(tmp_path, semantic_client=semantic_client)
    assert parser_stubs['semantic_client'] is semantic_client


def test_unplayable_is_needs_fix_and_keeps_exact_geometry(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    parser_stubs['playability'] = 'unreachable'
    before = [(p.start.x, p.start.y, p.end.x, p.end.y) for p in parser_stubs['semantic'].platforms]
    payload = parse(tmp_path, progress=parser_stubs['progress'])

    assert payload['status'] == 'needs_fix'
    after = [(p['start']['x'], p['start']['y'], p['end']['x'], p['end']['y'])
             for p in payload['result']['level']['platforms']]
    assert after == [before[1], before[0], before[2]]
    assert (tmp_path / 'level.json').exists()


def test_ambiguous_goal_is_needs_review_without_level_json(tmp_path, parser_stubs):
    from app.services.level_parser import parse

    goals = [_goal('goal-a', .64), _goal('goal-b', .61)]
    parser_stubs['semantic'] = SemanticResult('opencv', parser_stubs['semantic'].platforms, goals)
    payload = parse(tmp_path, progress=parser_stubs['progress'])

    assert payload['status'] == 'needs_review'
    assert payload['review']['reason'] == 'AMBIGUOUS_GOAL'
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
def test_rectify_review_reasons_never_publish_level(tmp_path, parser_stubs, reason):
    from app.services.level_parser import parse

    parser_stubs['rectify_issue'] = reason
    payload = parse(tmp_path, progress=parser_stubs['progress'])

    assert payload['status'] == 'needs_review'
    assert payload['review']['reason'] == reason
    assert not (tmp_path / 'level.json').exists()
    assert (tmp_path / 'overlay.png').exists()
    assert (tmp_path / 'analysis.json').exists()
    assert parser_stubs['stages'][-1] == 'publishing_artifacts'


@pytest.mark.parametrize('case, reason', [
    ('no-platform', 'NO_PLATFORM_DETECTED'),
    ('no-goal', 'GOAL_NOT_FOUND'),
    ('low-confidence', 'LOW_CONFIDENCE'),
    ('no-start', 'START_PLATFORM_NOT_FOUND'),
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
