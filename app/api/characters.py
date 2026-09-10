"""角色任务接口：POST 上传入队（幂等，force=true 可强制重跑），GET 轮询五态。"""
import json
import logging
from typing import Optional

import redis.exceptions
from fastapi import APIRouter, File, Query, Request, UploadFile

from app import contracts
from app.api import errors, urls
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/v1/characters', tags=['characters'])

PNG_MAGIC = b'\x89PNG'
JPEG_MAGIC = b'\xff\xd8'
GIF_MAGIC = b'GIF8'
WEBP_MAGIC = b'RIFF'


def _sniff(head: bytes) -> Optional[str]:
    """返回 'ok' | 'unsupported' | 'not_image'。仅接受 PNG/JPEG（P0 锁定）。注意：Python 3.9 不支持 `str | None` 运行时注解，必须用 Optional。"""
    if head.startswith(PNG_MAGIC) or head.startswith(JPEG_MAGIC):
        return 'ok'
    if head.startswith(GIF_MAGIC) or head.startswith(WEBP_MAGIC):
        return 'unsupported'
    return 'not_image'


def _accepted(job_id: str, base_url: str) -> dict:
    """入队响应：jobId + 完整地址的状态/预览链接，客户端不必再拼 Base URL。"""
    return contracts.JobAccepted(
        jobId=job_id,
        statusUrl=urls.absolute_url(base_url, urls.character_status_path(job_id)),
        viewUrl=urls.absolute_url(base_url, urls.character_view_path(job_id)),
    ).model_dump()


def _status_body(job_id: str, status: str, base_url: str, **extra) -> dict:
    body = {'status': status,
            'message': contracts.STATUS_MESSAGES.get(status, ''),
            'statusUrl': urls.absolute_url(base_url, urls.character_status_path(job_id)),
            'viewUrl': urls.absolute_url(base_url, urls.character_view_path(job_id))}
    body.update(extra)
    return urls.absolutize(body, base_url)


@router.post('', status_code=202)
async def create_character(request: Request, force: bool = Query(False),
                           file: UploadFile = File(...)):
    store: JobStore = request.app.state.store
    base_url = urls.public_base_url(request)
    content = await file.read(contracts.MAX_UPLOAD_BYTES + 1)
    if len(content) > contracts.MAX_UPLOAD_BYTES:
        logger.warning('角色上传被拒绝：文件超过 %d 字节上限（文件名 %s）',
                       contracts.MAX_UPLOAD_BYTES, file.filename)
        return errors.character_error(400, 'FILE_TOO_LARGE')
    verdict = _sniff(content[:4])
    if verdict != 'ok':
        code = 'UNSUPPORTED_FORMAT' if verdict == 'unsupported' else 'NOT_AN_IMAGE'
        logger.warning('角色上传被拒绝：%s（文件名 %s，大小 %d 字节）',
                       contracts.ERROR_MESSAGES[code], file.filename, len(content))
        return errors.character_error(400, code)

    job_id = contracts.derive_job_id(content)
    try:
        existing = store.get(job_id)
        if existing is not None and not force:
            # 非终态：不重复入队；终态：客户端轮询即可看到结果
            logger.info('角色任务 %s 已存在（状态 %s），按幂等直接返回，未重复入队',
                        job_id, existing.get('status'))
            return _accepted(job_id, base_url)
        job_dir = store.jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / 'input.png').write_bytes(content)
        if existing is not None:
            logger.info('角色任务 %s 强制重跑：已清理旧终态与产物（原状态 %s），重新入队',
                        job_id, existing.get('status'))
            store.reset(job_id)   # force：清旧终态结果与产物后重新入队
        else:
            logger.info('角色任务 %s 已创建并入队，原图 %d 字节 -> %s',
                        job_id, len(content), job_dir / 'input.png')
            store.create(job_id)
        store.enqueue(job_id)
    except redis.exceptions.RedisError:
        logger.error('角色任务 %s 入队失败：任务队列（Redis）不可用', job_id, exc_info=True)
        return errors.character_error(503, 'QUEUE_UNAVAILABLE')
    return _accepted(job_id, base_url)


@router.get('/{job_id}')
def get_character(job_id: str, request: Request):
    store: JobStore = request.app.state.store
    base_url = urls.public_base_url(request)
    try:
        data = store.get(job_id)
    except redis.exceptions.RedisError:
        logger.error('查询角色任务 %s 失败：任务队列（Redis）不可用', job_id, exc_info=True)
        return errors.character_error(503, 'QUEUE_UNAVAILABLE')
    if data is None:
        logger.warning('查询角色任务 %s 失败：任务不存在或磁盘快照损坏', job_id)
        return errors.character_error(404, 'JOB_NOT_FOUND')
    status = data['status']
    if 'result' in data:
        # 中文原因与完整地址都在响应出口补：result.json 里只存稳定错误码与相对路径
        payload = errors.enrich_reason(json.loads(data['result']))
        logger.debug('角色任务 %s 命中终态 %s', job_id, status)
        return _status_body(job_id, status, base_url, **{k: v for k, v in payload.items() if k != 'status'})
    logger.debug('角色任务 %s 处于非终态 %s', job_id, status)
    return _status_body(job_id, status, base_url, updatedAt=data.get('updatedAt'))
