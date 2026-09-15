from pathlib import Path

import pytest
from pydantic import ValidationError

from app.level_contracts import (
    ALGORITHM_MAJOR_VERSION,
    DEFAULT_PLAYABILITY_PROFILE,
    Level,
    LevelFailed,
    LevelNeedsFix,
    LevelNeedsReview,
    LevelReady,
    PlayabilityProfile,
    canonical_profile_json,
    derive_level_job_id,
)

FIXTURES = Path(__file__).parents[1] / 'testdata' / 'levels' / 'contracts'

@pytest.mark.parametrize('name,model', [
    ('ready.json', LevelReady), ('needs-fix.json', LevelNeedsFix),
    ('needs-review.json', LevelNeedsReview), ('failed.json', LevelFailed),
])
def test_terminal_fixture_matches_contract(name, model):
    value = model.model_validate_json((FIXTURES / name).read_text())
    assert value.status in {'ready', 'needs_fix', 'needs_review', 'failed'}

@pytest.mark.parametrize('name', ['invalid-out-of-bounds.json', 'invalid-background-size.json'])
def test_invalid_level_fixture_is_rejected(name):
    with pytest.raises(ValidationError):
        Level.model_validate_json((FIXTURES / name).read_text())

def test_level_job_id_is_stable_and_profile_sensitive():
    a = derive_level_job_id(b'image', DEFAULT_PLAYABILITY_PROFILE)
    same = derive_level_job_id(b'image', PlayabilityProfile(**DEFAULT_PLAYABILITY_PROFILE.model_dump()))
    changed = derive_level_job_id(b'image', DEFAULT_PLAYABILITY_PROFILE.model_copy(update={'maxJumpDistancePixels': 231}))
    assert a == same
    assert a.startswith('level_') and len(a) == 18
    assert changed != a
    assert canonical_profile_json(DEFAULT_PLAYABILITY_PROFILE)
    assert ALGORITHM_MAJOR_VERSION == '1'


def test_explicit_marker_jobs_do_not_reuse_old_inferred_start_cache():
    import hashlib
    old_digest = hashlib.sha256(b'image' + canonical_profile_json(DEFAULT_PLAYABILITY_PROFILE)
                                + ALGORITHM_MAJOR_VERSION.encode('ascii')).hexdigest()
    assert derive_level_job_id(b'image', DEFAULT_PLAYABILITY_PROFILE) != 'level_' + old_digest[:12]


def test_visible_canvas_jobs_do_not_reuse_old_marker_cache():
    import hashlib
    old_digest = hashlib.sha256(b'image' + canonical_profile_json(DEFAULT_PLAYABILITY_PROFILE)
                                + ALGORITHM_MAJOR_VERSION.encode('ascii')
                                + b':explicit-markers-v1').hexdigest()
    assert derive_level_job_id(b'image', DEFAULT_PLAYABILITY_PROFILE) != 'level_' + old_digest[:12]


def test_shape_geometry_v4_jobs_do_not_reuse_v3_cache():
    import hashlib
    expected_digest = hashlib.sha256(b'image' + canonical_profile_json(DEFAULT_PLAYABILITY_PROFILE)
                                     + ALGORITHM_MAJOR_VERSION.encode('ascii')
                                     + b':explicit-markers-v4').hexdigest()
    assert derive_level_job_id(b'image', DEFAULT_PLAYABILITY_PROFILE) == 'level_' + expected_digest[:12]
    previous_digest = hashlib.sha256(b'image' + canonical_profile_json(DEFAULT_PLAYABILITY_PROFILE)
                                     + ALGORITHM_MAJOR_VERSION.encode('ascii')
                                     + b':explicit-markers-v3').hexdigest()
    assert derive_level_job_id(b'image', DEFAULT_PLAYABILITY_PROFILE) != 'level_' + previous_digest[:12]


def test_ready_contract_accepts_skipped_playability():
    import json
    payload = json.loads((FIXTURES / 'ready.json').read_text())
    payload['result']['analysis'].update(playability='not_checked', path=[], warnings=[])
    assert LevelReady.model_validate(payload).result.analysis.playability == 'not_checked'

def _level_data():
    return {
        'schemaVersion': '1.0',
        'coordinateSystem': {'origin': 'top_left', 'xAxis': 'right', 'yAxis': 'down', 'unit': 'pixel'},
        'canvas': {'width': 100, 'height': 80},
        'background': {'imageUrl': '/artifacts/level_fixture/rectified.png', 'contentType': 'image/png', 'width': 100, 'height': 80, 'sha256': 'a' * 64},
        'playerStart': {'x': 10, 'y': 70, 'source': 'inferred', 'confidence': 0.9},
        'platforms': [{'id': 'platform_001', 'start': {'x': 5, 'y': 70}, 'end': {'x': 40, 'y': 70}, 'confidence': 0.9}],
        'goalRegion': {'x': 70, 'y': 10, 'width': 20, 'height': 20, 'confidence': 0.9},
    }

def test_level_rejects_external_background_url():
    data = _level_data()
    data['background']['imageUrl'] = 'https://example.invalid/rectified.png'
    with pytest.raises(ValidationError):
        Level.model_validate(data)


