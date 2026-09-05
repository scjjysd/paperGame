"""角色审查视图：汇总 job_dir 全部产物（原图/动图/标注/精灵表），并直出单页 HTML。

与 /v1/characters/{id} 的分工：后者是 Unity 契约（contracts.py 为唯一真源，不掺调试字段），
本模块是给人看的审查视图，故独立成 router —— Unity 侧镜像 contracts.py 时不会看到这两个端点。
"""
import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.characters import _error
from app.services.job_store import JobStore

MOTIONS = ('run', 'jump')


def _url(job_id: str, rel: str, job_dir: Path) -> Optional[str]:
    """产物真实存在才给 URL：早期失败时 result.json 的 maskUrl 是无条件写死的，会指向不存在的文件。"""
    return f'/artifacts/{job_id}/{rel}' if (job_dir / rel).exists() else None


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


def collect_detail(job_id: str, job_dir: Path, status: str, result: Optional[Dict]) -> Dict:
    """扫描 job_dir 汇总审查视图。缺失产物一律 None/空，供页面与客户端降级显示。"""
    result = result or {}
    result_json = job_dir / 'result.json'
    return {
        'characterId': job_id,
        'status': status,
        'reason': result.get('reason'),
        'code': result.get('code'),
        'renderedAt': (datetime.fromtimestamp(result_json.stat().st_mtime).astimezone().isoformat(timespec='seconds')
                       if result_json.exists() else None),
        'viewUrl': f'/v1/characters/{job_id}/view',
        'inputUrl': _url(job_id, 'input.png', job_dir),
        'annotation': {
            'maskUrl': _url(job_id, 'anno/mask.png', job_dir),
            'textureUrl': _url(job_id, 'anno/texture.png', job_dir),
            'charCfgUrl': _url(job_id, 'anno/char_cfg.yaml', job_dir),
            **_load_anno(job_dir / 'anno' / 'char_cfg.yaml'),
        },
        # gifUrl 是未裁切的渲染原件，历史 job 目录没有它（GIF 保留是后加的），降级为 None
        'animations': {m: {**meta, 'gifUrl': _url(job_id, f'{m}.gif', job_dir)}
                       for m, meta in (result.get('animations') or {}).items()},
    }


STATUS_COLOR = {'ready': '#1a7f37', 'needs_correction': '#9a6700', 'failed': '#cf222e'}

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f8fa;color:#1f2328;
     font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}
