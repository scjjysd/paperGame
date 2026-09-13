"""关卡审查视图：汇总 job_dir 全部产物 + 解析结果，并直出单页 HTML 预览。

与 /v1/levels/{id} 的分工：后者是客户端契约（level_contracts.py 为唯一真源），
本模块是给人看的审查视图 —— 把平台、出生点、终点、可玩性告警叠在拉正图上，
浏览器直接打开就能判断"识别对不对、关卡能不能玩"。

样式与拼装函数复用 review_html，角色审查页（character_view）用的是同一套。
"""
import json
import logging
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import redis.exceptions
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from PIL import Image

from app.api import errors, urls
from app.api.review_html import Raw, badge, figure, header, link_list, page, section, table
from app.level_contracts import LEVEL_STAGE_MESSAGES, LEVEL_STATUS_MESSAGES
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/v1/levels', tags=['levels'])

TERMINAL_STATES = ('ready', 'needs_fix', 'needs_review', 'failed')
STATUS_COLOR = {'ready': '#1a7f37', 'needs_fix': '#9a6700', 'needs_review': '#8250df',
                'failed': '#cf222e', 'processing': '#0969da', 'queued': '#59636e'}
PLAYABILITY_COLOR = {'playable': '#1a7f37', 'unreachable': '#cf222e'}
PLAYABILITY_LABEL = {'playable': '可玩', 'unreachable': '不可达', 'not_checked': '未检查'}
PATH_COLOR, OFF_PATH_COLOR, GOAL_COLOR, START_COLOR = '#1a7f37', '#d97706', '#cf222e', '#ff00ff'

# (响应字段, 磁盘文件名, 中文说明)：只列真实存在的文件，缺产物不给坏链接
ARTIFACTS: Tuple[Tuple[str, str, str], ...] = (
    ('inputUrl', 'input.png', '上传原图（已归一化为 PNG）'),
    ('rectifiedImageUrl', 'rectified.png', '透视拉正图（契约坐标系即此图）'),
    ('overlayImageUrl', 'overlay.png', '服务端生成的识别叠加图'),
    ('paperMaskUrl', 'paper-mask.png', '纸张区域遮罩'),
    ('inkMaskUrl', 'ink-mask.png', '墨迹遮罩（平台证据来源）'),
    ('levelJsonUrl', 'level.json', '关卡几何 JSON（契约产物）'),
    ('analysisJsonUrl', 'analysis.json', '可玩性分析 JSON'),
    ('transformJsonUrl', 'transform.json', '透视变换矩阵'),
    ('llmAuditUrl', 'llm-audit.json', 'LLM 语义复核审计'),
    ('resultJsonUrl', 'result.json', '终态快照'),
)
# 页面上直接展示的图片产物（其余只在"产物完整地址"清单里给链接）
IMAGE_FIELDS = ('inputUrl', 'rectifiedImageUrl', 'overlayImageUrl', 'paperMaskUrl', 'inkMaskUrl')
PROFILE_LABELS = (('profileVersion', '能力配置版本'), ('maxJumpRisePixels', '最大跳跃上升（像素）'),
                  ('maxJumpDistancePixels', '最大跳跃水平距离（像素）'),
                  ('characterWidthPixels', '角色宽度（像素）'),
                  ('characterHeightPixels', '角色高度（像素）'),
                  ('landingTolerancePixels', '落地容差（像素）'))

EXTRA_CSS = """
.overlay img{display:block;max-width:100%;height:auto;border:1px solid #d1d9e0}
.wide{display:flex;flex-direction:column;gap:10px}
ul.meta{margin:0;padding-left:18px}
.ok{color:#1a7f37}
.warn{color:#9a6700}
.bad{color:#cf222e}
"""


def _load_json(path: Path) -> Optional[Any]:
    """读产物 JSON；缺失或损坏一律 None，审查页要能在半成品目录上照常渲染。"""
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _parse_result(data: Dict) -> Dict:
    """Redis 里的终态载荷是 JSON 字符串；损坏时按空载荷处理，页面照样能看磁盘产物。"""
    raw = data.get('result')
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning('关卡任务的终态载荷不是合法 JSON，审查页改用磁盘产物')
        return {}
    return payload if isinstance(payload, dict) else {}


