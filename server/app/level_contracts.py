"""Level API v1 的 Pydantic v2 契约、几何校验和稳定任务 ID。"""

import hashlib
import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = '1.0'
ALGORITHM_VERSION = 'level-parser-1.0.0'
ALGORITHM_MAJOR_VERSION = '1'


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
