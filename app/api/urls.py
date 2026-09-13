"""把响应里的站内相对路径补成完整地址，客户端/浏览器拿到即可直接访问。

产物与状态 URL 在磁盘快照（result.json）里始终存相对路径：它跨机器、跨端口都成立，
换成绝对地址后一旦部署地址变更，历史任务里的链接就全部失效。因此只在响应出口补全：

- 基址优先取 ``PUBLIC_BASE_URL``，仅在需要固定统一地址时设置；
- 未配置时按请求协议和 Host（含外部端口）推导。Nginx 保留 Host 并覆盖转发协议头，
  Uvicorn 在可信容器网络中解析代理头，因此 HTTP/HTTPS 会各自生成同源链接。
"""
import os
from typing import Any, Optional
from urllib.parse import quote

# 只补全站内路径，避免误改外部链接与普通字符串
SITE_PREFIXES = ('/artifacts/', '/v1/')


def character_status_path(job_id: str) -> str:
    return '/v1/characters/' + quote(str(job_id), safe='')


def character_detail_path(job_id: str) -> str:
    return character_status_path(job_id) + '/detail'


def character_view_path(job_id: str) -> str:
    return character_status_path(job_id) + '/view'


def level_status_path(job_id: str) -> str:
    return '/v1/levels/' + quote(str(job_id), safe='')


def level_detail_path(job_id: str) -> str:
    return level_status_path(job_id) + '/detail'


def level_view_path(job_id: str) -> str:
    return level_status_path(job_id) + '/view'


def public_base_url(request=None) -> str:
    """对外基址，不带结尾斜杠；无从推导时返回空串（调用方据此保持相对路径）。"""
    configured = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')
    if configured:
        return configured
    if request is None:
        return ''
    return str(request.base_url).rstrip('/')


def absolute_url(base: str, path: Optional[str]) -> Optional[str]:
    """单个路径补全。base 为空或已是绝对链接时原样返回。"""
    if not path or not base or not isinstance(path, str):
        return path
    if path.startswith(('http://', 'https://')):
        return path
    if not path.startswith('/'):
        path = '/' + path
    return base + path


def site_url(path: Optional[str]) -> bool:
    return isinstance(path, str) and path.startswith(SITE_PREFIXES)


def absolutize(value: Any, base: str) -> Any:
    """递归补全 dict/list 里的站内相对路径，其余值原样返回（不改原对象）。"""
    if isinstance(value, str):
        return absolute_url(base, value) if site_url(value) else value
    if isinstance(value, dict):
        return {key: absolutize(item, base) for key, item in value.items()}
    if isinstance(value, list):
        return [absolutize(item, base) for item in value]
    return value