def _image_size(path: Path) -> Tuple[Optional[int], Optional[int]]:
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except (OSError, ValueError):
        return None, None


def _artifacts(job_id: str, job_dir: Path, base_url: str) -> Dict[str, Optional[str]]:
    return {field: (urls.absolute_url(base_url, '/artifacts/{}/{}'.format(job_id, name))
                    if (job_dir / name).exists() else None)
            for field, name, _label in ARTIFACTS}


def _canvas(level: Optional[Dict], job_dir: Path) -> Dict[str, Optional[int]]:
    """画布尺寸优先取契约里的 canvas，缺失时退回读拉正图实际尺寸（复核态常见）。"""
    canvas = (level or {}).get('canvas') or {}
    if canvas.get('width') and canvas.get('height'):
        return {'width': int(canvas['width']), 'height': int(canvas['height'])}
    width, height = _image_size(job_dir / 'rectified.png')
    return {'width': width, 'height': height}


def _platforms(level: Optional[Dict], analysis: Optional[Dict]) -> List[Dict[str, Any]]:
    """平台清单 + 是否落在可达路径上：一眼看出哪几块被判定为走不到。"""
    on_path = set((analysis or {}).get('path') or [])
    rows = []
    for platform in (level or {}).get('platforms') or []:
        start, end = platform.get('start') or {}, platform.get('end') or {}
        length = int(round(((end.get('x', 0) - start.get('x', 0)) ** 2
                            + (end.get('y', 0) - start.get('y', 0)) ** 2) ** .5))
        rows.append({'id': platform.get('id'), 'start': start, 'end': end, 'length': length,
                     'confidence': platform.get('confidence'), 'onPath': platform.get('id') in on_path})
    return rows


def _message(status: str, review: Optional[Dict], error: Optional[Dict],
             warnings: List[Dict]) -> str:
    """中文原因优先级：失败原因 > 复核原因 > 可玩性告警 > 状态说明。"""
    if error and error.get('message'):
        return '[{}] {}'.format(error.get('code'), error['message'])
    if review and review.get('message'):
        return '[{}] {}'.format(review.get('reason'), review['message'])
    base = LEVEL_STATUS_MESSAGES.get(status, '')
    if warnings:
        return '{}（{} 条可玩性告警：{}）'.format(
            base, len(warnings), '；'.join(str(w.get('message') or w.get('code') or '') for w in warnings[:3]))
    return base


def collect_level_detail(job_id: str, job_dir: Path, data: Optional[Dict],
                         base_url: str = '') -> Dict[str, Any]:
    """扫描 job_dir + 终态载荷，汇总关卡审查视图。缺失内容一律 None/空。

    base_url 非空时所有 URL 都是完整地址（浏览器直接可点）；为空则保持站内相对路径。
    """
    data = data or {}
    status = data.get('status', 'queued')
    payload = _parse_result(data)
    result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
    # Redis 过期后 level.json / analysis.json 仍在磁盘上，审查页不该因此空白
    level = result.get('level') or _load_json(job_dir / 'level.json')
    analysis = result.get('analysis') or _load_json(job_dir / 'analysis.json')
    level = level if isinstance(level, dict) else None
    analysis = analysis if isinstance(analysis, dict) else {}
    review = payload.get('review') if isinstance(payload.get('review'), dict) else None
    error = payload.get('error') if isinstance(payload.get('error'), dict) else None
    warnings = analysis.get('warnings') or []
    stage = data.get('stage', 'waiting')
    result_json = job_dir / 'result.json'

    return {
        'jobId': job_id,
        'status': status,
        'statusMessage': LEVEL_STATUS_MESSAGES.get(status, ''),
        'message': _message(status, review, error, warnings),
        'createdAt': payload.get('createdAt') or data.get('createdAt'),
        'updatedAt': payload.get('updatedAt') or data.get('updatedAt'),
        'parsedAt': (datetime.fromtimestamp(result_json.stat().st_mtime).astimezone().isoformat(timespec='seconds')
                     if result_json.exists() else None),
        'schemaVersion': payload.get('schemaVersion'),
        'algorithmVersion': payload.get('algorithmVersion'),
        'statusUrl': urls.absolute_url(base_url, urls.level_status_path(job_id)),
        'detailUrl': urls.absolute_url(base_url, urls.level_detail_path(job_id)),
        'viewUrl': urls.absolute_url(base_url, urls.level_view_path(job_id)),
        'progress': None if status in TERMINAL_STATES else {
            'stage': stage, 'stageLabel': LEVEL_STAGE_MESSAGES.get(stage, stage),
            'percent': 0 if status == 'queued' else 55},
        'canvas': _canvas(level, job_dir),
        'artifacts': _artifacts(job_id, job_dir, base_url),
        'level': level,
        'analysis': analysis or None,
        'review': review,
        'error': error,
        'platforms': _platforms(level, analysis),
        'playability': analysis.get('playability'),
        'path': analysis.get('path') or [],
        'warnings': warnings,
        'profile': analysis.get('profile'),
        # needs_review 的 reason 在 review 里；早期复核（拉正失败）只写在 analysis.json
        'reviewReason': (review or {}).get('reason') or analysis.get('reviewReason'),
    }


