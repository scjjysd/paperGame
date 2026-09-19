"""annotation_repair 的关键断言：补回丢弃部件、保持单连通、关节吸附、扩框护栏、门禁时序。"""
import cv2
import logging
import numpy as np
import pytest
import yaml
from contextlib import contextmanager
from PIL import Image
from scipy import ndimage
from skimage.morphology import skeletonize

from app.services import annotation_repair
from app.services.annotation_repair import (MAX_JOINT_OFFSET, MESH_GRID, PAD_RATIO,
                                            _candidate, extend_limb_tips,
                                            _bridge_width, _pad_box, _vendor_resized,
                                            downscale_annotation_for_render, mesh_unreachable,
                                            project_joints_to_axis,
                                            rebuild_mask, repair_or_reject, stroke_loss,
                                            worst_joint_offset)
from app.services.annotations import NeedsCorrection

# 生产代码不用绝对阈值（改用「不差于 vendor 原始标注」的相对基线），
# 这里只用于判定合成形状是否属于「已知会断开」那一类。
DISCONNECTED = 2

SIZE = 300
HALO = ((150, 60), 25)              # 与身体断开的头环：圆心 + 半径
BODY = ((120, 150), (180, 280))       # (x0,y0), (x1,y1)


def _drawing(size=SIZE, halo=True):
    """白底 + 实心身体 + 一个断开的头环（复现 s08 天使的拓扑）。"""
    img = np.full((size, size, 3), 255, np.uint8)
    (x0, y0), (x1, y1) = BODY
    cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), -1)
    if halo:
        cv2.circle(img, HALO[0], HALO[1], (0, 0, 0), 4)
    return img


def _body_only_mask():
    """vendor segment() 的行为：只保留最大连通轮廓，头环被丢弃。"""
    mask = np.zeros((SIZE, SIZE), np.uint8)
    (x0, y0), (x1, y1) = BODY
    mask[y0:y1, x0:x1] = 255
    return mask


def _skeleton(neck_loc, root_loc=(150, 250)):
    return [{'name': 'root', 'loc': list(root_loc), 'parent': None},
            {'name': 'neck', 'loc': list(neck_loc), 'parent': 'root'}]


def _write_anno(tmp_path, texture_bgr, mask, skeleton):
    """只写 texture/mask/char_cfg —— 走 repair_or_reject 的「无 image.png」回退路径。"""
    anno = tmp_path / 'anno'
    anno.mkdir()
    cv2.imwrite(str(anno / 'texture.png'), cv2.cvtColor(texture_bgr, cv2.COLOR_BGR2BGRA))
    Image.fromarray(mask).save(anno / 'mask.png')
    (anno / 'char_cfg.yaml').write_text(yaml.safe_dump(
        {'skeleton': skeleton, 'height': mask.shape[0], 'width': mask.shape[1]}))
    return anno


# ---------- mask 重建 ----------

def test_detached_halo_is_recovered_and_stays_single_connected():
    repaired = rebuild_mask(_drawing(), _body_only_mask())

    assert repaired.dtype == np.uint8 and set(np.unique(repaired)) <= {0, 255}
    assert repaired.shape == (SIZE, SIZE)                 # vendor 断言 mask 尺寸 == char_cfg 尺寸
    assert repaired[HALO[0][1] - HALO[1], HALO[0][0]] == 255    # 头环顶部被补回
    assert repaired[250, 150] == 255                             # 身体仍在
    # 必须单连通，否则 _generate_mesh 只取最长轮廓，补了也白补
    assert ndimage.label(repaired > 0)[1] == 1
    assert not repaired[0, :].any() and not repaired[-1, :].any()
    assert not repaired[:, 0].any() and not repaired[:, -1].any()
    assert repaired.flags['C_CONTIGUOUS']                 # cv2.line 要求连续内存


def test_clean_mask_is_left_alone():
    """已覆盖全部笔画的 mask 不应被膨胀成一坨白斑。"""
    texture = np.full((SIZE, SIZE, 3), 255, np.uint8)
    cv2.rectangle(texture, (120, 150), (180, 280), (0, 0, 0), -1)
    mask = np.zeros((SIZE, SIZE), np.uint8)
    mask[148:282, 118:182] = 255
    repaired = rebuild_mask(texture, mask)
    assert abs((repaired > 0).sum() / (mask > 0).sum() - 1) < 0.02


