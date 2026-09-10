import hashlib

import pytest
from pydantic import ValidationError

from app.contracts import (
    ERROR_MESSAGES, REASON_MESSAGES, AnimationMeta, CharacterReady, ErrorBody, FootAnchor,
    JobAccepted, JobFailed, NeedsCorrection, derive_job_id,
)


def test_derive_job_id_deterministic_and_prefixed():
    content = b'fake-png-bytes'
    jid = derive_job_id(content)
    assert jid == 'char_' + hashlib.sha256(content).hexdigest()[:12]
    assert derive_job_id(content) == jid


def test_derive_job_id_different_content_different_id():
    assert derive_job_id(b'a') != derive_job_id(b'b')


def test_animation_meta_contract_keys():
    meta = AnimationMeta(
        spriteSheetUrl='/artifacts/char_x/run.png', frameCount=13, fps=12,
        frameWidth=481, frameHeight=655, footAnchor=FootAnchor(x=240, y=655))
    dumped = meta.model_dump()
    assert set(dumped) == {'spriteSheetUrl', 'frameCount', 'fps', 'frameWidth', 'frameHeight', 'footAnchor'}
    assert dumped['footAnchor'] == {'x': 240, 'y': 655}


def test_character_ready_requires_both_motions():
    meta = dict(spriteSheetUrl='/a/run.png', frameCount=13, fps=12,
                frameWidth=481, frameHeight=655, footAnchor={'x': 240, 'y': 655})
    ready = CharacterReady(status='ready', characterId='char_x',
                           animations={'run': meta, 'jump': {**meta, 'spriteSheetUrl': '/a/jump.png'}})
    assert set(ready.animations) == {'run', 'jump'}
    with pytest.raises(ValidationError):
        CharacterReady(status='ready', characterId='char_x', animations={'run': meta})


def test_needs_correction_requires_16_joints():
    joints = [{'name': f'j{i}', 'loc': [i, i], 'parent': None} for i in range(16)]
    nc = NeedsCorrection(status='needs_correction', reason='NO_HUMANOID',
                         maskUrl='/artifacts/char_x/anno/mask.png', joints=joints)
    assert len(nc.joints) == 16
    with pytest.raises(ValidationError):
        NeedsCorrection(status='needs_correction', reason='NO_HUMANOID',
                        maskUrl='/m.png', joints=joints[:15])


def test_needs_correction_allows_empty_joints_for_early_failure():
    # 早期失败（NO_HUMANOID/NO_CONTOUR）无 char_cfg.yaml，无可编辑标注 → joints 空数组合法
    nc = NeedsCorrection(status='needs_correction', reason='NO_HUMANOID',
                         maskUrl='/artifacts/char_x/anno/mask.png', joints=[])
    assert nc.joints == []


def test_needs_correction_fills_chinese_reason():
    """只给 reason 时必须自动补中文原因，客户端可以直接展示。"""
    nc = NeedsCorrection(status='needs_correction', reason='SKELETON_MISFIT',
                         maskUrl='/m.png', joints=[])
    assert nc.message == REASON_MESSAGES['SKELETON_MISFIT']


def test_job_accepted_and_failed_shapes():
    accepted = JobAccepted(jobId='char_x', statusUrl='http://h:8000/v1/characters/char_x',
                           viewUrl='http://h:8000/v1/characters/char_x/view')
    assert accepted.model_dump() == {
        'jobId': 'char_x',
        'statusUrl': 'http://h:8000/v1/characters/char_x',
        'viewUrl': 'http://h:8000/v1/characters/char_x/view',
    }
    failed = JobFailed(status='failed', code='RENDER_TIMEOUT').model_dump()
    assert failed['status'] == 'failed' and failed['code'] == 'RENDER_TIMEOUT'
    assert failed['message'] == ERROR_MESSAGES['RENDER_TIMEOUT']


def test_error_body_carries_chinese_message():
    assert ErrorBody(code='FILE_TOO_LARGE').model_dump() == {
        'code': 'FILE_TOO_LARGE', 'message': ERROR_MESSAGES['FILE_TOO_LARGE']}
    # 显式传入的文案不被查表覆盖
    assert ErrorBody(code='INTERNAL', message='自定义').message == '自定义'


def test_every_error_code_and_reason_has_chinese_message():
    """新增错误码忘补文案就只能落到兜底说明，此处把它卡住。"""
    from app.contracts import CorrectionReason, ErrorCode

    for code in ErrorCode.__args__:
        assert ERROR_MESSAGES.get(code), code
    for reason in CorrectionReason.__args__:
        assert REASON_MESSAGES.get(reason), reason