def test_level_requires_fixed_coordinate_system_and_platform():
    data = _level_data()
    assert Level.model_validate(data).canvas.width == 100
    with pytest.raises(ValidationError):
        Level.model_validate({**data, 'coordinateSystem': {**data['coordinateSystem'], 'unit': 'world'}})
    with pytest.raises(ValidationError):
        Level.model_validate({**data, 'platforms': []})


def test_level_defaults_walls_and_blocks_to_empty_lists():
    level = Level.model_validate(_level_data())
    assert level.walls == []
    assert level.blocks == []


def test_blocks_only_level_is_valid_geometry():
    data = _level_data()
    data['platforms'] = []
    data['blocks'] = [{'id': 'block_001', 'region': {
        'x': 10, 'y': 50, 'width': 50, 'height': 20, 'confidence': .9}}]
    assert len(Level.model_validate(data).blocks) == 1


def test_level_accepts_valid_walls_and_blocks():
    data = _level_data()
    data['walls'] = [{
        'id': 'wall_001',
        'start': {'x': 50, 'y': 10},
        'end': {'x': 52, 'y': 60},
        'confidence': 0.8,
    }]
    data['blocks'] = [{
        'id': 'block_001',
        'region': {'x': 60, 'y': 40, 'width': 20, 'height': 15, 'confidence': 0.7},
    }]

    level = Level.model_validate(data)

    assert level.walls[0].id == 'wall_001'
    assert level.blocks[0].region.width == 20


@pytest.mark.parametrize('wall', [
    {'id': 'wall_001', 'start': {'x': 50, 'y': 10}, 'end': {'x': 50, 'y': 10}, 'confidence': 0.8},
    {'id': 'wall_001', 'start': {'x': 10, 'y': 20}, 'end': {'x': 60, 'y': 22}, 'confidence': 0.8},
    {'id': 'wall_001', 'start': {'x': 50, 'y': 10}, 'end': {'x': 50, 'y': 80}, 'confidence': 0.8},
])
def test_level_rejects_invalid_wall_geometry(wall):
    with pytest.raises(ValidationError):
        Level.model_validate({**_level_data(), 'walls': [wall]})


@pytest.mark.parametrize('region', [
    {'x': 60, 'y': 40, 'width': 0, 'height': 15, 'confidence': 0.7},
    {'x': 90, 'y': 40, 'width': 20, 'height': 15, 'confidence': 0.7},
])
def test_level_rejects_invalid_block_region(region):
    block = {'id': 'block_001', 'region': region}
    with pytest.raises(ValidationError):
        Level.model_validate({**_level_data(), 'blocks': [block]})


@pytest.mark.parametrize('geometry_field,geometry', [
    ('walls', {'id': 'platform_001', 'start': {'x': 50, 'y': 10}, 'end': {'x': 50, 'y': 60}, 'confidence': 0.8}),
    ('blocks', {'id': 'platform_001', 'region': {'x': 60, 'y': 40, 'width': 20, 'height': 15, 'confidence': 0.7}}),
])
def test_level_rejects_ids_reused_across_geometry_types(geometry_field, geometry):
    with pytest.raises(ValidationError):
        Level.model_validate({**_level_data(), geometry_field: [geometry]})

def test_level_rejects_duplicate_ids_bad_order_zero_length_and_outside_goal():
    data = _level_data()
    with pytest.raises(ValidationError):
        Level.model_validate({**data, 'platforms': [data['platforms'][0], data['platforms'][0]]})
    with pytest.raises(ValidationError):
        Level.model_validate({**data, 'platforms': [{**data['platforms'][0], 'start': {'x': 41, 'y': 70}}]})
    with pytest.raises(ValidationError):
        Level.model_validate({**data, 'platforms': [{**data['platforms'][0], 'end': {'x': 5, 'y': 70}}]})
    with pytest.raises(ValidationError):
        Level.model_validate({**data, 'goalRegion': {**data['goalRegion'], 'x': 90}})

@pytest.mark.parametrize('field,value', [
    ('maxJumpRisePixels', 0), ('maxJumpDistancePixels', 0), ('characterWidthPixels', 0),
    ('characterHeightPixels', 0), ('landingTolerancePixels', -1),
])
def test_playability_profile_has_positive_and_non_negative_boundaries(field, value):
    with pytest.raises(ValidationError):
        PlayabilityProfile(**{**DEFAULT_PLAYABILITY_PROFILE.model_dump(), field: value})
    profile = PlayabilityProfile(**{**DEFAULT_PLAYABILITY_PROFILE.model_dump(), 'landingTolerancePixels': 0})
    assert profile.landingTolerancePixels == 0

def test_http_error_envelope_and_optional_details():
    from app.level_contracts import ErrorEnvelope
    error = ErrorEnvelope.model_validate({'error': {'code': 'UNAUTHORIZED', 'message': 'no auth', 'retryable': False, 'requestId': 'req_1'}})
    assert error.error.code == 'UNAUTHORIZED'
    assert ErrorEnvelope.model_validate({'error': {'code': 'FILE_TOO_LARGE', 'message': 'too large', 'retryable': False, 'requestId': 'req_2', 'details': {'limitBytes': 10485760}}}).error.details['limitBytes'] == 10485760