def test_non_contiguous_base_mask_does_not_crash():
    """vendor segment() 返回 mask.T（转置视图）；不 ascontiguousarray 时 cv2.line 会抛异常。"""
    transposed = np.asfortranarray(_body_only_mask())
    assert not transposed.flags['C_CONTIGUOUS']
    assert rebuild_mask(_drawing(), transposed).shape == (SIZE, SIZE)


# ---------- 关节吸附（F1 / D4）----------

def test_out_of_mask_joints_are_snapped_onto_medial_axis():
    mask = rebuild_mask(_drawing(), _body_only_mask())
    skeleton = _skeleton(neck_loc=(150, 60))               # 头环圆心，填洞后已在 mask 内
    skeleton.append({'name': 'left_hand', 'loc': [90, 200], 'parent': 'neck'})   # 身体左侧外

    snapped, moved = project_joints_to_axis(skeleton, mask)

    assert moved == 1
    assert snapped[1]['loc'] == [150, 60]                  # 原本就在 mask 内的不动
    x, y = snapped[2]['loc']
    assert mask[y, x] == 255                               # 吸附后必然落在 mask 内
    # 必须落在中轴而非边缘：边缘点仍会被 ARAP 丢弃 pin。
    # 注：distance_transform_edt(X) 算的是「到最近 False 像素」的距离，所以传 mask>0
    # 得到的是「距 mask 边缘」；身体宽 60px，中轴距边约 25px。
    assert ndimage.distance_transform_edt(mask > 0)[y, x] > 10
    assert skeletonize(mask > 0)[y, x]


def test_worst_joint_offset_is_normalized_to_diagonal():
    mask = _body_only_mask()
    offset, name = worst_joint_offset(mask, _skeleton(neck_loc=(10, 10)))
    diagonal = float(np.hypot(SIZE, SIZE))
    expected = float(ndimage.distance_transform_edt(~(mask > 0))[10, 10]) / diagonal
    assert name == 'neck'
    assert offset == pytest.approx(expected, rel=1e-6)
    assert offset > MAX_JOINT_OFFSET


# ---------- 门禁时序（最易出错的一处）----------

def test_gate_measures_pre_projection_offset_and_still_persists_snapped_joints(tmp_path):
    """吸附会让关节偏移恒为 0；门禁必须在吸附前度量，否则永久失效。

    同时验证：即使门禁拒绝，也已写回吸附后的骨架 —— 骨架确认页要拿到落在身上的点，
    而不是空白处的原始猜测。
    """
    anno = _write_anno(tmp_path, _drawing(), _body_only_mask(), _skeleton(neck_loc=(10, 10)))

    with pytest.raises(NeedsCorrection) as e:
        repair_or_reject(anno)

    assert e.value.reason == 'SKELETON_MISFIT'
    assert 'neck' in e.value.detail
    saved = yaml.safe_load((anno / 'char_cfg.yaml').read_text())['skeleton']
    mask = np.array(Image.open(anno / 'mask.png'))
    x, y = dict((j['name'], j['loc']) for j in saved)['neck']
    assert mask[y, x] > 0, '拒绝时也应写回吸附后的关节，供确认页使用'


def test_in_range_skeleton_passes_gate_and_reports_diagnostics(tmp_path):
    anno = _write_anno(tmp_path, _drawing(), _body_only_mask(), _skeleton(neck_loc=(150, 60)))

    info = repair_or_reject(anno)

    assert info['offset'] <= MAX_JOINT_OFFSET and info['padded'] is False
    mask = np.array(Image.open(anno / 'mask.png'))
    assert mask[HALO[0][1] - HALO[1], HALO[0][0]] == 255      # 修复已落盘
    assert ndimage.label(mask > 0)[1] == 1


def test_written_texture_is_four_channel_rgba(tmp_path):
    """vendor _load_txtr 断言 texture 必须 RGBA；写成 3 通道会让渲染直接 assert False。"""
    anno = _write_anno(tmp_path, _drawing(), _body_only_mask(), _skeleton(neck_loc=(150, 60)))
    repair_or_reject(anno)
    assert cv2.imread(str(anno / 'texture.png'), cv2.IMREAD_UNCHANGED).shape[2] == 4


