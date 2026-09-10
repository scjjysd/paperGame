"""关卡图片异步解析接口：上传校验、幂等入队与状态查询。"""
import io
import json
import logging
import threading
import warnings
from pathlib import Path
from typing import Any, Optional

import redis.exceptions
from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, ImageFile, ImageOps
from pydantic import ValidationError

from app.api import errors, urls
from app.level_contracts import (DEFAULT_PLAYABILITY_PROFILE, LEVEL_STAGE_MESSAGES,
                                 LEVEL_STATUS_MESSAGES, LevelFailed, LevelNeedsFix,
                                 LevelNeedsReview, LevelReady, PlayabilityProfile,
                                 derive_level_job_id)
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/v1/levels', tags=['levels'])
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MIN_SIDE = 800
MAX_SIDE = 12000
MAX_PIXELS = 40_000_000
SUPPORTED_SCHEMA_VERSION = '1.0'
LEVEL_TERMINAL_STATES = frozenset({'ready', 'needs_fix', 'needs_review', 'failed'})
LEVEL_RERUN_ARTIFACTS = (
    'result.json', 'rectified.png', 'paper-mask.png', 'ink-mask.png', 'overlay.png',
    'level.json', 'analysis.json', 'transform.json', 'llm-audit.json', 'request.json',
    'result.json.tmp', 'rectified.tmp.png', 'paper-mask.tmp.png', 'transform.tmp.json',
)
TERMINAL_MODELS = {
    'ready': LevelReady, 'needs_fix': LevelNeedsFix,
    'needs_review': LevelNeedsReview, 'failed': LevelFailed,
}
# _normalize_image 的拒收原因是内部控制流字符串（英文、不得改），对外统一映射成中文说明
IMAGE_REJECT_MESSAGES = {
    'unsupported image format': '文件头不是 JPEG 或 PNG，请转成静态图后重传。',
    'animated image': '图片是多帧动图，只接受单帧静态图。',
    'invalid dimensions': '图片边长必须在 {}~{} 像素之间。'.format(MIN_SIDE, MAX_SIDE),
    'too many pixels': '图片像素总数超过 {} 万，请降低分辨率。'.format(MAX_PIXELS // 10000),
    # Pillow 的解压炸弹门禁先于上面的像素判定触发，必须单独给一句准确说明
    'decompression bomb': '图片像素数超过安全上限（{} 万），已拒绝解码。'.format(MAX_PIXELS // 10000),
    'image decode failed': '图片无法解码，文件可能已损坏或被截断。',
}
_PILLOW_SETTINGS_LOCK = threading.Lock()


def _error(status_code: int, code: str, message: Optional[str] = None,
           retryable: bool = False, details=None) -> JSONResponse:
    """关卡链路错误体；不传 message 时自动取 LEVEL_ERROR_MESSAGES 里的中文原因。"""
    return errors.level_error(status_code, code, message, retryable, details)


def _image_reject_message(reason: str) -> str:
    return IMAGE_REJECT_MESSAGES.get(reason, IMAGE_REJECT_MESSAGES['image decode failed'])


def _status_body(job_id: str, status: str, base_url: str, **extra) -> dict:
    """状态响应统一带上中文说明与完整地址的轮询/预览链接。"""
    body = {'jobId': job_id, 'status': status,
            'message': LEVEL_STATUS_MESSAGES.get(status, ''),
            'statusUrl': urls.absolute_url(base_url, urls.level_status_path(job_id)),
            'viewUrl': urls.absolute_url(base_url, urls.level_view_path(job_id))}
    body.update(extra)
    return urls.absolutize(body, base_url)


def _timestamps(data: dict, *fields: str) -> dict:
    """时间戳缺失时干脆不写进响应。

    Redis 过期后靠磁盘快照重建时，旧快照可能根本没这两个字段；
    给出 "createdAt": null 会让强类型客户端反序列化失败，缺字段比空值安全。
    """
    return {name: data[name] for name in fields if data.get(name)}


def _normalize_image(content: bytes) -> bytes:
    if not (content.startswith(b'\x89PNG\r\n\x1a\n') or content.startswith(b'\xff\xd8')):
        raise ValueError('unsupported image format')
    with _PILLOW_SETTINGS_LOCK:
        old_limit, old_truncated = Image.MAX_IMAGE_PIXELS, ImageFile.LOAD_TRUNCATED_IMAGES
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                try:
                    with Image.open(io.BytesIO(content)) as checked:
                        checked.verify()
                    with Image.open(io.BytesIO(content)) as image:
                        if getattr(image, 'n_frames', 1) != 1:
                            raise ValueError('animated image')
                        if not (MIN_SIDE <= image.width <= MAX_SIDE and MIN_SIDE <= image.height <= MAX_SIDE):
                            raise ValueError('invalid dimensions')
                        if image.width * image.height > MAX_PIXELS:
                            raise ValueError('too many pixels')
                        image.load()
                        normalized = ImageOps.exif_transpose(image)
                        mode = 'RGBA' if ('A' in normalized.getbands() or normalized.mode in ('P', 'LA')) else 'RGB'
                        output = io.BytesIO()
                        normalized.convert(mode).save(output, format='PNG', optimize=False)
                        return output.getvalue()
                except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                    raise ValueError('decompression bomb') from exc
                except ValueError as exc:
                    # 自己的门禁判定原样抛出，对外才能给出精确的中文原因；其余归为解码失败
                    if str(exc) in IMAGE_REJECT_MESSAGES:
                        raise
                    raise ValueError('image decode failed') from exc
                except OSError as exc:
                    raise ValueError('image decode failed') from exc
        finally:
            Image.MAX_IMAGE_PIXELS, ImageFile.LOAD_TRUNCATED_IMAGES = old_limit, old_truncated


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')), encoding='utf-8')
    temporary.replace(path)


def _profile(raw: Optional[str]) -> PlayabilityProfile:
    if raw is None or raw == '':
        return DEFAULT_PLAYABILITY_PROFILE
    try:
        return PlayabilityProfile.model_validate_json(raw)
    except (ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise ValueError('invalid profile') from exc


@router.post('', status_code=202)
async def create_level(request: Request, force: bool = Query(False),
                       schemaVersion: str = Form('1.0'),
                       playabilityProfile: Optional[str] = Form(None),
                       file: UploadFile = File(...)):
    base_url = urls.public_base_url(request)
    if schemaVersion != SUPPORTED_SCHEMA_VERSION:
        logger.warning('关卡上传被拒绝：schemaVersion=%s，服务端只接受 %s',
                       schemaVersion, SUPPORTED_SCHEMA_VERSION)
        return _error(400, 'UNSUPPORTED_SCHEMA_VERSION')
    try:
        profile = _profile(playabilityProfile)
    except ValueError:
        logger.warning('关卡上传被拒绝：playabilityProfile 不合法（%s）', playabilityProfile)
        return _error(400, 'INVALID_PLAYABILITY_PROFILE')
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        logger.warning('关卡上传被拒绝：图片超过 %d MiB 上限（文件名 %s）',
                       MAX_UPLOAD_BYTES // (1024 * 1024), file.filename)
        return _error(413, 'FILE_TOO_LARGE', details={'limitBytes': MAX_UPLOAD_BYTES})
    try:
        normalized = _normalize_image(content)
    except ValueError as exc:
        reason = str(exc)
        code = 'UNSUPPORTED_IMAGE_FORMAT' if reason == 'unsupported image format' else 'IMAGE_DECODE_FAILED'
        status_code = 415 if code == 'UNSUPPORTED_IMAGE_FORMAT' else 422
        logger.warning('关卡上传被拒绝：%s（文件名 %s，大小 %d 字节）',
                       _image_reject_message(reason), file.filename, len(content))
        return _error(status_code, code, _image_reject_message(reason),
                      details={'reason': reason})

    job_id = derive_level_job_id(content, profile)
    store: JobStore = request.app.state.level_store
    try:
        existing = store.get(job_id)
        if existing is not None and not force:
            logger.info('关卡任务 %s 已存在（状态 %s），按幂等直接返回，未重复入队',
                        job_id, existing.get('status'))
            return _status_body(job_id, existing.get('status', 'queued'), base_url,
                                **_timestamps(existing, 'createdAt'))
        if existing is not None and existing.get('status') not in LEVEL_TERMINAL_STATES:
            logger.warning('关卡任务 %s 强制重跑被拒绝：当前状态 %s 尚未进入终态',
                           job_id, existing.get('status'))
            return _error(409, 'JOB_IN_PROGRESS', retryable=True)
        job_dir = store.jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / 'input.png').write_bytes(normalized)
        if existing is not None:
            logger.info('关卡任务 %s 强制重跑：已清理旧终态与产物（原状态 %s），重新入队',
                        job_id, existing.get('status'))
            store.reset(job_id)
        else:
            logger.info('关卡任务 %s 已创建并入队，归一化后 %d 字节 -> %s',
                        job_id, len(normalized), job_dir / 'input.png')
            store.create(job_id)
        created = store.get(job_id) or {}
        _atomic_json(job_dir / 'request.json', {
            'playabilityProfile': profile.model_dump(),
            'createdAt': created.get('createdAt'),
            'updatedAt': created.get('updatedAt'),
        })
        store.enqueue(job_id)
    except redis.exceptions.RedisError:
        logger.error('关卡任务 %s 入队失败：任务队列（Redis）不可用', job_id, exc_info=True)
        return _error(503, 'QUEUE_UNAVAILABLE', retryable=True)
    return _status_body(job_id, 'queued', base_url, createdAt=created.get('createdAt'))


@router.get('/{job_id}')
def get_level(job_id: str, request: Request):
    store: JobStore = request.app.state.level_store
    base_url = urls.public_base_url(request)
    try:
        data = store.get(job_id)
    except redis.exceptions.RedisError:
        logger.error('查询关卡任务 %s 失败：任务队列（Redis）不可用', job_id, exc_info=True)
        return _error(503, 'QUEUE_UNAVAILABLE', retryable=True)
    if data is None:
        logger.warning('查询关卡任务 %s 失败：任务不存在且无可用磁盘快照', job_id)
        return _error(404, 'JOB_NOT_FOUND')
    if 'result' in data:
        try:
            payload = json.loads(data['result'])
            model = TERMINAL_MODELS.get(payload.get('status'))
            if model is None:
                raise ValueError('invalid terminal status')
            envelope = model.model_validate(payload).model_dump()
        except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            logger.warning('关卡任务 %s 的终态快照不符合契约，当作不存在处理：%s', job_id, exc)
            return _error(404, 'JOB_NOT_FOUND')
        logger.debug('关卡任务 %s 命中终态 %s', job_id, envelope.get('status'))
        return _status_body(job_id, envelope['status'], base_url,
                            **{k: v for k, v in envelope.items() if k not in ('jobId', 'status')})
    status = data.get('status')
    stage = data.get('stage', 'waiting')
    logger.debug('关卡任务 %s 处于非终态 %s（阶段 %s）', job_id, status, stage)
    return _status_body(job_id, status, base_url,
                        progress={'stage': stage,
                                  'stageLabel': LEVEL_STAGE_MESSAGES.get(stage, stage),
                                  'percent': 0 if status == 'queued' else 55},
                        createdAt=data.get('createdAt'), updatedAt=data.get('updatedAt'))
