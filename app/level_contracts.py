"""Level API v1 的 Pydantic v2 契约、几何校验和稳定任务 ID。"""

import hashlib
import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = '1.0'
ALGORITHM_VERSION = 'level-parser-1.0.0'
ALGORITHM_MAJOR_VERSION = '1'

# 错误码 -> 中文原因：响应体除了 code 还必须给出人能直接看懂的说明。
# worker 写 result.json 与 API 即时报错共用本表，避免同一码在两处文案不一致。
UNKNOWN_LEVEL_MESSAGE = '关卡解析失败，请查看服务端 out/logs 下的当天日志。'
LEVEL_ERROR_MESSAGES = {
    'UNSUPPORTED_SCHEMA_VERSION': '服务端不支持该 schemaVersion，当前只接受 1.0。',
    'INVALID_PLAYABILITY_PROFILE': '角色能力参数缺失或越界，请对照文档校验 playabilityProfile。',
    'FILE_TOO_LARGE': '图片不能超过 10 MiB，请压缩后重传。',
    'UNSUPPORTED_IMAGE_FORMAT': '仅支持 JPEG 或 PNG，请转换格式后重传。',
    'IMAGE_DECODE_FAILED': '图片无法安全解码：可能已损坏、边长不在 800~12000 像素之间、超过 4000 万像素或为动图。',
    'JOB_IN_PROGRESS': '任务正在处理中，不能强制重跑，请等当前任务进入终态。',
    'QUEUE_UNAVAILABLE': '任务队列（Redis）不可用，请稍后重试。',
    'JOB_NOT_FOUND': '任务不存在或已过期，请重新上传关卡图。',
    'PROCESSING_TIMEOUT': '关卡解析超时（单次 90 秒，已自动重试一次）。',
    'PROCESSING_CRASHED': '关卡解析进程异常退出或结果不符合契约，已自动重试一次。',
    'INTERNAL': '服务端内部错误，请查看 out/logs 下的当天日志。',
}

# 任务状态 -> 中文说明，审查页与日志统一使用
LEVEL_STATUS_MESSAGES = {
    'queued': '排队中，等待关卡 worker 领取。',
    'processing': '解析中，正在拉正纸张、识别平台与终点。',
    'ready': '解析完成，关卡可玩。',
    'needs_fix': '解析完成，但存在可玩性问题（跳不过去或终点悬空）。',
    'needs_review': '识别证据不足，需要人工复核或重拍。',
    'failed': '解析失败，属于技术故障，可重试。',
}

# 解析阶段 -> 中文说明（stage 本身是契约值，保持英文不变）
LEVEL_STAGE_MESSAGES = {
    'waiting': '等待中',
    'validating_upload': '校验上传图',
    'rectifying_paper': '拉正纸张',
    'detecting_platforms': '识别平台',
    'detecting_goal': '识别终点',
    'semantic_review': '语义复核',
    'validating_geometry': '校验几何',
    'analyzing_playability': '分析可玩性',
    'publishing_artifacts': '发布产物',
}