# ---------- 扩框（D0）----------

def test_pad_box_expands_by_ratio_and_clips_to_image():
    box = {'top': 10, 'bottom': 110, 'left': 5, 'right': 195}
    top, bottom, left, right = _pad_box(box, height=1000, width=200)
    dy, dx = int(100 * PAD_RATIO), int(190 * PAD_RATIO)
    assert top == 0 and left == 0 and right == 200       # 三侧被裁到图像边界
    assert bottom == 110 + dy                            # 下侧未触边，正常外扩


def test_out_of_crop_joint_does_not_crash_and_counts_as_far():
    """pose 模型会输出 crop 之外的关节；不夹边界会 IndexError，且不能当成「偏移 0」放过门禁。"""
    mask = _body_only_mask()
    offset, name = worst_joint_offset(mask, _skeleton(neck_loc=(9999, 9999)))
    assert name == 'neck' and offset > MAX_JOINT_OFFSET
    snapped, moved = project_joints_to_axis(_skeleton(neck_loc=(9999, 9999)), mask)
    assert moved == 1
    x, y = snapped[1]['loc']
    assert 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and mask[y, x] > 0


# ---------- 网格连通性（死锁护栏）----------

def _two_lobes(neck: int, size: int = 400) -> np.ndarray:
    """单一连通但带细颈的 mask——桥接可能产出的形状。"""
    m = np.zeros((size, size), np.uint8)
    m[60:200, 60:200] = 255
    m[60:200, 240:380] = 255
    m[120:120 + neck, 200:240] = 255
    return m


def test_thin_neck_mask_is_detected_as_mesh_disconnected():
    """颈宽小于 vendor 网格间距时，三角面会被「质心必须在轮廓内」全部过滤掉，
    网格断成两片 → ARAP 刚度矩阵奇异 → vendor `while det == 0` 扰动循环死锁。

    实测（400px 图、间距 10.3px）：颈宽 3~8px 断开（211 不可达），≥12px 连通。
    """
    assert mesh_unreachable(_two_lobes(4)) > DISCONNECTED     # 4px = 旧的固定桥宽，死区中央
    assert mesh_unreachable(_two_lobes(8)) > DISCONNECTED
    assert mesh_unreachable(_two_lobes(20)) <= DISCONNECTED   # 宽到能容下三角面


def test_bridge_width_scales_with_mesh_grid_spacing():
    """桥宽必须 ≥ 2× 网格间距；1.5× 实测仍会断开（300px 图上 12px 断、16px 通）。"""
    for dim in (200, 300, 400, 512, 1000):
        spacing = dim / (MESH_GRID - 1)
        assert _bridge_width((dim, dim)) >= spacing * 2, f'{dim}px 图：桥宽不足 2× 间距 {spacing:.1f}px'
    assert _bridge_width((30, 30)) == 4                              # 极小图保底


def test_bridged_mask_keeps_mesh_connected():
    """端到端：补回头环并桥接后的 mask，像素与三角网格都必须连通。"""
    repaired = rebuild_mask(_drawing(), _body_only_mask())
    assert ndimage.label(repaired > 0)[1] == 1                       # 像素连通
    assert mesh_unreachable(repaired) <= DISCONNECTED                  # 且网格连通


def test_falls_back_when_repaired_mask_would_deadlock(tmp_path, monkeypatch):
    """长桥会把三角网格切断 → vendor ARAP 卡在 det 扰动循环；必须降级而不是硬写。"""
    monkeypatch.setattr(annotation_repair, '_bridge_width', lambda shape: 4)   # 人为造出细颈
    assert mesh_unreachable(rebuild_mask(_drawing(), _body_only_mask())) > DISCONNECTED

    anno = _write_anno(tmp_path, _drawing(), _body_only_mask(), _skeleton(neck_loc=(150, 60)))
    repair_or_reject(anno)

    written = np.array(Image.open(anno / 'mask.png'))
    assert mesh_unreachable(written) <= DISCONNECTED                  # 已降级到 vendor 原始 mask
    assert written.sum() > 0


