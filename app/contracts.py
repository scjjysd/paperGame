"""前后端共享契约（P0 任务 1 要求）：Pydantic 模型 + 错误码。Unity GameContracts.cs 将来照此镜像。"""
import hashlib
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, field_validator, model_validator

JobState = Literal['queued', 'processing', 'needs_correction', 'ready', 'failed']
ErrorCode = Literal['FILE_TOO_LARGE', 'NOT_AN_IMAGE', 'UNSUPPORTED_FORMAT', 'JOB_NOT_FOUND',
                    'QUEUE_UNAVAILABLE', 'RENDER_TIMEOUT', 'RENDER_CRASHED', 'ASSET_MISSING', 'INTERNAL']
CorrectionReason = Literal['NO_HUMANOID', 'NO_SKELETON', 'MULTIPLE_SKELETONS', 'NO_CONTOUR',
                           'SKELETON_MISFIT', 'ANALYZE_FAILED']
NON_TERMINAL_STATES = ('queued', 'processing')
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# 错误码 -> 中文原因。响应体在 code 之外必须给出人能直接看懂的说明，
# 新增/修改 ErrorCode 时同步补文案，否则只能落到 UNKNOWN_MESSAGE 兜底。
UNKNOWN_MESSAGE = '请求处理失败，请查看服务端 out/logs 下的当天日志。'
ERROR_MESSAGES: Dict[str, str] = {
    'FILE_TOO_LARGE': '图片超过 10 MiB 上限，请压缩后重传。',
    'NOT_AN_IMAGE': '文件头不是已知图片格式，请上传 PNG 或 JPEG 图片。',
    'UNSUPPORTED_FORMAT': '暂不支持 GIF / WEBP，请上传 PNG 或 JPEG 图片。',
    'JOB_NOT_FOUND': '任务不存在或已过期，请重新上传原图。',
    'QUEUE_UNAVAILABLE': '任务队列（Redis）不可用，请稍后重试。',
    'RENDER_TIMEOUT': '单次渲染超过 120 秒，自动重试后仍失败，请重试或简化画面。',
    'RENDER_CRASHED': '渲染子进程异常退出或结果文件损坏，自动重试后仍失败。',
    'ASSET_MISSING': '渲染依赖的资产缺失，请检查服务端部署是否完整。',
    'INTERNAL': '服务端内部错误，请查看 out/logs 下的当天日志。',
}

# 任务状态 -> 中文说明。非终态只能靠它告诉客户端“现在到哪一步了”
STATUS_MESSAGES: Dict[str, str] = {
    'queued': '排队中，等待渲染 worker 领取。',
    'processing': '渲染中，正在生成 run / jump 精灵表。',
    'ready': '渲染完成，可按 spriteSheetUrl 下载精灵表。',
    'needs_correction': '需要修正：认不出人形或标注质量不过门禁，具体原因见 reason。',
    'failed': '渲染失败，属于技术故障，具体原因见 code。',
}

# needs_correction.reason -> 中文原因，客户端可直接展示给家长/孩子
REASON_MESSAGES: Dict[str, str] = {
    'NO_HUMANOID': '画面里没有找到人形，请只画一个完整的火柴人后重传。',
    'NO_SKELETON': '找到了人形但推不出骨架，请确保头、躯干与四肢线条清晰相连。',
    'MULTIPLE_SKELETONS': '画面里检出多个人形，请只保留一个主角后重传。',
    'NO_CONTOUR': '提不出闭合轮廓，请检查线条是否断裂或过于潦草。',
    'SKELETON_MISFIT': '关节位置偏离墨迹过远，请对照 /view 页面的关节叠加图重画。',
    'ANALYZE_FAILED': '标注服务调用失败，请稍后重试；持续失败请检查 TorchServe 是否健康。',
}


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
    message: str = ''   # reason 的中文说明，未显式传入时自动查表补齐
    maskUrl: str
    joints: List[Joint]

    @field_validator('joints')
    @classmethod
    def _sixteen_joints(cls, v):
        # 早期失败（NO_HUMANOID/NO_CONTOUR 等）时 char_cfg.yaml 不存在，无可编辑标注，joints 为空数组
        if len(v) != 16 and len(v) != 0:
            raise ValueError(f'joints must have exactly 16 items or be empty (early-failure), got {len(v)}')
        return v

    @model_validator(mode='after')
    def _fill_message(self):
        self.message = self.message or REASON_MESSAGES.get(self.reason, UNKNOWN_MESSAGE)
        return self


class JobFailed(BaseModel):
    status: Literal['failed'] = 'failed'
    code: ErrorCode
    message: str = ''   # code 的中文说明

    @model_validator(mode='after')
    def _fill_message(self):
        self.message = self.message or ERROR_MESSAGES.get(self.code, UNKNOWN_MESSAGE)
        return self


class JobAccepted(BaseModel):
    jobId: str
    # 完整地址（含 scheme + host），客户端不必再拼 Base URL；/view 可直接浏览器打开预览
    statusUrl: str
    viewUrl: str


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str = ''   # code 的中文说明

    @model_validator(mode='after')
    def _fill_message(self):
        self.message = self.message or ERROR_MESSAGES.get(self.code, UNKNOWN_MESSAGE)
        return self
