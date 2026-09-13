import base64
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
import requests

from app.services.level_detect import (
    DetectionResult, GoalCandidate, PlatformCandidate, PointCandidate, RegionCandidate,
)
from app.services.level_semantic import RequestsSemanticClient, review


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def classify(self, image_data_uri, candidates):
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def detection(tmp_path):
    assert cv2.imwrite(str(tmp_path / 'rectified.png'),
                       np.full((100, 200, 3), 245, dtype=np.uint8))
    return DetectionResult(
        platform_candidates=[
            PlatformCandidate('line_001', PointCandidate(20, 80), PointCandidate(180, 80), .92),
            PlatformCandidate('line_002', PointCandidate(5, 10), PointCandidate(195, 10), .71),
        ],
        goal_candidates=[
            GoalCandidate('goal_001', RegionCandidate(150, 25, 30, 45, .91), .91),
            GoalCandidate('goal_002', RegionCandidate(20, 25, 30, 45, .89), .89),
        ],
        ink_mask_path=tmp_path / 'ink-mask.png',
    )


def accepted_response():
    return {
        'lineClassifications': [
            {'candidateId': 'line_001', 'label': 'platform', 'confidence': .98},
            {'candidateId': 'line_002', 'label': 'shadow', 'confidence': .96},
        ],
        'goalClassifications': [
            {'candidateId': 'goal_001', 'label': 'goal_flag', 'confidence': .97},
            {'candidateId': 'goal_002', 'label': 'other', 'confidence': .95},
        ],
        'sceneIssues': [], 'decision': 'accepted',
    }


def test_unconfigured_llm_uses_opencv_candidates(monkeypatch, detection):
    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    result = review(Path('rectified.png'), detection)
    assert result.source == 'opencv'
    assert [x.id for x in result.platforms] == ['line_001', 'line_002']


@pytest.mark.parametrize('missing', ['LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'])
def test_all_three_environment_variables_are_required(monkeypatch, detection, missing):
    monkeypatch.setenv('LEVEL_LLM_BASE_URL', 'https://llm.example/v1')
    monkeypatch.setenv('LEVEL_LLM_API_KEY', 'secret')
    monkeypatch.setenv('LEVEL_LLM_MODEL', 'reviewer')
    monkeypatch.delenv(missing)
    assert review(Path('missing.png'), detection).source == 'opencv'


def test_llm_can_only_select_existing_candidate_ids(tmp_path, detection):
    client = FakeClient({'lineClassifications': [
        {'candidateId': 'invented', 'label': 'platform', 'confidence': .99}],
        'goalClassifications': [], 'sceneIssues': [], 'decision': 'accepted'})
    result = review(tmp_path / 'rectified.png', detection, client=client)
    assert result.source == 'opencv'
    assert result.degraded_reason == 'INVALID_LLM_RESPONSE'


@pytest.mark.parametrize('response', [
    'not json',
    {'lineClassifications': [], 'goalClassifications': [], 'sceneIssues': []},
    {'lineClassifications': [{'candidateId': 'line_001', 'label': 'bridge', 'confidence': .9}],
     'goalClassifications': [], 'sceneIssues': [], 'decision': 'accepted'},
])
def test_invalid_response_degrades_to_opencv(tmp_path, detection, response):
    result = review(tmp_path / 'rectified.png', detection, client=FakeClient(response))
    assert result.source == 'opencv'
    assert result.degraded_reason == 'INVALID_LLM_RESPONSE'


@pytest.mark.parametrize('error', [requests.Timeout(), requests.RequestException('offline')])
def test_request_failure_degrades_to_opencv(tmp_path, detection, error):
    result = review(tmp_path / 'rectified.png', detection, client=FakeClient(error=error))
    assert result.source == 'opencv'
    assert result.degraded_reason == 'LLM_REQUEST_FAILED'


def test_legal_response_filters_candidates_without_replacing_geometry(tmp_path, detection):
    result = review(tmp_path / 'rectified.png', detection, client=FakeClient(accepted_response()))
    assert result.source == 'llm'
    assert result.platforms == [detection.platform_candidates[0]]
    assert result.platforms[0] is detection.platform_candidates[0]
    assert result.goals == [detection.goal_candidates[0]]
    assert result.goals[0] is detection.goal_candidates[0]


@pytest.mark.parametrize(('response_change', 'reason'), [
    ({'decision': 'needs_review'}, 'LLM_NEEDS_REVIEW'),
    ({'sceneIssues': ['severe_occlusion']}, 'LLM_SCENE_ISSUE'),
])
def test_llm_ambiguity_produces_review_reason(tmp_path, detection, response_change, reason):
    response = accepted_response()
    response.update(response_change)
    result = review(tmp_path / 'rectified.png', detection, client=FakeClient(response))
    assert reason in result.review_reasons


def test_close_goal_scores_produce_review_reason(tmp_path, detection):
    response = accepted_response()
    response['goalClassifications'][1] = {
        'candidateId': 'goal_002', 'label': 'goal_flag', 'confidence': .94,
    }
    result = review(tmp_path / 'rectified.png', detection, client=FakeClient(response))
    assert 'AMBIGUOUS_GOAL' in result.review_reasons


def test_audit_is_atomic_and_does_not_contain_api_key(tmp_path, detection):
    client = RequestsSemanticClient('https://llm.example/v1', 'top-secret-key', 'reviewer')
    client.classify = FakeClient(accepted_response()).classify
    result = review(tmp_path / 'rectified.png', detection, client=client)
    audit_path = tmp_path / 'llm-audit.json'
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    assert result.source == 'llm'
    assert audit['model'] == 'reviewer'
    assert audit['promptVersion'] == 'level-semantic-markers-v2'
    assert audit['requestCandidateIds'] == ['line_001', 'line_002', 'goal_001', 'goal_002']
    assert 'top-secret-key' not in audit_path.read_text(encoding='utf-8')
    assert not (tmp_path / 'llm-audit.json.tmp').exists()


