"""角色任务接口：POST 上传入队（幂等），GET 轮询五态。"""
import json
from typing import Optional

import redis.exceptions
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from app import contracts
from app.services.job_store import JobStore

router = APIRouter(prefix='/v1/characters', tags=['characters'])

PNG_MAGIC = b'\x89PNG'
JPEG_MAGIC = b'\xff\xd8'
GIF_MAGIC = b'GIF8'
WEBP_MAGIC = b'RIFF'


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=contracts.ErrorBody(code=code).model_dump())


def _sniff(head: bytes) -> Optional[str]:
    """返回 'ok' | 'unsupported' | 'not_image'。仅接受 PNG/JPEG（P0 锁定）。注意：Python 3.9 不支持 `str | None` 运行时注解，必须用 Optional。"""
    if head.startswith(PNG_MAGIC) or head.startswith(JPEG_MAGIC):
        return 'ok'
    if head.startswith(GIF_MAGIC) or head.startswith(WEBP_MAGIC):
        return 'unsupported'
    return 'not_image'


@router.post('', status_code=202)
async def create_character(request: Request, file: UploadFile = File(...)):
    store: JobStore = request.app.state.store
    content = await file.read(contracts.MAX_UPLOAD_BYTES + 1)
    if len(content) > contracts.MAX_UPLOAD_BYTES:
        return _error(400, 'FILE_TOO_LARGE')
    verdict = _sniff(content[:4])
    if verdict == 'unsupported':
        return _error(400, 'UNSUPPORTED_FORMAT')
    if verdict == 'not_image':
        return _error(400, 'NOT_AN_IMAGE')

    job_id = contracts.derive_job_id(content)
    try:
        existing = store.get(job_id)
        if existing is not None:
            # 非终态：不重复入队；终态：客户端轮询即可看到结果
            return contracts.JobAccepted(jobId=job_id).model_dump()
        job_dir = store.jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / 'input.png').write_bytes(content)
        store.create(job_id)
        store.enqueue(job_id)
    except redis.exceptions.RedisError:
        return _error(503, 'QUEUE_UNAVAILABLE')
    return contracts.JobAccepted(jobId=job_id).model_dump()


@router.get('/{job_id}')
def get_character(job_id: str, request: Request):
    store: JobStore = request.app.state.store
    data = store.get(job_id)
    if data is None:
        return _error(404, 'JOB_NOT_FOUND')
    if 'result' in data:
        return json.loads(data['result'])
    return {'status': data['status'], 'updatedAt': data.get('updatedAt')}
