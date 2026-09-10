import pytest

from app.level_contracts import DEFAULT_PLAYABILITY_PROFILE, Level
from app.services.playability import analyze


def _level(platforms, start=(20, 180), goal=(330, 40, 30, 30)):
    return Level.model_validate({
        'schemaVersion': '1.0',
        'coordinateSystem': {'origin': 'top_left', 'xAxis': 'right', 'yAxis': 'down', 'unit': 'pixel'},
        'canvas': {'width': 400, 'height': 240},
        'background': {
            'imageUrl': '/artifacts/level_fixture/rectified.png',
            'contentType': 'image/png',
            'width': 400,
            'height': 240,
            'sha256': 'a' * 64,
        },
        'playerStart': {'x': start[0], 'y': start[1], 'source': 'inferred', 'confidence': 0.9},
        'platforms': [
            {'id': platform_id, 'start': {'x': x1, 'y': y1}, 'end': {'x': x2, 'y': y2}, 'confidence': 0.9}
            for platform_id, x1, y1, x2, y2 in platforms
        ],
        'goalRegion': {'x': goal[0], 'y': goal[1], 'width': goal[2], 'height': goal[3], 'confidence': 0.9},
    })


@pytest.fixture
def profile():
    return DEFAULT_PLAYABILITY_PROFILE.model_copy(update={
        'maxJumpRisePixels': 60,
        'maxJumpDistancePixels': 100,
        'characterWidthPixels': 32,
        'landingTolerancePixels': 6,
    })


def _warnings(result, code):
    return [warning for warning in result.warnings if warning.code == code]


def test_finds_stable_shortest_path_without_mutating_geometry(profile):
    level = _level([
        ('platform_004', 290, 70, 380, 70),
        ('platform_003', 130, 120, 210, 120),
        ('platform_002', 130, 120, 220, 120),
        ('platform_001', 0, 180, 80, 180),
    ])
    before = level.model_dump_json()

    first = analyze(level, profile)
    second = analyze(level, profile)

    assert first.playability == 'playable'
    assert first.path == ['platform_001', 'platform_002', 'platform_004']
    assert first.model_dump_json() == second.model_dump_json()
    assert level.model_dump_json() == before


def test_unreachable_returns_no_path_warning(profile):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 320, 70, 380, 70),
    ])

    result = analyze(level, profile)

    assert result.playability == 'unreachable'
    assert result.path == []
    assert _warnings(result, 'NO_PATH_TO_GOAL')[0].relatedPlatformIds == ['platform_001', 'platform_004']


def test_start_warning_uses_same_nearest_candidate_for_id_and_distance(profile):
    level = _level([
        ('platform_001', 0, 160, 80, 160),
        ('platform_002', 20, 140, 80, 140),
        ('platform_004', 320, 70, 380, 70),
    ], start=(10, 140))

    warning = _warnings(analyze(level, profile), 'START_NOT_SUPPORTED')[0]

    assert warning.relatedPlatformIds == ['platform_002']
    assert warning.requiredValuePixels == 10


@pytest.mark.parametrize('start,goal,code,related,required,available', [
    ((20, 160), (330, 40, 30, 30), 'START_NOT_SUPPORTED', ['platform_001'], 20, 6),
    ((20, 180), (330, 20, 30, 30), 'GOAL_NOT_SUPPORTED', ['platform_004'], 20, 6),
])
def test_reports_unsupported_endpoint_with_pixel_diagnostics(profile, start, goal, code, related, required, available):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 320, 70, 380, 70),
    ], start=start, goal=goal)

    warning = _warnings(analyze(level, profile), code)[0]

    assert warning.relatedPlatformIds == related
    assert warning.requiredValuePixels == required
    assert warning.availableValuePixels == available


def test_goal_support_checks_full_sloped_overlap(profile):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 320, 80, 380, 60),
    ], goal=(330, 40, 30, 30))

    result = analyze(level, profile)

    assert result.goalPlatformId == 'platform_004'
    assert not _warnings(result, 'GOAL_NOT_SUPPORTED')


