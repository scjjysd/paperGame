"""review_batch 汇总页自检：纯字符串生成，离线可跑（无需 TorchServe / OpenGL）。"""
from scripts.review_batch import build_html

READY = {
    'name': 's01',
    'status': 'ready',
    'elapsed': 21.3,
    'source': 's01/source.png',
    'mask_overlay': 's01/mask_overlay.png',
    'texture': 's01/anno/texture.png',
    'joints': 16,
    'animations': {
        'run': {'gif': 's01/run/run.gif', 'sheet': 's01/run/run.png',
                'frameCount': 13, 'frameWidth': 220, 'frameHeight': 300, 'fps': 12},
        'jump': {'gif': 's01/jump/jump.gif', 'sheet': 's01/jump/jump.png',
                 'frameCount': 12, 'frameWidth': 220, 'frameHeight': 300, 'fps': 12},
    },
}
GATED = {
    'name': 's07',
    'status': 'needs_correction',
    'reason': 'SKELETON_MISFIT',
    'detail': 'joint left_hand is 31.2% of diagonal away',
    'elapsed': 8.4,
    'source': 's07/source.png',
    'mask_overlay': 's07/mask_overlay.png',
    'texture': 's07/anno/texture.png',
    'joints': 16,
    'animations': {},
}
CRASHED = {
    'name': 's13',
    'status': 'failed',
    'detail': 'RuntimeError: <glfw> init failed & aborted',
    'elapsed': 3.0,
    'source': 's13/source.png',
    'mask_overlay': None,
    'texture': None,
    'joints': 0,
    'animations': {},
}


def test_all_three_statuses_render_a_card_with_status_text():
    html = build_html([READY, GATED, CRASHED])
    for r in (READY, GATED, CRASHED):
        assert f'id="card-{r["name"]}"' in html
    assert 'ready' in html and 'needs_correction' in html and 'failed' in html
    assert 'SKELETON_MISFIT' in html


def test_ready_card_embeds_every_key_image():
    html = build_html([READY])
    for path in ('s01/source.png', 's01/run/run.gif', 's01/jump/jump.gif',
                 's01/mask_overlay.png', 's01/anno/texture.png',
                 's01/run/run.png', 's01/jump/jump.png'):
        assert f'src="{path}"' in html, f'缺少关键图片 {path}'
    assert '13' in html and '16' in html          # 帧数与关节数可见


def test_problem_cards_are_sorted_before_ready_ones():
    html = build_html([READY, GATED, CRASHED])
    assert html.index('id="card-s13"') < html.index('id="card-s01"')
    assert html.index('id="card-s07"') < html.index('id="card-s01"')


def test_detail_text_is_html_escaped():
    """detail 来自 vendor 异常消息，含 < & 时不得破坏页面结构。"""
    html = build_html([CRASHED])
    assert '<glfw>' not in html
    assert '&lt;glfw&gt;' in html and '&amp;' in html


def test_summary_counts_each_status():
    html = build_html([READY, GATED, CRASHED])
    assert 'ready 1' in html and 'needs_correction 1' in html and 'failed 1' in html
    assert '共 3' in html


def test_missing_images_leave_no_broken_src():
    html = build_html([CRASHED])
    assert 'src="None"' not in html and 'src=""' not in html