def test_vendor_resize_is_replicated_before_using_bounding_box():
    """image.png 存于 resize 之前、bbox 属于 resize 之后的坐标系；不复现会裁错区域。

    手机拍摄必然 >1000px，所以这是真实输入路径上的必要步骤。
    """
    big = np.full((4032, 3024, 3), 255, np.uint8)          # garlic 的真实尺寸
    resized = _vendor_resized(big)
    assert max(resized.shape[:2]) == 1000
    assert resized.shape == (1000, 750, 3)                # 与 vendor round(scale*w/h) 一致

    small = np.full((512, 512, 3), 255, np.uint8)          # ≤1000 不缩放，原样返回
    assert _vendor_resized(small) is small


def test_downscale_annotation_updates_pixels_dimensions_and_skeleton(tmp_path):
    mask = np.zeros((1000, 1000), np.uint8)
    mask[150:280, 120:180] = 255
    anno = _write_anno(tmp_path, _drawing(size=1000), mask,
                       _skeleton(neck_loc=(500, 600), root_loc=(500, 900)))

    assert downscale_annotation_for_render(anno, max_dim=800) is True

    cfg = yaml.safe_load((anno / 'char_cfg.yaml').read_text())
    texture = cv2.imread(str(anno / 'texture.png'), cv2.IMREAD_UNCHANGED)
    mask = np.array(Image.open(anno / 'mask.png'))
    assert (cfg['height'], cfg['width']) == (800, 800)
    assert texture.shape[:2] == mask.shape == (800, 800)
    skeleton = {j['name']: j['loc'] for j in cfg['skeleton']}
    assert skeleton['neck'] == [400, 480]
    assert skeleton['root'] == [400, 720]
    assert set(np.unique(mask)) <= {0, 255}
    assert downscale_annotation_for_render(anno, max_dim=800) is False


def test_clean_annotation_skips_mesh_candidate_checks(tmp_path, monkeypatch):
    clean = np.full((SIZE, SIZE, 3), 255, np.uint8)
    anno = _write_anno(tmp_path, clean, _body_only_mask(),
                       _skeleton(neck_loc=(150, 60)))
    cv2.imwrite(str(anno / 'image.png'), clean)
    (anno / 'bounding_box.yaml').write_text(yaml.safe_dump(
        {'top': 0, 'bottom': SIZE, 'left': 0, 'right': SIZE}))
    monkeypatch.setattr(annotation_repair, 'mesh_unreachable',
                        lambda *_: pytest.fail('clean annotation should skip mesh checks'))
    info = repair_or_reject(anno)
    assert info['crop'] == 'plain'


def test_repair_logs_fast_path_stage_timings(tmp_path, caplog):
    image = _drawing(halo=False)
    anno = _write_anno(tmp_path, image, _body_only_mask(),
                       _skeleton(neck_loc=(150, 200)))
    cv2.imwrite(str(anno / 'image.png'), image)
    (anno / 'bounding_box.yaml').write_text(yaml.safe_dump(
        {'top': 0, 'bottom': SIZE, 'left': 0, 'right': SIZE}))

    with caplog.at_level(logging.INFO):
        repair_or_reject(anno)

    stages = {record.message.split('stage=')[1].split()[0]
              for record in caplog.records if '标注修复计时' in record.message}
    assert {'baseline', 'clean_check', 'skeleton', 'write'} <= stages
    assert not any(stage.startswith(('mesh.', 'candidate.')) for stage in stages)


def test_skeleton_timing_covers_projection_and_tip_extension(tmp_path, monkeypatch):
    image = _drawing(halo=False)
    anno = _write_anno(tmp_path, image, _body_only_mask(),
                       _skeleton(neck_loc=(150, 200)))
    cv2.imwrite(str(anno / 'image.png'), image)
    (anno / 'bounding_box.yaml').write_text(yaml.safe_dump(
        {'top': 0, 'bottom': SIZE, 'left': 0, 'right': SIZE}))

    active = []
    calls = []

    @contextmanager
    def tracking_stage(_anno_dir, stage):
        active.append(stage)
        try:
            yield
        finally:
            active.pop()

    original_project = annotation_repair.project_joints_to_axis
    original_extend = annotation_repair.extend_limb_tips

    def tracked_project(*args, **kwargs):
        calls.append(('project', tuple(active)))
        return original_project(*args, **kwargs)

    def tracked_extend(*args, **kwargs):
        calls.append(('extend', tuple(active)))
        return original_extend(*args, **kwargs)

    monkeypatch.setattr(annotation_repair, '_timed_repair_stage', tracking_stage)
    monkeypatch.setattr(annotation_repair, 'project_joints_to_axis', tracked_project)
    monkeypatch.setattr(annotation_repair, 'extend_limb_tips', tracked_extend)
    repair_or_reject(anno)

    assert [name for name, _ in calls] == ['project', 'extend']
    assert all(active_stages == ('skeleton',) for _, active_stages in calls)


