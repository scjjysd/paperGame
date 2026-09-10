"""审查页共享构件：角色 /view 与关卡 /view 复用同一套样式与拼装函数。

约定：所有函数都是纯函数（不碰磁盘、不读 request），入参已是可展示的字符串/URL，
出参是 HTML 片段 —— 便于离线单测断言页面内容（照 scripts/diag/review_batch.py 的范式）。
用户可控内容一律 escape，URL 额外用 quote=True。
"""
from html import escape
from typing import Any, Iterable, List, Optional, Sequence

# 两个审查页共用的骨架样式；各页的特有样式（精灵表播放、mask 叠加、几何叠加图）自行追加
PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f8fa;color:#1f2328;
     font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}
a{color:#0969da}
header{position:sticky;top:0;z-index:2;padding:14px 20px;background:#fff;border-bottom:1px solid #d1d9e0}
h1{margin:0;font-size:17px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.badge{font-size:12px;color:#fff;padding:2px 9px;border-radius:10px;white-space:nowrap}
.sum{margin-top:5px;color:#59636e;font-size:12px}
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
.missing{display:flex;align-items:center;justify-content:center;width:150px;height:110px;
         border:1px dashed #d1d9e0;border-radius:6px;color:#8c959f;font-size:12px;text-align:center}
.overlay{position:relative;display:inline-block;line-height:0}
.overlay svg{position:absolute;inset:0;width:100%;height:100%}
.meta{font-size:12px;color:#59636e;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{border:1px solid #d1d9e0;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa;color:#59636e;font-weight:600;white-space:nowrap}
td.num,th.num{font-variant-numeric:tabular-nums;text-align:right}
.empty{color:#8c959f;font-size:12px}
"""

NEUTRAL_COLOR = '#59636e'


class Raw(str):
    """已自行转义好的 HTML 片段：table 见到它就不再二次转义。"""


def badge(text: Any, color: str = NEUTRAL_COLOR) -> str:
    return (f'<span class="badge" style="background:{escape(color, quote=True)}">'
            f'{escape(str(text))}</span>')


def figure(url: Optional[str], label: str, cls: str = 'thumb alpha') -> str:
    """缺图时输出占位块，绝不产生空 src（否则浏览器会把当前页面当图片再请求一遍）。"""
    body = (f'<img class="{cls}" src="{escape(url, quote=True)}" alt="{escape(label)}">'
            if url else f'<div class="missing">无 {escape(label)}</div>')
    return f'<figure>{body}<figcaption>{escape(label)}</figcaption></figure>'


def _numeric_class(index: int, numeric_columns: Sequence[int]) -> str:
    return ' class="num"' if index in numeric_columns else ''


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]], numeric_columns: Sequence[int] = ()) -> str:
    """无数据时返回空串，调用方据此决定整节是否渲染。"""
    rows = list(rows)
    if not rows:
        return ''
    head = ''.join(f'<th{_numeric_class(i, numeric_columns)}>{escape(str(h))}</th>'
                   for i, h in enumerate(headers))
    body = ''.join(
        '<tr>' + ''.join(
            f'<td{_numeric_class(i, numeric_columns)}>'
            f'{cell if isinstance(cell, Raw) else escape("" if cell is None else str(cell))}</td>'
            for i, cell in enumerate(row)) + '</tr>'
        for row in rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def section(title: str, body: str) -> str:
    """空 body 直接不出这一节，避免页面上出现一堆空壳标题。"""
    return '' if not body else f'<section><h2>{escape(title)}</h2>{body}</section>'


def header(title: Any, badges: str = '', summary: str = '') -> str:
    return (f'<header><h1>{escape(str(title))}{badges}</h1>'
            + (f'<div class="sum">{summary}</div>' if summary else '')
            + '</header>')


def page(title: str, header_html: str, body_html: str, extra_css: str = '') -> str:
    """拼出完整单页 HTML：title 与 header/body 由调用方保证已转义。"""
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title>'
            f'<style>{PAGE_CSS}{extra_css}</style></head><body>'
            f'{header_html}<main>{body_html}</main></body></html>')


def link_list(items: Sequence[Any]) -> str:
    """(名称, URL) 列表 -> 可点击清单。URL 为空的项跳过。"""
    entries: List[str] = []
    for name, url in items:
        if not url:
            continue
        entries.append(f'<li><a href="{escape(str(url), quote=True)}" target="_blank">'
                       f'{escape(str(name))}</a></li>')
    return f'<ul class="meta">{"".join(entries)}</ul>' if entries else ''
