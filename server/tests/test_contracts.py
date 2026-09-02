import hashlib

import pytest
from pydantic import ValidationError

from app.contracts import (
    AnimationMeta, CharacterReady, FootAnchor, JobAccepted, JobFailed,
    NeedsCorrection, derive_job_id,
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


def test_job_accepted_and_failed_shapes():
    assert JobAccepted(jobId='char_x').model_dump() == {'jobId': 'char_x'}
    assert JobFailed(status='failed', code='RENDER_TIMEOUT').model_dump() == {'status': 'failed', 'code': 'RENDER_TIMEOUT'}