def test_repair_times_plain_rebuild_candidate(tmp_path, caplog):
    image = _drawing()
    anno = _write_anno(tmp_path, image[190:340, 160:240],
                       _body_only_mask()[190:340, 160:240],
                       _skeleton(neck_loc=(40, 20), root_loc=(40, 120)))
    cv2.imwrite(str(anno / 'image.png'), image)
    (anno / 'bounding_box.yaml').write_text(yaml.safe_dump(
        {'top': 190, 'bottom': 340, 'left': 160, 'right': 240}))

    with caplog.at_level(logging.INFO):
        with pytest.raises(NeedsCorrection):
            repair_or_reject(anno)

    stages = {record.message.split('stage=')[1].split()[0]
              for record in caplog.records if '标注修复计时' in record.message}
    assert 'candidate.plain' in stages


def test_repair_times_repaired_plain_without_image_or_bbox(tmp_path, caplog):
    anno = _write_anno(tmp_path, _drawing(halo=False), _body_only_mask(),
                       _skeleton(neck_loc=(150, 200)))

    with caplog.at_level(logging.INFO):
        repair_or_reject(anno)

    stages = {record.message.split('stage=')[1].split()[0]
              for record in caplog.records if '标注修复计时' in record.message}
    assert 'candidate.plain' in stages


def test_repair_logs_only_executed_candidate_and_mesh_stages(tmp_path, caplog):
    image = _drawing()
    anno = _write_anno(tmp_path, image[190:340, 160:240],
                       _body_only_mask()[190:340, 160:240],
                       _skeleton(neck_loc=(40, 20), root_loc=(40, 120)))
    cv2.imwrite(str(anno / 'image.png'), image)
    (anno / 'bounding_box.yaml').write_text(yaml.safe_dump(
        {'top': 190, 'bottom': 340, 'left': 160, 'right': 240}))

    with caplog.at_level(logging.INFO):
        with pytest.raises(NeedsCorrection):
            repair_or_reject(anno)

    stages = {record.message.split('stage=')[1].split()[0]
              for record in caplog.records if '标注修复计时' in record.message}
    assert {'candidate.plain', 'candidate.ink', 'candidate.pad',
            'mesh.baseline'} <= stages
    assert any(stage.startswith('mesh.') and stage != 'mesh.baseline'
               for stage in stages)