def _svg(detail: Dict[str, Any]) -> str:
    """把平台 / 出生点 / 终点 / 复核候选画成 SVG，与拉正图同坐标系叠加。"""
    canvas = detail.get('canvas') or {}
    width, height = canvas.get('width'), canvas.get('height')
    if not (width and height):
        return ''
    stroke = max(2, min(width, height) // 400)
    font = max(10, min(width, height) // 70)
    level = detail.get('level') or {}
    review = detail.get('review') or {}
    on_path = set(detail.get('path') or [])
    parts: List[str] = []

    def text(x: int, y: int, content: str, color: str) -> None:
        parts.append('<text x="{}" y="{}" font-size="{}" fill="{}">{}</text>'.format(
            x, max(font, y - stroke), font, color, escape(str(content))))

    for platform in detail.get('platforms') or []:
        start, end = platform['start'], platform['end']
        color = PATH_COLOR if platform['id'] in on_path else OFF_PATH_COLOR
        parts.append('<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}" stroke-width="{}" '
                     'stroke-linecap="round"/>'.format(start.get('x', 0), start.get('y', 0),
                                                       end.get('x', 0), end.get('y', 0), color, stroke))
        text(start.get('x', 0), start.get('y', 0), platform['id'], color)
    for candidate in review.get('candidates') or []:
        region = candidate.get('region') or {}
        parts.append('<rect x="{}" y="{}" width="{}" height="{}" fill="none" stroke="{}" '
                     'stroke-width="{}"/>'.format(region.get('x', 0), region.get('y', 0),
                                                  region.get('width', 0), region.get('height', 0),
                                                  GOAL_COLOR, stroke))
        text(region.get('x', 0), region.get('y', 0), candidate.get('id'), GOAL_COLOR)
    start = level.get('playerStart') or {}
    if start:
        parts.append('<circle cx="{}" cy="{}" r="{}" fill="{}" stroke="#000" stroke-width="1"/>'
                     .format(start.get('x', 0), start.get('y', 0), stroke + 2, START_COLOR))
        parts.append('<text x="{}" y="{}" font-size="{}" fill="{}">出生点</text>'.format(
            start.get('x', 0) + stroke + 4, start.get('y', 0), font, START_COLOR))
    goal = level.get('goalRegion') or {}
    if goal:
        parts.append('<rect x="{}" y="{}" width="{}" height="{}" fill="none" stroke="{}" '
                     'stroke-width="{}"/>'.format(goal.get('x', 0), goal.get('y', 0),
                                                  goal.get('width', 0), goal.get('height', 0),
                                                  GOAL_COLOR, stroke))
        text(goal.get('x', 0), goal.get('y', 0), '终点', GOAL_COLOR)
    return '<svg viewBox="0 0 {} {}">{}</svg>'.format(width, height, ''.join(parts))


def _overlay_figure(detail: Dict[str, Any]) -> str:
    """拉正图上叠几何：不依赖服务端的 overlay.png，缺它也能预览。"""
    image_url = (detail.get('artifacts') or {}).get('rectifiedImageUrl')
    label = '识别叠加（绿 = 可达路径上的平台，橙 = 未纳入路径，红框 = 终点/复核候选，紫点 = 出生点）'
    if (detail.get('analysis') or {}).get('playability') == 'not_checked':
        label = '识别叠加（橙色 = 平台，红框 = 终点，紫点 = 圆圈起点；未检查可达性）'
    if not image_url:
        return figure(None, '识别叠加')
    return ('<figure><div class="overlay"><img src="{}" alt="{}">{}</div>'
            '<figcaption>{}</figcaption></figure>'.format(
                escape(image_url, quote=True), escape('识别叠加'), _svg(detail), escape(label)))


def _analysis_html(detail: Dict[str, Any]) -> str:
    analysis = detail.get('analysis')
    if analysis and analysis.get('playability') == 'not_checked':
        return '<p class="meta">已识别起点和终点；不判断平台承载、跳跃距离或通关可达性。</p>'
    if not analysis:
        return '<p class="empty">尚无可玩性分析（任务未进入分析阶段，或产物已被清理）。</p>'
    parts: List[str] = []
    playability = detail.get('playability')
    if playability:
        parts.append('<p class="meta">判定 {} · 出生平台 {} · 终点平台 {} · 路径 {}</p>'.format(
            badge(PLAYABILITY_LABEL.get(playability, playability),
                  PLAYABILITY_COLOR.get(playability, '#59636e')),
            escape(str(analysis.get('startPlatformId') or '未确定')),
            escape(str(analysis.get('goalPlatformId') or '未确定')),
            escape(' → '.join(str(item) for item in detail.get('path') or []) or '无')))
    profile = detail.get('profile') or {}
    parts.append(table(['角色能力参数', '取值'],
                       [[label, profile.get(key)] for key, label in PROFILE_LABELS if key in profile]))
    parts.append(table(['告警码', '中文说明', '相关平台', '需要(px)', '实际(px)'],
                       [[w.get('code'), w.get('message'), ', '.join(w.get('relatedPlatformIds') or []),
                         w.get('requiredValuePixels'), w.get('availableValuePixels')]
                        for w in detail.get('warnings') or []], numeric_columns=(3, 4))
               or '<p class="meta ok">无可玩性告警。</p>')
    return ''.join(part for part in parts if part)


def _review_html(review: Optional[Dict]) -> str:
    if not review:
        return ''
    parts = ['<p class="meta">{} {}</p>'.format(badge(review.get('reason') or '-', '#8250df'),
                                                escape(str(review.get('message') or '')))]
    rows = []
    for candidate in review.get('candidates') or []:
        region = candidate.get('region') or {}
        rows.append([candidate.get('id'), candidate.get('type'),
                     'x={} y={} 宽={} 高={}'.format(region.get('x'), region.get('y'),
                                                    region.get('width'), region.get('height')),
                     candidate.get('confidence')])
    parts.append(table(['候选 ID', '类型', '区域', '置信度'], rows, numeric_columns=(3,)))
    suggestions = review.get('suggestions') or []
    if suggestions:
        parts.append('<ul class="meta">{}</ul>'.format(
            ''.join('<li>{}</li>'.format(escape(str(item))) for item in suggestions)))
    return ''.join(part for part in parts if part)


def _error_html(error: Optional[Dict]) -> str:
    if not error:
        return ''
    return table(['错误码', '中文原因', '可重试', 'requestId'],
                 [[error.get('code'), error.get('message'),
                   '是' if error.get('retryable') else '否', error.get('requestId')]])


def _platform_rows(detail: Dict[str, Any]) -> List[List[Any]]:
    rows = []
    for platform in detail.get('platforms') or []:
        start, end = platform['start'], platform['end']
        rows.append([platform['id'],
                     '({}, {})'.format(start.get('x'), start.get('y')),
                     '({}, {})'.format(end.get('x'), end.get('y')),
                     platform['length'], platform['confidence'],
                     Raw('<span class="ok">是</span>') if platform['onPath']
                     else Raw('<span class="warn">否</span>')])
    return rows


def build_level_html(detail: Dict[str, Any]) -> str:
    """detail 字典 -> 单页 HTML。纯函数不碰磁盘，便于离线单测断言页面内容。"""
    job_id, status = detail['jobId'], detail['status']
    artifacts, canvas = detail['artifacts'], detail['canvas']
    color = STATUS_COLOR.get(status, '#59636e')

    bits: List[str] = []
    if detail.get('progress'):
        bits.append('阶段 {}（{}%）'.format(detail['progress']['stageLabel'], detail['progress']['percent']))
    if canvas.get('width'):
        bits.append('画布 {}x{}'.format(canvas['width'], canvas['height']))
    bits.append('{} 块平台'.format(len(detail.get('platforms') or [])))
    playability = detail.get('playability')
    if playability:
        bits.append('可玩性 {}'.format(PLAYABILITY_LABEL.get(playability, playability)))
    if detail.get('parsedAt'):
        bits.append('解析于 {}'.format(detail['parsedAt']))
    elif detail.get('createdAt'):
        bits.append('创建于 {}'.format(detail['createdAt']))
    detail_href = escape(detail.get('detailUrl') or urls.level_detail_path(job_id), quote=True)
    summary = escape(' · '.join(bits)) + ' · <a href="{}">detail JSON</a>'.format(detail_href)

    badges = badge(LEVEL_STATUS_MESSAGES.get(status, status).split('，')[0], color) + badge(status, color)
    if detail.get('message'):
        badges += badge(detail['message'], color)

    images = ''.join(figure(artifacts.get(field), label)
                     for field, _name, label in ARTIFACTS if field in IMAGE_FIELDS)
    links = link_list([(label, artifacts.get(field)) for field, _name, label in ARTIFACTS])
    body = (
        section('关卡图与产物', '<div class="row">{}</div>'.format(images))
        + section('识别结果叠加', '<div class="wide">{}</div>'.format(_overlay_figure(detail)))
        + section('平台清单', table(['平台 ID', '起点', '终点', '长度(px)', '置信度', '在可达路径上'],
                                   _platform_rows(detail), numeric_columns=(3, 4))
                  or '<p class="empty">尚未识别出平台。</p>')
        + section('可玩性分析', _analysis_html(detail))
        + section('人工复核', _review_html(detail.get('review')))
        + section('失败原因', _error_html(detail.get('error')))
        + section('产物完整地址', links or '<p class="empty">暂无产物落盘。</p>')
    )
    return page('{} · 关卡审查'.format(job_id), header(job_id, badges, summary), body, EXTRA_CSS)


def _lookup(job_id: str, request: Request):
    """(detail, None) 或 (None, 错误响应)。detail 与 view 共用同一条查找路径，两者不会各说各话。"""
    store: JobStore = request.app.state.level_store
    try:
        data = store.get(job_id)
    except redis.exceptions.RedisError:
        logger.error('查询关卡任务 %s 失败：任务队列（Redis）不可用', job_id, exc_info=True)
        return None, errors.level_error(503, 'QUEUE_UNAVAILABLE', retryable=True)
    if data is None:
        logger.warning('查询关卡任务 %s 失败：任务不存在且无可用磁盘快照', job_id)
        return None, errors.level_error(404, 'JOB_NOT_FOUND')
    detail = collect_level_detail(job_id, store.jobs_root / job_id, data, urls.public_base_url(request))
    return detail, None


@router.get('/{job_id}/detail')
def get_level_detail(job_id: str, request: Request):
    """审查用：一次拿到关卡图、识别几何、可玩性分析与全部产物的完整地址。"""
    detail, err = _lookup(job_id, request)
    return err or detail


@router.get('/{job_id}/view', response_class=HTMLResponse)
def get_level_view(job_id: str, request: Request):
    """同一份信息的单页 HTML，浏览器直接打开即可目视验收。"""
    detail, err = _lookup(job_id, request)
    return err or HTMLResponse(build_level_html(detail))
