"""中文错误响应：错误码之外必须给出人能直接看懂的原因。

三种响应体形状按各自既有契约保留，不做统一（客户端已分别消费）：

- 角色链路：扁平体 ``{"code", "message"}``（真源 contracts.ErrorBody）；
- 关卡链路：信封体 ``{"error": {"code", "message", "retryable", "requestId"}}``
  （真源 level_contracts.ErrorEnvelope）；
- 框架级错误（路径不存在 / 方法不允许 / 参数校验失败 / 未捕获异常）：两套键都给，
  客户端读自己那套即可，不必先猜错误来自哪条链路。

文案统一放在契约模块里（contracts.ERROR_MESSAGES / level_contracts.LEVEL_ERROR_MESSAGES），
新增错误码时必须同时补文案，否则这里会兜底成通用中文说明。
"""
import uuid
from typing import Any, Dict, Optional

from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app import contracts
from app.level_contracts import LEVEL_ERROR_MESSAGES

# 框架级错误码：FastAPI 默认只回英文 {"detail": "Not Found"}，排查时无从下手
NOT_FOUND = 'NOT_FOUND'
METHOD_NOT_ALLOWED = 'METHOD_NOT_ALLOWED'
INVALID_REQUEST = 'INVALID_REQUEST'
INTERNAL = 'INTERNAL'

FRAMEWORK_MESSAGES = {
    NOT_FOUND: '请求的路径不存在，请检查接口地址与 jobId。',
    METHOD_NOT_ALLOWED: '该路径不支持当前请求方法。',
    INVALID_REQUEST: '请求参数不合法，请检查字段名与取值。',
    INTERNAL: '服务端内部错误，请查看 out/logs 下的当天日志。',
}
STATUS_MESSAGES = {
    400: '请求参数不合法。',
    404: '请求的资源不存在。',
    405: '请求方法不被支持。',
    413: '上传内容超过大小上限。',
    415: '上传内容类型不被支持。',
    422: '请求参数校验失败。',
    500: '服务端内部错误。',
    503: '依赖服务暂不可用。',
}
FALLBACK_MESSAGE = '请求处理失败，请查看 out/logs 下的当天日志。'
# 两条链路的错误码有重叠（FILE_TOO_LARGE / JOB_NOT_FOUND 等），必须按链路选表，
# 否则关卡接口会拿到角色接口的文案
CHARACTER_TABLES = (contracts.ERROR_MESSAGES, FRAMEWORK_MESSAGES)
LEVEL_TABLES = (LEVEL_ERROR_MESSAGES, contracts.ERROR_MESSAGES, FRAMEWORK_MESSAGES)


def request_id() -> str:
    return 'req_' + uuid.uuid4().hex[:24]


def message_for(code: Optional[str], status_code: Optional[int] = None,
                message: Optional[str] = None, tables=()) -> str:
    """按 显式文案 > 错误码表（按传入顺序）> 状态码文案 > 兜底 的顺序取中文原因。"""
    if message:
        return message
    if code:
        for table in tables:
            text = table.get(code)
            if text:
                return text
    if status_code and status_code in STATUS_MESSAGES:
        return STATUS_MESSAGES[status_code]
    return FALLBACK_MESSAGE


def character_error(status_code: int, code: str, message: Optional[str] = None) -> JSONResponse:
    """角色链路错误体，形状以 contracts.ErrorBody（真源）为准：{"code", "message"}。"""
    text = message_for(code, status_code, message, CHARACTER_TABLES)
    try:
        body: Dict[str, Any] = contracts.ErrorBody(code=code, message=text).model_dump()
    except ValidationError:
        # code 不在 ErrorCode 枚举内（框架级错误复用本函数时）：不能因为文案而抛异常
        body = {'code': code, 'message': text}
    return JSONResponse(status_code=status_code, content=body)


def level_error(status_code: int, code: str, message: Optional[str] = None,
                retryable: bool = False, details: Any = None) -> JSONResponse:
    """关卡链路错误体，形状以 level_contracts.ErrorEnvelope（真源）为准：{"error": {...}}。"""
    body = {'code': code, 'message': message_for(code, status_code, message, LEVEL_TABLES),
            'retryable': retryable, 'requestId': request_id()}
    if details is not None:
        body['details'] = details
    return JSONResponse(status_code=status_code, content={'error': body})


def framework_error(status_code: int, code: str, message: Optional[str] = None,
                    retryable: bool = False, details: Any = None,
                    headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    """框架级错误体：扁平键与信封键都给，兼容两条链路的客户端。

    headers 用于透传框架自带的必要响应头（如 405 的 Allow）。
    """
    text = message_for(code, status_code, message, LEVEL_TABLES)
    envelope: Dict[str, Any] = {'code': code, 'message': text, 'retryable': retryable,
                                'requestId': request_id()}
    body: Dict[str, Any] = {'code': code, 'message': text, 'error': envelope}
    if details is not None:
        body['details'] = details
        envelope['details'] = details
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def enrich_reason(payload: Dict[str, Any]) -> Dict[str, Any]:
    """给角色链路终态载荷补中文原因：needs_correction 按 reason，failed 按 code。

    result.json 里只存稳定的错误码（跨机器可读、不随文案变更而失效），中文原因在响应时补。
    载荷已带 message、或本身是关卡链路的 {"error": {...}} 信封时原样返回。
    """
    if not isinstance(payload, dict) or payload.get('message') or isinstance(payload.get('error'), dict):
        return payload
    status = payload.get('status')
    if status == 'needs_correction':
        payload['message'] = contracts.REASON_MESSAGES.get(
            payload.get('reason'), '标注质量不达标，请按 /view 页面的提示修图后重传。')
    elif status == 'failed':
        payload['message'] = contracts.ERROR_MESSAGES.get(
            payload.get('code'), '渲染失败，请查看 out/logs 下的当天日志。')
    return payload
