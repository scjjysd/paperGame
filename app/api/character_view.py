"""角色审查视图：汇总 job_dir 全部产物（原图/动图/标注/精灵表），并直出单页 HTML。

与 /v1/characters/{id} 的分工：后者是 Unity 契约（contracts.py 为唯一真源，不掺调试字段），
本模块是给人看的审查视图，故独立成 router —— Unity 侧镜像 contracts.py 时不会看到这两个端点。

页面样式与拼装函数复用 review_html，关卡审查页（level_view）用的是同一套。
"""
import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Dict, Optional

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api import errors, urls
from app.api.review_html import badge, figure, header, page, section
from app.services.job_store import JobStore

MOTIONS = ('run', 'jump')
STATUS_COLOR = {'ready': '#1a7f37', 'needs_correction': '#9a6700', 'failed': '#cf222e'}

# 角色页特有样式：精灵表播放、mask 红色叠加。通用骨架样式见 review_html.PAGE_CSS
EXTRA_CSS = """
.sheet{display:block;max-width:100%;border:1px solid #d1d9e0}
.playbox{border:1px solid #d1d9e0}
.play{width:100%;height:100%;background-repeat:no-repeat}
.overlay img{display:block;max-height:300px;width:auto}
.maskred{position:absolute;inset:0;background:#ff2828;opacity:.45;
         mask-size:100% 100%;mask-mode:luminance;
         -webkit-mask-size:100% 100%;-webkit-mask-source-type:luminance}
"""


def _url(job_id: str, rel: str, job_dir: Path, base_url: str = '') -> Optional[str]:
    """产物真实存在才给 URL：早期失败时 result.json 的 maskUrl 是无条件写死的，会指向不存在的文件。"""
    if not (job_dir / rel).exists():
        return None
    return urls.absolute_url(base_url, f'/artifacts/{job_id}/{rel}')


def _load_anno(char_cfg: Path) -> Dict:
    """char_cfg.yaml -> 画布尺寸 + 关节表（joints 的 loc 相对该尺寸）。早期失败时该文件不存在。"""
    if not char_cfg.exists():
        return {'width': None, 'height': None, 'joints': []}
    cfg = yaml.safe_load(char_cfg.read_text()) or {}
    return {
        'width': cfg.get('width'), 'height': cfg.get('height'),
        'joints': [{'name': j['name'], 'loc': [int(x) for x in j['loc']], 'parent': j['parent']}
                   for j in cfg.get('skeleton', [])],
    }


def collect_detail(job_id: str, job_dir: Path, status: str, result: Optional[Dict],
                   base_url: str = '') -> Dict:
    """扫描 job_dir 汇总审查视图。缺失产物一律 None/空，供页面与客户端降级显示。

    base_url 非空时所有 URL 都是完整地址（浏览器直接可点）；为空则保持站内相对路径。
    """
    result = result or {}
    result_json = job_dir / 'result.json'
    reason, code = result.get('reason'), result.get('code')
    return {
        'characterId': job_id,
        'status': status,
        'reason': reason,
        'code': code,
        # 中文原因与 GET /v1/characters/{id} 同源：reason 走 REASON_MESSAGES，code 走 ERROR_MESSAGES
        'message': errors.enrich_reason(dict(result)).get('message'),
        'renderedAt': (datetime.fromtimestamp(result_json.stat().st_mtime).astimezone().isoformat(timespec='seconds')
                       if result_json.exists() else None),
        'viewUrl': urls.absolute_url(base_url, urls.character_view_path(job_id)),
        'detailUrl': urls.absolute_url(base_url, urls.character_detail_path(job_id)),
        'inputUrl': _url(job_id, 'input.png', job_dir, base_url),
        'annotation': {
            'maskUrl': _url(job_id, 'anno/mask.png', job_dir, base_url),
            'textureUrl': _url(job_id, 'anno/texture.png', job_dir, base_url),
            'charCfgUrl': _url(job_id, 'anno/char_cfg.yaml', job_dir, base_url),
            **_load_anno(job_dir / 'anno' / 'char_cfg.yaml'),
        },
        # gifUrl 是未裁切的渲染原件，历史 job 目录没有它（GIF 保留是后加的），降级为 None；
        # result.json 里的 spriteSheetUrl 存的是相对路径，这里一并补成完整地址
        'animations': {m: {**urls.absolutize(meta, base_url),
                           'gifUrl': _url(job_id, f'{m}.gif', job_dir, base_url)}
                       for m, meta in (result.get('animations') or {}).items()},
    }


def _keyframes(animations: Dict) -> str:
    """每个动作一条位移关键帧。只对 MOTIONS 内的名字生成，动作名不会流进 CSS 标识符。"""
    return ''.join(
        f'@keyframes play-{m}{{from{{background-position:0 0}}'
        f'to{{background-position:-{animations[m]["frameWidth"] * animations[m]["frameCount"]}px 0}}}}'
        for m in MOTIONS if animations.get(m, {}).get('spriteSheetUrl'))


def _player(motion: str, a: Dict) -> str:
    """用 CSS steps() 播精灵表：切帧参数与 Unity 完全同一套，所见即真机效果。"""
    if not a.get('spriteSheetUrl'):
        return figure(None, f'{motion} 播放')
    n, fps = a['frameCount'], a['fps']
    url = escape(a['spriteSheetUrl'], quote=True)
    return (f'<figure><div class="alpha playbox" '
            f'style="width:{a["frameWidth"]}px;height:{a["frameHeight"]}px">'
            f'<div class="play" style="background-image:url({url});'
            f'animation:play-{motion} {n / fps:.3f}s steps({n}) infinite"></div></div>'
            f'<figcaption>{escape(motion)} · 精灵表播放（{n} 帧 / {fps}fps，Unity 实际效果）'
            f'</figcaption></figure>')