def _artifact_url(value: str) -> str:
    if not value.startswith('/artifacts/'):
        raise ValueError('artifact URL must use the /artifacts/level_ prefix')
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Point(ContractModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class Region(ContractModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)


class Canvas(ContractModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class CoordinateSystem(ContractModel):
    origin: Literal['top_left']
    xAxis: Literal['right']
    yAxis: Literal['down']
    unit: Literal['pixel']


class Background(ContractModel):
    imageUrl: str
    contentType: Literal['image/png', 'image/jpeg']
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('imageUrl')
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _artifact_url(value)


class PlayerStart(Point):
    source: Literal['detected', 'inferred', 'reviewed']
    confidence: float = Field(ge=0, le=1)


class Platform(ContractModel):
    id: str = Field(min_length=1)
    start: Point
    end: Point
    confidence: float = Field(ge=0, le=1)


class Level(ContractModel):
    schemaVersion: Literal[SCHEMA_VERSION]
    coordinateSystem: CoordinateSystem
    canvas: Canvas
    background: Background
    playerStart: PlayerStart
    platforms: List[Platform] = Field(min_length=1)
    goalRegion: Region

    @model_validator(mode='after')
    def validate_geometry(self):
        if self.background.width != self.canvas.width or self.background.height != self.canvas.height:
            raise ValueError('background dimensions must match canvas dimensions')

        def inside(point: Point) -> bool:
            return point.x < self.canvas.width and point.y < self.canvas.height

        if not inside(self.playerStart):
            raise ValueError('playerStart must be inside canvas')
        if not inside(Point(x=self.goalRegion.x, y=self.goalRegion.y)):
            raise ValueError('goalRegion origin must be inside canvas')
        if self.goalRegion.x + self.goalRegion.width > self.canvas.width or self.goalRegion.y + self.goalRegion.height > self.canvas.height:
            raise ValueError('goalRegion must be fully inside canvas')

        ids = [platform.id for platform in self.platforms]
        if len(ids) != len(set(ids)):
            raise ValueError('platform ids must be unique')
        for platform in self.platforms:
            if platform.start.x > platform.end.x:
                raise ValueError('platform start.x must be <= end.x')
            if platform.start == platform.end:
                raise ValueError('platform must not have zero length')
            if not inside(platform.start) or not inside(platform.end):
                raise ValueError('platform endpoints must be inside canvas')
        return self


class PlayabilityProfile(ContractModel):
    profileVersion: str = Field(min_length=1)
    maxJumpRisePixels: int = Field(gt=0)
    maxJumpDistancePixels: int = Field(gt=0)
    characterWidthPixels: int = Field(gt=0)
    characterHeightPixels: int = Field(gt=0)
    landingTolerancePixels: int = Field(ge=0)


DEFAULT_PLAYABILITY_PROFILE = PlayabilityProfile(
    profileVersion='unity-c1-test-1',
    maxJumpRisePixels=150,
    maxJumpDistancePixels=230,
    characterWidthPixels=32,
    characterHeightPixels=58,
    landingTolerancePixels=6,
)


class Warning(ContractModel):
    code: str
    message: str
    relatedPlatformIds: List[str] = Field(default_factory=list)
    requiredValuePixels: Optional[int] = None
    availableValuePixels: Optional[int] = None


class PlayabilityAnalysis(ContractModel):
    playability: Literal['playable', 'unreachable']
    profile: PlayabilityProfile
    startPlatformId: Optional[str] = None
    goalPlatformId: Optional[str] = None
    path: List[str] = Field(default_factory=list)
    warnings: List[Warning] = Field(default_factory=list)


class Artifacts(ContractModel):
    inputUrl: Optional[str] = None
    rectifiedImageUrl: str
    overlayImageUrl: str
    levelJsonUrl: Optional[str] = None
    analysisJsonUrl: Optional[str] = None

    @model_validator(mode='after')
    def validate_urls(self):
        for value in (self.inputUrl, self.rectifiedImageUrl, self.overlayImageUrl, self.levelJsonUrl, self.analysisJsonUrl):
            if value is not None:
                _artifact_url(value)
        return self


class ReviewCandidate(ContractModel):
    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    region: Region
    confidence: float = Field(ge=0, le=1)


class Review(ContractModel):
    reason: str
    message: str
    candidates: List[ReviewCandidate] = Field(min_length=1)
    suggestions: List[str] = Field(default_factory=list)


class LevelResult(ContractModel):
    level: Level
    analysis: PlayabilityAnalysis
    artifacts: Artifacts


class ErrorDetail(ContractModel):
    code: str
    message: str
    retryable: bool
    requestId: str
    details: Optional[Dict[str, Any]] = None


class ErrorEnvelope(ContractModel):
    error: ErrorDetail


class TerminalEnvelope(ContractModel):
    jobId: str
    schemaVersion: Literal[SCHEMA_VERSION]
    algorithmVersion: Literal[ALGORITHM_VERSION]
    createdAt: str
    updatedAt: str


class LevelReady(TerminalEnvelope):
    status: Literal['ready']
    result: LevelResult


class LevelNeedsFix(TerminalEnvelope):
    status: Literal['needs_fix']
    result: LevelResult


class LevelNeedsReview(TerminalEnvelope):
    status: Literal['needs_review']
    review: Review
    artifacts: Artifacts


class LevelFailed(ContractModel):
    jobId: str
    status: Literal['failed']
    error: ErrorDetail
    createdAt: str
    updatedAt: str


def canonical_profile_json(profile: PlayabilityProfile) -> bytes:
    return json.dumps(profile.model_dump(), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def derive_level_job_id(content: bytes, profile: PlayabilityProfile) -> str:
    digest = hashlib.sha256(content + canonical_profile_json(profile) + ALGORITHM_MAJOR_VERSION.encode('ascii')).hexdigest()
    return 'level_' + digest[:12]
