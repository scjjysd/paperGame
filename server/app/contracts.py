"""前后端共享契约（P0 任务 1 要求）：Pydantic 模型 + 错误码。Unity GameContracts.cs 将来照此镜像。"""
import hashlib
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, field_validator

JobState = Literal['queued', 'processing', 'needs_correction', 'ready', 'failed']
ErrorCode = Literal['FILE_TOO_LARGE', 'NOT_AN_IMAGE', 'UNSUPPORTED_FORMAT', 'JOB_NOT_FOUND',
                    'QUEUE_UNAVAILABLE', 'RENDER_TIMEOUT', 'RENDER_CRASHED', 'ASSET_MISSING', 'INTERNAL']
CorrectionReason = Literal['NO_HUMANOID', 'NO_SKELETON', 'MULTIPLE_SKELETONS', 'NO_CONTOUR', 'ANALYZE_FAILED']
NON_TERMINAL_STATES = ('queued', 'processing')
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def derive_job_id(content: bytes) -> str:
    return 'char_' + hashlib.sha256(content).hexdigest()[:12]


class FootAnchor(BaseModel):
    x: int
    y: int


class AnimationMeta(BaseModel):
    spriteSheetUrl: str
    frameCount: int
    fps: int
    frameWidth: int
    frameHeight: int
    footAnchor: FootAnchor


class CharacterReady(BaseModel):
    status: Literal['ready'] = 'ready'
    characterId: str
    animations: Dict[str, AnimationMeta]

    @field_validator('animations')
    @classmethod
    def _both_motions(cls, v):
        if set(v) != {'run', 'jump'}:
            raise ValueError("animations must contain exactly 'run' and 'jump'")
        return v


class Joint(BaseModel):
    name: str
    loc: List[int]
    parent: Optional[str] = None


class NeedsCorrection(BaseModel):
    status: Literal['needs_correction'] = 'needs_correction'
    reason: CorrectionReason
    maskUrl: str
    joints: List[Joint]

    @field_validator('joints')
    @classmethod
    def _sixteen_joints(cls, v):
        # 早期失败（NO_HUMANOID/NO_CONTOUR 等）时 char_cfg.yaml 不存在，无可编辑标注，joints 为空数组
        if len(v) != 16 and len(v) != 0:
            raise ValueError(f'joints must have exactly 16 items or be empty (early-failure), got {len(v)}')
        return v


class JobFailed(BaseModel):
    status: Literal['failed'] = 'failed'
    code: ErrorCode


class JobAccepted(BaseModel):
    jobId: str


class ErrorBody(BaseModel):
    code: ErrorCode