def test_goal_support_detects_internal_crossing_of_goal_bottom(profile):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 320, 90, 380, 50),
    ], goal=(330, 40, 30, 30))

    result = analyze(level, profile)

    assert result.goalPlatformId == 'platform_004'
    assert not _warnings(result, 'GOAL_NOT_SUPPORTED')


def test_goal_without_horizontal_overlap_omits_inapplicable_pixel_pair(profile):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 280, 70, 310, 70),
    ])

    warning = _warnings(analyze(level, profile), 'GOAL_NOT_SUPPORTED')[0]

    assert warning.requiredValuePixels is None
    assert warning.availableValuePixels is None


def test_reports_jump_rise_with_source_and_target_ids(profile):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 120, 70, 180, 70),
    ], goal=(130, 40, 30, 30))

    warning = _warnings(analyze(level, profile), 'JUMP_GAP_TOO_HIGH')[0]

    assert warning.relatedPlatformIds == ['platform_001', 'platform_004']
    assert warning.requiredValuePixels == 110
    assert warning.availableValuePixels == 60


def test_reports_jump_width_with_source_and_target_ids(profile):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 200, 180, 260, 180),
    ], goal=(210, 150, 30, 30))

    warning = _warnings(analyze(level, profile), 'JUMP_GAP_TOO_WIDE')[0]

    assert warning.relatedPlatformIds == ['platform_001', 'platform_004']
    assert warning.requiredValuePixels == 120
    assert warning.availableValuePixels == 106


def test_reports_short_landing_area(profile):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 120, 180, 145, 180),
    ], goal=(120, 150, 25, 30))

    warning = _warnings(analyze(level, profile), 'LANDING_AREA_TOO_SHORT')[0]

    assert warning.relatedPlatformIds == ['platform_004']
    assert warning.requiredValuePixels == 32
    assert warning.availableValuePixels == 26


def test_rejects_edge_when_only_one_pixel_of_target_is_reachable(profile):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 186, 180, 260, 180),
    ], goal=(220, 150, 30, 30))

    result = analyze(level, profile)
    warning = _warnings(result, 'LANDING_AREA_TOO_SHORT')[0]

    assert result.playability == 'unreachable'
    assert warning.relatedPlatformIds == ['platform_001', 'platform_004']
    assert warning.requiredValuePixels == 32
    assert warning.availableValuePixels == 1


def test_rejects_partially_overlapping_target_when_reachable_span_is_short(profile):
    level = _level([
        ('platform_001', 100, 180, 200, 180),
        ('platform_004', 0, 180, 120, 180),
    ], start=(180, 180), goal=(10, 150, 30, 30))
    narrow_profile = profile.model_copy(update={'maxJumpDistancePixels': 5, 'landingTolerancePixels': 0})

    result = analyze(level, narrow_profile)
    warning = [
        item for item in _warnings(result, 'LANDING_AREA_TOO_SHORT')
        if item.relatedPlatformIds == ['platform_001', 'platform_004']
    ][0]

    assert result.playability == 'unreachable'
    assert warning.requiredValuePixels == 32
    assert warning.availableValuePixels == 26


def test_downward_jump_does_not_apply_rise_limit(profile):
    level = _level([
        ('platform_001', 0, 70, 80, 70),
        ('platform_004', 120, 180, 180, 180),
    ], start=(20, 70), goal=(130, 150, 30, 30))

    result = analyze(level, profile)

    assert result.playability == 'playable'
    assert result.path == ['platform_001', 'platform_004']
    assert not [
        warning for warning in _warnings(result, 'JUMP_GAP_TOO_HIGH')
        if warning.relatedPlatformIds == ['platform_001', 'platform_004']
    ]


def test_slope_uses_target_landing_y_for_rise(profile):
    level = _level([
        ('platform_001', 0, 180, 80, 180),
        ('platform_004', 120, 130, 180, 70),
    ], goal=(150, 45, 30, 30))

    result = analyze(level, profile)

    assert result.playability == 'playable'
    assert result.path == ['platform_001', 'platform_004']
    assert not _warnings(result, 'JUMP_GAP_TOO_HIGH')