header{position:sticky;top:0;z-index:2;padding:14px 20px;background:#fff;border-bottom:1px solid #d1d9e0}
h1{margin:0;font-size:17px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.badge{font-size:12px;color:#fff;padding:2px 9px;border-radius:10px}
.sum{margin-top:5px;color:#59636e;font-size:12px}
.sum a{color:#0969da}
main{padding:18px;display:flex;flex-direction:column;gap:14px}
section{background:#fff;border:1px solid #d1d9e0;border-radius:8px;padding:12px 16px 16px}
h2{margin:0 0 10px;font-size:13px;font-weight:600;color:#59636e}
.row{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end}
figure{margin:0;display:flex;flex-direction:column;gap:6px;align-items:center}
figcaption{font-size:12px;color:#59636e;text-align:center;max-width:300px}
.alpha{background-image:linear-gradient(45deg,#e9ecef 25%,transparent 25%,transparent 75%,#e9ecef 75%),
       linear-gradient(45deg,#e9ecef 25%,transparent 25%,transparent 75%,#e9ecef 75%);
       background-size:16px 16px;background-position:0 0,8px 8px}
.thumb{display:block;max-height:280px;border:1px solid #d1d9e0}
.sheet{display:block;max-width:100%;border:1px solid #d1d9e0}
.missing{display:flex;align-items:center;justify-content:center;width:150px;height:110px;
         border:1px dashed #d1d9e0;border-radius:6px;color:#8c959f;font-size:12px}
.playbox{border:1px solid #d1d9e0}
.play{width:100%;height:100%;background-repeat:no-repeat}
.overlay{position:relative;display:inline-block;line-height:0}
.overlay img{display:block;max-height:300px;width:auto}
.maskred{position:absolute;inset:0;background:#ff2828;opacity:.45;
         mask-size:100% 100%;mask-mode:luminance;
         -webkit-mask-size:100% 100%;-webkit-mask-source-type:luminance}
.overlay svg{position:absolute;inset:0;width:100%;height:100%}
.meta{font-size:12px;color:#59636e;font-variant-numeric:tabular-nums}
"""


def _figure(url: Optional[str], label: str, cls: str = 'thumb alpha') -> str:
    """缺图时输出占位块，绝不产生空 src（否则浏览器会把当前页面当图片再请求一遍）。"""
    body = (f'<img class="{cls}" src="{escape(url, quote=True)}" alt="{escape(label)}">'
            if url else f'<div class="missing">无 {escape(label)}</div>')
    return f'<figure>{body}<figcaption>{escape(label)}</figcaption></figure>'


def _keyframes(animations: Dict) -> str:
    """每个动作一条位移关键帧。只对 MOTIONS 内的名字生成，动作名不会流进 CSS 标识符。"""
    return ''.join(
        f'@keyframes play-{m}{{from{{background-position:0 0}}'
        f'to{{background-position:-{animations[m]["frameWidth"] * animations[m]["frameCount"]}px 0}}}}'
        for m in MOTIONS if animations.get(m, {}).get('spriteSheetUrl'))


def _player(motion: str, a: Dict) -> str:
    """用 CSS steps() 播精灵表：切帧参数与 Unity 完全同一套，所见即真机效果。"""
    if not a.get('spriteSheetUrl'):
        return _figure(None, f'{motion} 播放')
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
        return _figure(None, 'mask 叠加 + 关节')
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


def build_detail_html(detail: Dict) -> str:
    """detail 字典 -> 单页 HTML。纯函数不碰磁盘（照 review_batch.build_html 的范式，便于离线自检）。"""
    job_id, status = detail['characterId'], detail['status']
    animations, anno = detail.get('animations') or {}, detail.get('annotation') or {}
    label = status + ''.join(f' · {detail[k]}' for k in ('reason', 'code') if detail.get(k))
    bits = [f'渲染于 {detail["renderedAt"]}' if detail.get('renderedAt') else '尚无产物',
            f'{len(anno.get("joints") or [])} 关节']
    if anno.get('width'):
        bits.append(f'画布 {anno["width"]}x{anno["height"]}')

    players = ''.join(_player(m, animations[m]) for m in MOTIONS if m in animations)
    gifs = ''.join(_figure(animations.get(m, {}).get('gifUrl'), f'{m}.gif') for m in MOTIONS)
    sheets = ''.join(
        f'<figure><div class="meta">{escape(m)}.png · {animations[m]["frameCount"]} 帧 · '
        f'{animations[m]["frameWidth"]}x{animations[m]["frameHeight"]} · '
        f'{animations[m]["fps"]}fps · 脚底锚点 ({animations[m]["footAnchor"]["x"]},'
        f'{animations[m]["footAnchor"]["y"]})</div>'
        f'<a href="{escape(animations[m]["spriteSheetUrl"], quote=True)}" target="_blank">'
        f'<img class="sheet alpha" src="{escape(animations[m]["spriteSheetUrl"], quote=True)}" '
        f'alt="{escape(m)} sprite sheet"></a></figure>'
        for m in MOTIONS if animations.get(m, {}).get('spriteSheetUrl'))
    href = escape(f'/v1/characters/{quote(job_id, safe="")}/detail', quote=True)

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(job_id)} · 角色审查</title>'
        f'<style>{CSS}{_keyframes(animations)}</style></head><body>'
        f'<header><h1>{escape(job_id)}'
        f'<span class="badge" style="background:{STATUS_COLOR.get(status, "#59636e")}">'
        f'{escape(label)}</span></h1>'
        f'<div class="sum">{escape(" · ".join(bits))} · <a href="{href}">detail JSON</a></div>'
        '</header><main>'
        f'<section><h2>原图与动画</h2><div class="row">'
        f'{_figure(detail.get("inputUrl"), "原图")}{players}{gifs}</div></section>'
        '<section><h2>标注 · 红色 = mask 覆盖（留在原色的笔画会被丢弃）；青点 = 吸附后关节</h2>'
        f'<div class="row">{_overlay(anno)}{_figure(anno.get("textureUrl"), "texture")}'
        f'{_figure(anno.get("maskUrl"), "mask")}</div></section>'
        + (f'<section><h2>Unity 精灵表（点击看原尺寸）</h2><div class="row">{sheets}</div></section>'
           if sheets else '')
        + '</main></body></html>')


router = APIRouter(prefix='/v1/characters', tags=['characters'])


def _lookup(job_id: str, request: Request):
    """(detail, None) 或 (None, 404)。detail 与 view 共用同一条查找路径，两者不会各说各话。"""
    store: JobStore = request.app.state.store
    data = store.get(job_id)
    if data is None:
        return None, _error(404, 'JOB_NOT_FOUND')
    result = json.loads(data['result']) if 'result' in data else None
    return collect_detail(job_id, store.jobs_root / job_id, data['status'], result), None


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