def test_padding_recovers_ink_clipped_by_detector_box(tmp_path):
    """检测框从头环中间切过时，扩框应把框上方那一段救回来并更新 char_cfg 尺寸。

    注意 PAD_RATIO=0.15 只能救回「略微被切」的部件，救不回完全远离主体的独立部件
    （真实 s08 属于前者：头环顶部被切 914px，扩框后全图丢失 15.6%→1.0%）。
    这类「略微被切」现在通常由更强的墨迹框接管，故只断言救回了，不指定用哪个框。
    """
    canvas = np.full((400, 400, 3), 255, np.uint8)
    cv2.rectangle(canvas, (170, 200), (230, 330), (0, 0, 0), -1)      # 身体
    cv2.circle(canvas, (200, 175), 25, (0, 0, 0), 4)                  # 头环，跨 y=146..204
    box = {'top': 190, 'bottom': 340, 'left': 160, 'right': 240}      # 从头环中间切过
    dy = int((box['bottom'] - box['top']) * PAD_RATIO)

    anno = tmp_path / 'anno'
    anno.mkdir()
    cv2.imwrite(str(anno / 'image.png'), canvas)
    (anno / 'bounding_box.yaml').write_text(yaml.safe_dump(box))
    # vendor 一定会写 texture.png（= 按 bbox 的裁切）与 mask.png；基线候选依赖它们
    plain_crop = canvas[box['top']:box['bottom'], box['left']:box['right']]
    cv2.imwrite(str(anno / 'texture.png'), cv2.cvtColor(plain_crop, cv2.COLOR_BGR2BGRA))
    baseline_mask = np.zeros(plain_crop.shape[:2], np.uint8)
    baseline_mask[10:140, 10:70] = 255                                # 只框住身体（vendor 行为）
    Image.fromarray(baseline_mask).save(anno / 'mask.png')
    # 关节在【原 crop】坐标系：crop = canvas[190:340,160:240] = 150行x80列，身体在 x[10,70] y[10,140]
    (anno / 'char_cfg.yaml').write_text(yaml.safe_dump(
        {'skeleton': _skeleton(neck_loc=(40, 20), root_loc=(40, 120)),
         'height': 150, 'width': 80}))

    info = repair_or_reject(anno)

    cfg = yaml.safe_load((anno / 'char_cfg.yaml').read_text())
    mask = np.array(Image.open(anno / 'mask.png'))
    assert info['padded'] is True and info['crop'] in ('ink', 'pad')
    assert mask.shape == (cfg['height'], cfg['width'])                # vendor 会断言两者一致
    assert mask.shape[0] > 150                                        # 框确实被扩大了
    # 新纳入的前 dy 行（= 原框上方、以前根本不存在的区域）必须有内容
    assert mask[:dy].any(), '扩框后应覆盖到原检测框上方的墨迹'
    texture = cv2.imread(str(anno / 'texture.png'), cv2.IMREAD_UNCHANGED)
    assert texture.shape[2] == 4                                      # vendor 断言 texture 必须 RGBA
    assert stroke_loss(cv2.cvtColor(texture[..., :3], cv2.COLOR_RGBA2BGR), mask) < 10.0


# ---- 末端外推（治「腿识别太上去」）----
# 竖直长条当一条腿：墨迹到 y=279
LEG_MASK_H, LEG_MASK_W, LEG_INK_BOTTOM = 300, 200, 280


def _leg_mask():
    mask = np.zeros((LEG_MASK_H, LEG_MASK_W), np.uint8)
    mask[100:LEG_INK_BOTTOM, 90:110] = 255
    return mask


def _leg_skeleton(foot_y):
    return [{'name': 'left_knee', 'loc': [100, 140], 'parent': 'left_hip'},
            {'name': 'left_foot', 'loc': [100, foot_y], 'parent': 'left_knee'}]


def test_末端控制点外推到墨迹末端():
    """pose 模型常把脚/手标在肢体中段（实测脚中位短 8%、最差 s14 36%；手中位 14%、最差 s15 53%），
    末端那截没有 pin，ARAP 会随邻近网格把它任意拖拽——这就是「手脚被拉长扭曲」。"""
    out, moved = extend_limb_tips(_leg_skeleton(200), _leg_mask())

    foot = next(j for j in out if j['name'] == 'left_foot')
    assert moved == 1
    assert foot['loc'][1] >= LEG_INK_BOTTOM - 10, f"脚只推到 y={foot['loc'][1]}，墨迹到 {LEG_INK_BOTTOM - 1}"


def test_末端已到位时不动():
    """已贴着墨迹末端的关节不能再动，否则每跑一次修复骨架就抖一次。"""
    out, moved = extend_limb_tips(_leg_skeleton(275), _leg_mask())

    assert moved == 0
    assert next(j for j in out if j['name'] == 'left_foot')['loc'] == [100, 275]


def test_外推不越出墨迹():
    """肢体拐弯时直线外推会立刻出 mask，此时必须放弃而不是把关节甩到空白处。"""
    mask = _leg_mask()
    mask[210:LEG_INK_BOTTOM, 90:110] = 0          # 腿在 y=210 断掉，下面是空白
    out, moved = extend_limb_tips(_leg_skeleton(150), mask)

    foot = next(j for j in out if j['name'] == 'left_foot')
    assert mask[foot['loc'][1], foot['loc'][0]] > 0, '关节被推到了墨迹之外'
    assert foot['loc'][1] < 210