def test_missing_or_duplicate_classification_degrades_to_opencv(tmp_path, detection):
    missing = accepted_response()
    missing['lineClassifications'].pop()
    duplicate = accepted_response()
    duplicate['lineClassifications'].append(
        {'candidateId': 'line_001', 'label': 'shadow', 'confidence': .99})
    for response in (missing, duplicate):
        result = review(tmp_path / 'rectified.png', detection, client=FakeClient(response))
        assert result.degraded_reason == 'INVALID_LLM_RESPONSE'


def test_llm_receives_overlay_with_candidate_labels(tmp_path, detection):
    image = tmp_path / 'rectified.png'
    assert cv2.imwrite(str(image), np.full((100, 200, 3), 245, dtype=np.uint8))

    class CapturingClient(FakeClient):
        def classify(self, image_data_uri, candidates):
            self.image_data_uri = image_data_uri
            return super().classify(image_data_uri, candidates)

    client = CapturingClient(accepted_response())
    review(image, detection, client=client)
    encoded = client.image_data_uri.split(',', 1)[1]
    overlay = cv2.imdecode(np.frombuffer(base64.b64decode(encoded), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert overlay is not None
    assert np.any(overlay != 245)


def test_malformed_chat_completion_envelope_degrades_to_invalid_response(monkeypatch, tmp_path, detection):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': []}

    monkeypatch.setattr(requests, 'post', lambda *args, **kwargs: Response())
    client = RequestsSemanticClient('https://llm.example/v1', 'secret', 'reviewer')
    result = review(tmp_path / 'rectified.png', detection, client=client)
    assert result.degraded_reason == 'INVALID_LLM_RESPONSE'


def test_failure_audit_does_not_contain_api_key(monkeypatch, tmp_path, detection):
    monkeypatch.setattr(requests, 'post', lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout()))
    client = RequestsSemanticClient('https://llm.example/v1', 'failure-secret', 'reviewer')
    review(tmp_path / 'rectified.png', detection, client=client)
    assert 'failure-secret' not in (tmp_path / 'llm-audit.json').read_text(encoding='utf-8')


def test_invalid_image_degrades_without_calling_llm(tmp_path, detection):
    class FailIfCalled:
        def classify(self, image_data_uri, candidates):
            raise AssertionError('invalid image must not call LLM')

    result = review(tmp_path / 'missing.png', detection, client=FailIfCalled())
    assert result.source == 'opencv'
    assert result.degraded_reason == 'LLM_IMAGE_UNAVAILABLE'


def test_arbitrary_client_exception_and_audit_failure_degrade(monkeypatch, tmp_path, detection):
    client = FakeClient(error=RuntimeError('provider bug'))
    monkeypatch.setattr(Path, 'write_text', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
    result = review(tmp_path / 'rectified.png', detection, client=client)
    assert result.source == 'opencv'


def test_audit_replace_failure_preserves_old_file(monkeypatch, tmp_path, detection):
    image = tmp_path / 'rectified.png'
    assert cv2.imwrite(str(image), np.full((100, 200, 3), 245, dtype=np.uint8))
    audit = tmp_path / 'llm-audit.json'
    audit.write_text('{"old":true}', encoding='utf-8')
    original_replace = Path.replace

    def fail_audit_replace(path, target):
        if Path(target) == audit:
            raise OSError('replace failed')
        return original_replace(path, target)

    monkeypatch.setattr(Path, 'replace', fail_audit_replace)
    result = review(image, detection, client=FakeClient(accepted_response()))
    assert result.source == 'llm'
    assert audit.read_text(encoding='utf-8') == '{"old":true}'


def test_invalid_default_client_configuration_degrades_to_opencv(monkeypatch, detection, tmp_path):
    monkeypatch.setenv('LEVEL_LLM_BASE_URL', 'http://llm.example/v1')
    monkeypatch.setenv('LEVEL_LLM_API_KEY', 'secret')
    monkeypatch.setenv('LEVEL_LLM_MODEL', 'reviewer')
    result = review(tmp_path / 'rectified.png', detection)
    assert result.source == 'opencv'
    assert result.degraded_reason == 'LLM_CONFIGURATION_INVALID'


def test_requests_client_rejects_non_https_base_url():
    with pytest.raises(ValueError, match='HTTPS'):
        RequestsSemanticClient('http://llm.example/v1', 'secret', 'reviewer')


def test_requests_client_rejects_non_json_as_invalid_response(monkeypatch, tmp_path, detection):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            raise requests.exceptions.JSONDecodeError('bad', 'x', 0)

    monkeypatch.setattr(requests, 'post', lambda *args, **kwargs: Response())
    client = RequestsSemanticClient('https://llm.example/v1', 'secret', 'reviewer')
    result = review(tmp_path / 'rectified.png', detection, client=client)
    assert result.degraded_reason == 'INVALID_LLM_RESPONSE'


def test_requests_client_uses_openai_compatible_contract(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': [{'message': {'content': json.dumps(accepted_response())}}]}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(requests, 'post', fake_post)
    client = RequestsSemanticClient('https://llm.example/v1/', 'secret', 'reviewer')
    response = client.classify('data:image/jpeg;base64,abc', {'lines': [], 'goals': []})

    assert response == accepted_response()
    assert captured['url'] == 'https://llm.example/v1/chat/completions'
    assert captured['timeout'] == (5, 30)
    assert captured['json']['temperature'] == .1
    assert captured['json']['response_format']['json_schema']['strict'] is True
    assert captured['headers']['Authorization'] == 'Bearer secret'
