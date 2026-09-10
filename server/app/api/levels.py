"""关卡图片异步解析接口：上传校验、幂等入队与状态查询。"""
import io
import json
import threading
import uuid
import warnings
from pathlib import Path
from typing import Any, Optional

import redis.exceptions
from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, ImageFile, ImageOps
from pydantic import ValidationError

from app.level_contracts import (DEFAULT_PLAYABILITY_PROFILE, LevelFailed, LevelNeedsFix,
                                 LevelNeedsReview, LevelReady, PlayabilityProfile,
                                 derive_level_job_id)
from app.services.job_store import JobStore

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
_PILLOW_SETTINGS_LOCK = threading.Lock()


def _request_id() -> str:
    return 'req_' + uuid.uuid4().hex[:24]


def _error(status_code: int, code: str, message: str, retryable: bool = False, details=None) -> JSONResponse:
    body = {'code': code, 'message': message, 'retryable': retryable, 'requestId': _request_id()}
    if details is not None:
        body['details'] = details
    return JSONResponse(status_code=status_code, content={'error': body})


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
                except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError) as exc:
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
    if schemaVersion != SUPPORTED_SCHEMA_VERSION:
        return _error(400, 'UNSUPPORTED_SCHEMA_VERSION', '服务端不支持请求版本。')
    try:
        profile = _profile(playabilityProfile)
    except ValueError:
        return _error(400, 'INVALID_PLAYABILITY_PROFILE', '角色能力参数缺失或越界。')
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        return _error(413, 'FILE_TOO_LARGE', '图片不能超过 10 MiB。', details={'limitBytes': MAX_UPLOAD_BYTES})
    try:
        normalized = _normalize_image(content)
    except ValueError as exc:
        if str(exc) == 'unsupported image format':
            return _error(415, 'UNSUPPORTED_IMAGE_FORMAT', '仅支持 JPEG 或 PNG。')
        return _error(422, 'IMAGE_DECODE_FAILED', '图片无法安全解码。')

    job_id = derive_level_job_id(content, profile)
    store: JobStore = request.app.state.level_store
    try:
        existing = store.get(job_id)
        if existing is not None and not force:
            body = {'jobId': job_id, 'status': existing.get('status', 'queued'), 'statusUrl': f'/v1/levels/{job_id}'}
            if existing.get('createdAt'):
                body['createdAt'] = existing['createdAt']
            return body
        if existing is not None and existing.get('status') not in LEVEL_TERMINAL_STATES:
            return _error(409, 'JOB_IN_PROGRESS', '任务正在处理中，不能强制重跑。', retryable=True)
        job_dir = store.jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / 'input.png').write_bytes(normalized)
        if existing is not None:
            store.reset(job_id)
        else:
            store.create(job_id)
        created = store.get(job_id) or {}
        _atomic_json(job_dir / 'request.json', {
            'playabilityProfile': profile.model_dump(),
            'createdAt': created.get('createdAt'),
            'updatedAt': created.get('updatedAt'),
        })
        store.enqueue(job_id)
    except redis.exceptions.RedisError:
        return _error(503, 'QUEUE_UNAVAILABLE', '任务队列暂不可用。', retryable=True)
    return {'jobId': job_id, 'status': 'queued', 'statusUrl': f'/v1/levels/{job_id}', 'createdAt': created.get('createdAt')}


@router.get('/{job_id}')
def get_level(job_id: str, request: Request):
    store: JobStore = request.app.state.level_store
    try:
        data = store.get(job_id)
    except redis.exceptions.RedisError:
        return _error(503, 'QUEUE_UNAVAILABLE', '任务队列暂不可用。', retryable=True)
    if data is None:
        return _error(404, 'JOB_NOT_FOUND', '任务不存在或已过期。')
    if 'result' in data:
        try:
            payload = json.loads(data['result'])
            model = TERMINAL_MODELS.get(payload.get('status'))
            if model is None:
                raise ValueError('invalid terminal status')
            return model.model_validate(payload).model_dump()
        except (TypeError, ValueError, json.JSONDecodeError, ValidationError):
            return _error(404, 'JOB_NOT_FOUND', '任务不存在或已过期。')
    status = data.get('status')
    return {'jobId': job_id, 'status': status,
            'progress': {'stage': data.get('stage', 'waiting'), 'percent': 0 if status == 'queued' else 55},
            'createdAt': data.get('createdAt'), 'updatedAt': data.get('updatedAt')}