def _overlay(anno: Dict) -> str:
    """texture 上叠红 mask + 青关节点：红区外的黑笔画会被 vendor 强制透明、在成片里彻底消失。

    mask.png 是 L 模式无 alpha，CSS 遮罩必须显式 luminance，否则按 alpha 解读会整块涂红。
    """
    texture, mask = anno.get('textureUrl'), anno.get('maskUrl')
    if not (texture and mask):
        return figure(None, 'mask 叠加 + 关节')
    w, h = anno.get('width') or 0, anno.get('height') or 0
    radius = max(2, min(w, h) // 90) if w and h else 3
    circles = ''.join(
        f'<circle cx="{j["loc"][0]}" cy="{j["loc"][1]}" r="{radius}" '
        f'fill="#00d2ff" stroke="#000" stroke-width="1"/>' for j in anno.get('joints') or [])
    svg = f'<svg viewBox="0 0 {w} {h}">{circles}</svg>' if (w and h and circles) else ''
    m = escape(mask, quote=True)
    return (f'<figure><div class="overlay">'
            f'<img src="{escape(texture, quote=True)}" alt="mask 叠加">'
            f'<div class="maskred" style="mask-image:url({m});-webkit-mask-image:url({m})"></div>'
            f'{svg}</div><figcaption>mask 叠加 + 关节</figcaption></figure>')


def _sheets(animations: Dict) -> str:
    return ''.join(
        f'<figure><div class="meta">{escape(m)}.png · {animations[m]["frameCount"]} 帧 · '
        f'{animations[m]["frameWidth"]}x{animations[m]["frameHeight"]} · '
        f'{animations[m]["fps"]}fps · 脚底锚点 ({animations[m]["footAnchor"]["x"]},'
        f'{animations[m]["footAnchor"]["y"]})</div>'
        f'<a href="{escape(animations[m]["spriteSheetUrl"], quote=True)}" target="_blank">'
        f'<img class="sheet alpha" src="{escape(animations[m]["spriteSheetUrl"], quote=True)}" '
        f'alt="{escape(m)} sprite sheet"></a></figure>'
        for m in MOTIONS if animations.get(m, {}).get('spriteSheetUrl'))


def build_detail_html(detail: Dict) -> str:
    """detail 字典 -> 单页 HTML。纯函数不碰磁盘（照 review_batch.build_html 的范式，便于离线自检）。"""
    job_id, status = detail['characterId'], detail['status']
    animations, anno = detail.get('animations') or {}, detail.get('annotation') or {}
    label = status + ''.join(f' · {detail[k]}' for k in ('reason', 'code') if detail.get(k))
    if detail.get('message'):
        label += f'（{detail["message"]}）'
    bits = [f'渲染于 {detail["renderedAt"]}' if detail.get('renderedAt') else '尚无产物',
            f'{len(anno.get("joints") or [])} 关节']
    if anno.get('width'):
        bits.append(f'画布 {anno["width"]}x{anno["height"]}')
    detail_href = escape(detail.get('detailUrl') or urls.character_detail_path(job_id), quote=True)
    summary = escape(' · '.join(bits)) + f' · <a href="{detail_href}">detail JSON</a>'

    players = ''.join(_player(m, animations[m]) for m in MOTIONS if m in animations)
    gifs = ''.join(figure(animations.get(m, {}).get('gifUrl'), f'{m}.gif') for m in MOTIONS)
    sheets = _sheets(animations)
    body = (
        section('原图与动画', f'<div class="row">{figure(detail.get("inputUrl"), "原图")}'
                             f'{players}{gifs}</div>')
        + section('标注 · 红色 = mask 覆盖（留在原色的笔画会被丢弃）；青点 = 吸附后关节',
                  f'<div class="row">{_overlay(anno)}{figure(anno.get("textureUrl"), "texture")}'
                  f'{figure(anno.get("maskUrl"), "mask")}</div>')
        + section('Unity 精灵表（点击看原尺寸）', f'<div class="row">{sheets}</div>')
    )
    return page(f'{job_id} · 角色审查',
                header(job_id, badge(label, STATUS_COLOR.get(status, '#59636e')), summary),
                body, EXTRA_CSS + _keyframes(animations))


router = APIRouter(prefix='/v1/characters', tags=['characters'])


def _lookup(job_id: str, request: Request):
    """(detail, None) 或 (None, 404)。detail 与 view 共用同一条查找路径，两者不会各说各话。"""
    store: JobStore = request.app.state.store
    data = store.get(job_id)
    if data is None:
        return None, errors.character_error(404, 'JOB_NOT_FOUND')
    result = json.loads(data['result']) if 'result' in data else None
    detail = collect_detail(job_id, store.jobs_root / job_id, data['status'], result,
                            urls.public_base_url(request))
    return detail, None


@router.get('/{job_id}/detail')
def get_character_detail(job_id: str, request: Request):
    """审查用：一次拿到原图 / GIF / 标注 / 精灵表的全部 URL 与元数据。"""
    detail, err = _lookup(job_id, request)
    return err or detail


@router.get('/{job_id}/view', response_class=HTMLResponse)
def get_character_view(job_id: str, request: Request):
    """同一份信息的单页 HTML，浏览器直接打开即可目视验收。"""
    detail, err = _lookup(job_id, request)
    return err or HTMLResponse(build_detail_html(detail))