def test_按墨迹重裁救回框外的整块画(tmp_path):
    """pose 框只框住画的一部分时，按比例扩框救不回来，必须按墨迹真实范围重裁。

    复现 s07（横躺的猪）：检测框只框住右半边，框外还有 41.7% 的墨迹，
    PAD_RATIO=15% 只能往外推十几像素、差 240px。按墨迹包围盒重裁后丢失率降到 0.5%。
    """
    canvas = np.full((400, 400, 3), 255, np.uint8)
    cv2.rectangle(canvas, (60, 180), (320, 270), (0, 0, 0), -1)     # 横躺的身体，墨迹 x∈[60,320]
    box = {'top': 170, 'bottom': 280, 'left': 240, 'right': 330}    # 只框住右侧约 1/3
    assert box['left'] - int((box['right'] - box['left']) * PAD_RATIO) > 60, '前提：扩框够不到左端'

    anno = tmp_path / 'anno'
    anno.mkdir()
    cv2.imwrite(str(anno / 'image.png'), canvas)
    (anno / 'bounding_box.yaml').write_text(yaml.safe_dump(box))
    crop = canvas[box['top']:box['bottom'], box['left']:box['right']]
    cv2.imwrite(str(anno / 'texture.png'), cv2.cvtColor(crop, cv2.COLOR_BGR2BGRA))
    baseline = np.zeros(crop.shape[:2], np.uint8)
    baseline[10:100, 5:85] = 255                                    # vendor 只拿到框内那一段
    Image.fromarray(baseline).save(anno / 'mask.png')
    (anno / 'char_cfg.yaml').write_text(yaml.safe_dump(
        {'skeleton': _skeleton(neck_loc=(40, 20), root_loc=(40, 90)),
         'height': crop.shape[0], 'width': crop.shape[1]}))

    info = repair_or_reject(anno)

    cfg = yaml.safe_load((anno / 'char_cfg.yaml').read_text())
    mask = np.array(Image.open(anno / 'mask.png'))
    assert info['crop'] == 'ink', f"应采用墨迹框，实际 {info['crop']}"
    assert mask.shape == (cfg['height'], cfg['width'])
    assert cfg['width'] > box['right'] - box['left'], '框没被扩到墨迹左端'
    # 原检测框左边界在新 crop 里的位置：左侧这一整块以前根本不在 texture 里
    assert mask[:, :box['left'] - 60].any(), '重裁后应覆盖到原框左侧的墨迹'
    # 关节坐标必须跟着框一起平移，否则 ARAP 拿错位的 pin 驱动网格（s07 就是这样被拧歪压扁的）。
    # 这两个关节换框后仍落在 mask 内，不会被吸附改写，所以可以直接查坐标。
    loc = {j['name']: j['loc'] for j in cfg['skeleton']}
    shift = box['left'] - 60                     # 新框左边界 = 墨迹左端 x=60
    assert loc['root'] == [40 + shift, 90] and loc['neck'] == [40 + shift, 20], loc


def test_换框后关节跟着平移(tmp_path):
    """char_cfg 的关节坐标相对 vendor 那个检测框；换更大的框后必须整体平移。

    不平移的后果按框差大小放大：15% 扩框只偏框宽的 15%（勉强能目视通过，所以一直没暴露），
    按墨迹重裁可能偏几百像素——实测 s07 偏 239px，ARAP 拿错位的 pin 驱动网格，
    把水平的猪拧歪并压扁 31%（首帧宽高比 1.62→1.12）。
    """
    canvas = np.full((400, 400, 3), 255, np.uint8)
    cv2.rectangle(canvas, (250, 180), (320, 270), (0, 0, 0), -1)
    ref = {'top': 170, 'bottom': 280, 'left': 240, 'right': 330}      # vendor 的检测框
    wide = {'top': 160, 'bottom': 280, 'left': 60, 'right': 330}      # 按墨迹重裁后的框
    skeleton = [{'name': 'root', 'loc': [45, 50], 'parent': None}]    # 相对 ref crop，落在身体上

    cand = _candidate(canvas, wide, skeleton, ref_box=ref)

    x, y = cand['skeleton'][0]['loc']
    assert (x, y) == (45 + (ref['left'] - wide['left']), 50 + (ref['top'] - wide['top']))
    # (x, y) 相对新框，换回全图坐标应仍是原来那一处墨迹
    assert canvas[y + wide['top'], x + wide['left']].tolist() == [0, 0, 0]
