"""标注修复与质量门禁：夹在 ①分析 与 ②渲染 之间，零改动 vendor。

vendor 的标注产物有四类缺陷，本模块逐一对治（编号沿用 docs/animation-spike-results.md 第⑥节）：

  D0 检测框裁切   人形检测器给的 bbox 没框住整张画，框外墨迹根本不进 texture.png。
                  实测 s09 有 38% 的墨迹被裁掉、s08 的头环顶部被切。
                  → 按 bbox 尺寸外扩重裁，仅当指标严格更好时才采用（见 _better）。
  D1 只留最大轮廓 segment() 的 retain largest contour 丢弃与身体断开的部件（头环、整块头部）。
  D2 mask 外透明  _load_txtr() 把 mask 之外的纹理像素强制 alpha=0，被丢弃的部件彻底消失。
  D3 网格只取最长轮廓 _generate_mesh() 只用 contours[0]，故修复后的 mask 必须单连通。
                  → D1/D2/D3 由 rebuild_mask 一并解决：补回被丢弃的部件并桥接为单连通。
  D4 ARAP 丢 pin  关节落在网格三角面之外时该 pin 被永久丢弃，肢体失去驱动；
                  pose 模型把关节估到体外时还会用悬空骨骼拖拽网格产生剪切变形。
                  → project_joints_to_axis 把关节吸附到 mask 中轴。

门禁：吸附前若仍有关节远离角色，说明 pose 估计本身不可信，抛 NeedsCorrection 走
契约已有的 needs_correction 通道（返回 mask + 吸附后的关节给骨架确认页）。
"""
import math
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import yaml
from PIL import Image
from scipy import ndimage
from scipy.spatial import Delaunay
from shapely import geometry
from skimage import measure
from skimage.morphology import skeletonize

from app.services.annotations import NeedsCorrection

# ponytail: 25% 是「离谱到吸附也救不回」的防线，不是精调值。20 份样本经 F1 吸附后
# 目视均可用（最大 s15 吸附前偏 13.0%），故没有一个会触发；它防的是未采样到的灾难性
# pose 失败。真实儿童画复测后用 scripts/diag/diagnose_annotations.py 重新标定。
MAX_JOINT_OFFSET = 0.25
# ponytail: 15% 外扩在 13 份线稿样本上均严格改善（s08 2.1→1.0%、s09 2.3→1.2%），
# 且彩色样本 s01 会被 _better 护栏自动回退。上限是「扩太多会引入背景纹理」，
# 真实照片输入（F6 待办）落地后需重标定。
PAD_RATIO = 0.15
VENDOR_MAX_DIM = 1000     # vendor 存 image.png 在 resize 之前，bbox 却在 resize 之后的坐标系
MIN_PART_RATIO = 0.002    # 小于此面积占比的丢失部件视为碎点噪声，不补
SKIP_RATIO = 0.004        # 丢失总量低于此占比视为干净样本，mask 原样不动
MESH_GRID = 40            # vendor _generate_mesh 的 linspace(0, img_dim, 40) 内部顶点数
BORDER_MARGIN = 2         # vendor 要求 mask 不触边，否则 contour 查找失败
RENDER_MAX_DIM = 800      # ARAP 前的最长边；保留原始标注尺寸，渲染副本可降采样


def _bridge_width(mask_shape) -> int:
    """桥宽必须 ≥ 2× vendor 网格间距（img_dim/(MESH_GRID-1)），否则桥内落不到三角面。

    实测（300px 图、间距 7.7px、头环距身体 65px 的长桥）：
      桥宽 12px（1.5×间距）→ 网格断开（122 不可达顶点）→ ARAP 矩阵奇异 → det 扰动循环死锁；
      桥宽 16px（2×间距）→ 连通（不可达 1）。
    断开与否取决于桥的「长宽比」而非单纯宽度，长桥需要更宽；取 2× 并把残余风险
    交给 repair_or_reject 里的 mesh 降级阶梯兜底。代价是桥的白底面积略增（可忽略）。
    """
    return max(4, int(np.ceil(max(mask_shape) / (MESH_GRID - 1) * 2)))


def _ink(texture_bgr: np.ndarray) -> np.ndarray:
    """笔画二值化，参数与 vendor segment() 逐字一致，保证与 mask 同坐标系可比。"""
    grey = np.min(texture_bgr, axis=2)
    thresh = cv2.adaptiveThreshold(grey, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, 115, 8)
    return cv2.bitwise_not(thresh) > 0


def _shortest_bridge(canvas: np.ndarray, comp: np.ndarray, nearest: np.ndarray,
                     width: int) -> None:
    """在 comp 上取离锚区最近的点，画一条最短线过去（面积代价最小）。

    nearest 为锚区的 distance_transform_edt(return_indices=True) 结果，已编码「每像素→最近锚点」。
    """
    pts = np.argwhere(comp)
    qy = nearest[0][pts[:, 0], pts[:, 1]]
    qx = nearest[1][pts[:, 0], pts[:, 1]]
    k = int(np.argmin(np.hypot(pts[:, 0] - qy, pts[:, 1] - qx)))
    cv2.line(canvas, (int(pts[k, 1]), int(pts[k, 0])), (int(qx[k]), int(qy[k])), 255, width)


def _single_connected(mask: np.ndarray) -> np.ndarray:
    """收敛到单连通：小部件全部桥接到最大部件（D3）。清边可能把桥切断，所以放在最后。"""
    labels, count = ndimage.label(mask)
    if count <= 1:
        return mask
    canvas = mask.astype(np.uint8) * 255
    bridge_w = _bridge_width(mask.shape)
    sizes = ndimage.sum(mask, labels, range(1, count + 1))
    keep = labels == (int(np.argmax(sizes)) + 1)
    nearest = ndimage.distance_transform_edt(~keep, return_indices=True)[1]
    for i in range(1, count + 1):
        comp = labels == i
        if comp.any() and not (comp & keep).any():
            _shortest_bridge(canvas, comp, nearest, bridge_w)
    return ndimage.binary_fill_holes(canvas > 0)


def _clear_border(mask: np.ndarray) -> np.ndarray:
    mask[:BORDER_MARGIN, :] = mask[-BORDER_MARGIN:, :] = False
    mask[:, :BORDER_MARGIN] = mask[:, -BORDER_MARGIN:] = False
    return mask


def rebuild_mask(texture_bgr: np.ndarray, base_mask: np.ndarray) -> np.ndarray:
    """只把 vendor 丢弃的笔画部件补回 base_mask，返回 uint8 0/255、单连通、不触边的 mask。

    以 base_mask 为基底原封不动，干净样本几乎零改动（避免整体膨胀成一坨白斑）。
    """
    height, width = base_mask.shape[:2]
    base = base_mask > 0
    # cv2.line 要求连续内存；vendor segment() 返回 mask.T 是转置视图，不 ascontiguousarray 会崩
    canvas = np.ascontiguousarray(base.astype(np.uint8) * 255)
    missing = _ink(texture_bgr) & ~base

    if missing.sum() >= SKIP_RATIO * height * width and base.any():
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        parts = cv2.dilate(missing.astype(np.uint8), kernel, iterations=2) > 0
        count, labels, stats, _ = cv2.connectedComponentsWithStats(parts.astype(np.uint8))
        nearest = ndimage.distance_transform_edt(~base, return_indices=True)[1]
        bridge_w = _bridge_width(base_mask.shape)
        for i in range(1, count):
            if stats[i, cv2.CC_STAT_AREA] < MIN_PART_RATIO * height * width:
                continue
            comp = labels == i
            canvas[comp] = 255
            _shortest_bridge(canvas, comp, nearest, bridge_w)
        base = canvas > 0

    mask = _clear_border(ndimage.binary_fill_holes(base))
    return np.ascontiguousarray(_single_connected(mask).astype(np.uint8) * 255)


def stroke_loss(texture_bgr: np.ndarray, mask: np.ndarray) -> float:
    """原图笔画落在 mask 之外的百分比。这些像素会被 _load_txtr 强制透明，即成片里彻底消失。"""
    ink = _ink(texture_bgr)
    return 100.0 * (ink & ~(mask > 0)).sum() / max(int(ink.sum()), 1)


def worst_joint_offset(mask: np.ndarray, skeleton: Sequence[Dict]) -> Tuple[float, str]:
    """最远关节到 mask 的距离，归一化到 crop 对角线。

    必须传【吸附前】的 skeleton：吸附后关节按定义落在 mask 内，度量恒为 0、门禁会永久失效。
    关节可能在 crop 之外（pose 模型会输出框外坐标），这时把越界距离一并计入，
    否则全部越界的骨架会被当成「偏移 0」放过门禁。
    """
    height, width = mask.shape[:2]
    distance = ndimage.distance_transform_edt(~(mask > 0))
    diagonal = float(np.hypot(width, height))
    worst, name = -1.0, ''
    for joint in skeleton:
        x, y = int(joint['loc'][0]), int(joint['loc'][1])
        cx, cy = min(max(x, 0), width - 1), min(max(y, 0), height - 1)
        ratio = (float(distance[cy, cx]) + float(np.hypot(x - cx, y - cy))) / diagonal
        if ratio > worst:
            worst, name = ratio, joint['name']
    return max(worst, 0.0), name


def project_joints_to_axis(skeleton: Sequence[Dict], mask: np.ndarray) -> Tuple[List[Dict], int]:
    """把 mask 外的关节吸附到 mask 中轴（D4）。返回 (新 skeleton, 吸附个数)。

    必须是中轴而非「最近的 mask 像素」：网格三角面只覆盖 mask 内部区域（内部顶点是固定
    40×40 网格），落在边缘的关节仍会被 ARAP 丢弃 pin（实测 s08 只从 22 降到 4，中轴才降到 0）。
    """
    inside = mask > 0
    axis = skeletonize(inside)
    if not axis.any():
        return list(skeleton), 0
    nearest = ndimage.distance_transform_edt(~axis, return_indices=True)[1]
    out, moved = [], 0
    for joint in skeleton:
        x, y = int(joint['loc'][0]), int(joint['loc'][1])
        if 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and inside[y, x]:
            out.append(dict(joint))
            continue
        # 关节可能在 crop 之外，先夹到边界内再查最近中轴点（否则索引越界崩溃）
        cx, cy = min(max(x, 0), mask.shape[1] - 1), min(max(y, 0), mask.shape[0] - 1)
        out.append({**joint, 'loc': [int(nearest[1][cy, cx]), int(nearest[0][cy, cx])]})
        moved += 1
    return out, moved


# 末端外推的四对 (基点, 末端)：只有手脚需要，肘/膝本身就在肢体中段
TIP_PAIRS = (('left_knee', 'left_foot'), ('right_knee', 'right_foot'),
             ('left_elbow', 'left_hand'), ('right_elbow', 'right_hand'))
# ponytail: 5% 是「抖动阈」而非精调值——低于图短边 5% 的收益抵不过每跑一次修复关节就挪一下。
# 依据 20 份代理样本实测缺口分布：脚中位 8.2%、手中位 14.1%，5% 之下的样本本来也无需外推。
TIP_MIN_GAIN = 0.05
TIP_GAP = 3        # 容许跨过的墨迹断口像素：手绘笔画（尤其马克笔）常有几像素细缝


def extend_limb_tips(skeleton: Sequence[Dict], mask: np.ndarray) -> Tuple[List[Dict], int]:
    """把手/脚控制点沿肢体方向外推到墨迹末端。返回 (新 skeleton, 外推个数)。

    pose 模型常把末端标在肢体中段（实测脚中位短 8.2%、最差 s14 35.7%；手中位 14.1%、
    最差 s15 53.1%），那截没有 pin 的墨迹会被 ARAP 随邻近网格任意拖拽，就是用户看到的
    「手脚被拉长扭曲」「腿识别太上去了」。

    ponytail: 只沿 base->tip 直线外推。天花板是拐弯的肢体（脚掌横着补一笔）推不到位；
    升级路径是沿 mask 中轴做测地线追踪。
    """
    loc = {j['name']: (int(j['loc'][0]), int(j['loc'][1])) for j in skeleton}
    h, w = mask.shape[:2]
    min_gain = max(min(h, w) * TIP_MIN_GAIN, 1.0)
    moved: Dict[str, List[int]] = {}
    for base, tip in TIP_PAIRS:
        if base not in loc or tip not in loc:
            continue
        (bx, by), (tx, ty) = loc[base], loc[tip]
        length = math.hypot(tx - bx, ty - by)
        if length < 1:
            continue
        dx, dy = (tx - bx) / length, (ty - by) / length
        best, gap, step = None, 0, 1
        while True:
            x, y = int(round(tx + dx * step)), int(round(ty + dy * step))
            if not (0 <= x < w and 0 <= y < h):
                break
            if mask[y, x] > 0:
                best, gap = (x, y), 0
            else:
                gap += 1
                if gap > TIP_GAP:
                    break
            step += 1
        if best is not None and math.hypot(best[0] - tx, best[1] - ty) >= min_gain:
            moved[tip] = [best[0], best[1]]
    return ([{**j, 'loc': moved[j['name']]} if j['name'] in moved else dict(j) for j in skeleton],
            len(moved))


def _vendor_resized(image: np.ndarray) -> np.ndarray:
    """复现 vendor image_to_annotations 的缩放，使 image 与 bounding_box.yaml 同坐标系。

    vendor 先 `cv2.imwrite(outdir/'image.png', img)` 存下原图，再做
    `if np.max(img.shape) > 1000: img = cv2.resize(...)`，最后用 bbox 裁切。
    所以 image.png 与 bbox 不同坐标系。不复现这一步，扩框会裁到完全错误的区域
    （实测 garlic 3024x4032 重裁后只能复现 58% 的 mask）——而手机拍摄必然超过 1000px。
    """
    longest = max(image.shape[:2])
    if longest <= VENDOR_MAX_DIM:
        return image
    scale = VENDOR_MAX_DIM / longest
    return cv2.resize(image, (round(scale * image.shape[1]), round(scale * image.shape[0])))


def downscale_annotation_for_render(anno_dir, max_dim: Optional[int] = None) -> bool:
    """按比例缩小已修复标注，降低 vendor ARAP 的网格与矩阵规模。

    该步骤只应在 repair 完成后调用：此时 bounding_box/image 已不再参与坐标推导，
    texture、mask 和 skeleton 可以安全地按同一比例变换。返回是否实际缩放。
    """
    anno_dir = Path(anno_dir)
    cfg_path = anno_dir / 'char_cfg.yaml'
    cfg = yaml.safe_load(cfg_path.read_text())
    height, width = int(cfg['height']), int(cfg['width'])
    if max_dim is None:
        max_dim = os.environ.get('RENDER_MAX_DIM', str(RENDER_MAX_DIM))
    limit = int(max_dim)
    if limit <= 0:
        raise ValueError('max_dim must be positive')
    longest = max(height, width)
    if longest <= limit:
        return False

    scale = limit / float(longest)
    new_width = max(1, round(width * scale))
    new_height = max(1, round(height * scale))
    texture_path = anno_dir / 'texture.png'
    mask_path = anno_dir / 'mask.png'
    texture = cv2.imread(str(texture_path), cv2.IMREAD_UNCHANGED)
    mask = np.array(Image.open(mask_path))
    if texture is None or texture.shape[:2] != (height, width) or mask.shape[:2] != (height, width):
        raise NeedsCorrection('NO_CONTOUR', f'invalid annotation dimensions: {anno_dir}')

    texture = cv2.resize(texture, (new_width, new_height), interpolation=cv2.INTER_AREA)
    mask = cv2.resize(mask, (new_width, new_height), interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(str(texture_path), texture)
    Image.fromarray(np.ascontiguousarray(mask)).save(mask_path)
    for joint in cfg['skeleton']:
        joint['loc'] = [round(float(joint['loc'][0]) * scale),
                        round(float(joint['loc'][1]) * scale)]
    cfg.update({'height': new_height, 'width': new_width})
    cfg_path.write_text(yaml.safe_dump(cfg))
    return True


def loss_in_image_frame(image: np.ndarray, box: Dict, mask: np.ndarray) -> float:
    """把 mask 映射回原图坐标系后，全图笔画有多少没被覆盖。

    比较两个不同大小的 crop 时必须用这个而不是 stroke_loss：后者分母是各自 crop 的
    墨迹量，不扩框的 crop 里被裁掉的部件根本不存在，丢失率反而接近 0，会得出相反结论。
    """
    ink = _ink(image)
    covered = np.zeros(ink.shape, bool)
    covered[box['top']:box['bottom'], box['left']:box['right']] = mask > 0
    return 100.0 * (ink & ~covered).sum() / max(int(ink.sum()), 1)


def mesh_unreachable(mask: np.ndarray) -> int:
    """复刻 vendor _generate_mesh 的三角面构造，返回三角网格图中从顶点 0 不可达的顶点数。

    该值偏大说明网格断开，ARAP 刚度矩阵奇异，vendor 会卡在
    `while np.linalg.det(...) == 0.0` 的扰动循环里（float32 下永不收敛，实测 >300s、983% CPU）。
    纯 CPU、约 0.3~0.7s，比渲染超时（120s）便宜两个数量级。

    注：这个值**没有可用的绝对阈值**——实测与死锁并非单调对应（s17 的 vendor 原始 mask
    有 10 个第二分量顶点却渲染正常，s07 扩框后 25 个就死锁）。所以 _mesh_ok 用它做
    **相对基线**比较，而不是卡绝对值。
    """
    img_dim = max(mask.shape)
    rotated = np.rot90(mask, 3)                       # 与 vendor _load_mask 一致：先转正再补成正方形
    square = np.zeros([img_dim, img_dim], rotated.dtype)
    square[0:rotated.shape[0], 0:rotated.shape[1]] = rotated

    contours = measure.find_contours(square, 128)
    if not contours:
        return 0
    contours.sort(key=len, reverse=True)              # vendor 只用最长的一条
    outline = geometry.Polygon(contours[0])
    grid = np.linspace(0, img_dim, 40)                # vendor 的固定 40x40 内部顶点
    xv, yv = np.meshgrid(grid, grid)
    inside = [(x, y) for x, y in zip(xv.flatten(), yv.flatten())
              if outline.contains(geometry.Point(x, y))]
    if not inside:
        return 0
    vertices = np.concatenate([measure.approximate_polygon(contours[0], tolerance=0.25),
                              np.array(inside)]).astype(np.float32)
    triangles = [t for t in Delaunay(vertices).simplices
                 if outline.contains(geometry.Point(*np.mean(
                     [vertices[t[0]], vertices[t[1]], vertices[t[2]]], 0)))]
    if not triangles:
        return 0

    count = int(max(max(t) for t in triangles)) + 1
    adjacency = {i: set() for i in range(count)}
    for a, b, c in triangles:
        adjacency[a] |= {b, c}
        adjacency[b] |= {a, c}
        adjacency[c] |= {a, b}
    seen, stack = {0}, [0]
    while stack:
        for node in adjacency[stack.pop()]:
            if node not in seen:
                seen.add(node)
                stack.append(node)
    return count - len(seen)


def _pad_box(box: Dict, height: int, width: int) -> Tuple[int, int, int, int]:
    """按检测框自身尺寸外扩 PAD_RATIO，不依赖墨迹检测（对纹理背景安全）。"""
    dy = int((box['bottom'] - box['top']) * PAD_RATIO)
    dx = int((box['right'] - box['left']) * PAD_RATIO)
    return (max(0, box['top'] - dy), min(height, box['bottom'] + dy),
            max(0, box['left'] - dx), min(width, box['right'] + dx))


def _ink_box(image: np.ndarray, box: Dict) -> Dict:
    """墨迹真实包围盒与检测框的并集（只会变大，不会变小）。

    治 D0 的极端情形：pose 框只框住画的一部分。s07（横躺的猪）只被框住右半边，
    框外还有 41.7% 的墨迹，而 _pad_box 按框宽外扩 15% 只能推十几像素、差 240px。
    实测 20 份样本按墨迹重裁后全图丢失率：s07 41.7→0.5%、s14 61.8→4.9%、s05 22.8→0.6%。

    对纹理/彩色背景不安全（garlic 上 _ink 大量误判背景，框会扩到整图、segment 抓错区域），
    但**不需要事先判别输入类型**：丢失率不降则不入候选，网格连通性变差则被 _mesh_ok 回退
    ——实测 s01 garlic 的不可达顶点 1→2246，正是那道现成护栏拦下的。
    """
    ys, xs = np.nonzero(_ink(image))
    if not len(ys):
        return dict(box)
    return {'top': min(box['top'], int(ys.min())), 'bottom': max(box['bottom'], int(ys.max()) + 1),
            'left': min(box['left'], int(xs.min())), 'right': max(box['right'], int(xs.max()) + 1)}


def _candidate(image: np.ndarray, box: Dict, skeleton: Sequence[Dict],
               ref_box: Optional[Dict] = None) -> Dict:
    """构造一个标注候选：裁切 → 分割 → 修 mask，返回 texture/mask/skeleton/指标。

    skeleton 的坐标相对 ref_box（vendor 输出的 char_cfg 就是相对它那个检测框的）；
    换框时必须整体平移，否则关节全体错位，错位量等于两框左上角之差：15% 扩框只偏框宽的
    15%（勉强目视可过，所以长期没暴露），按墨迹重裁能偏几百像素——实测 s07 偏 239px，
    ARAP 拿错位的 pin 驱动网格，把水平的猪拧歪压扁 31%。ref_box 省略即视为不换框。
    """
    from image_to_annotations import segment   # vendor examples，sys.path 由 annotations 模块注入

    ref_box = ref_box if ref_box is not None else box
    top, bottom, left, right = box['top'], box['bottom'], box['left'], box['right']
    texture = np.ascontiguousarray(image[top:bottom, left:right])
    dy, dx = ref_box['top'] - top, ref_box['left'] - left
    shifted = [{**j, 'loc': [j['loc'][0] + dx, j['loc'][1] + dy]} for j in skeleton]
    mask = rebuild_mask(texture, np.ascontiguousarray(segment(texture.copy())))
    offset, joint = worst_joint_offset(mask, shifted)
    return {'texture': texture, 'mask': mask, 'skeleton': shifted, 'box': box,
            'loss': loss_in_image_frame(image, box, mask), 'offset': offset, 'joint': joint}


def _disk_candidate(anno_dir: Path, skeleton: Sequence[Dict], repair: bool) -> Dict:
    """以磁盘上 vendor 已产出的 texture/mask 构造候选；repair=False 即完全不修的原始标注。

    注：这里 loss 用 crop 内口径（stroke_loss），与 _candidate 的全图口径不同；
    仅用于诊断输出，不参与扩框与否的比较（那个比较两边都来自 _candidate）。
    """
    texture = cv2.imread(str(anno_dir / 'texture.png'), cv2.IMREAD_COLOR)
    if texture is None:
        raise NeedsCorrection('NO_CONTOUR', f'texture missing: {anno_dir}')
    mask = np.array(Image.open(anno_dir / 'mask.png'))
    if repair:
        mask = rebuild_mask(texture, mask)
    offset, joint = worst_joint_offset(mask, skeleton)
    return {'texture': texture, 'mask': mask, 'skeleton': list(skeleton),
            'loss': stroke_loss(texture, mask), 'offset': offset, 'joint': joint}


def _mesh_ok(candidate: Dict, floor: int) -> bool:
    """网格连通性不得差于 vendor 原始标注这个已知安全基线。

    用相对判据而非绝对阈值：实测「不可达顶点数」与死锁并非单调对应
    （s17 的 vendor 原始 mask 有 10 个第二分量顶点却渲染正常，s07 扩框后 25 个就死锁），
    定绝对阈值会在 10~25 之间靠猜。相对基线则无需常数：修复要么不引入新风险，要么不做。
    """
    return mesh_unreachable(candidate['mask']) <= floor


def repair_or_reject(anno_dir) -> Dict:
    """就地修复 anno 目录下的 texture.png / mask.png / char_cfg.yaml。

    顺序是硬约束：先用【吸附前】的关节度量门禁，再吸附写回——反了门禁会永久失效。
    门禁触发时也已写回吸附结果，让骨架确认页拿到落在身上的关节而不是空白处的原始猜测。

    返回诊断信息 {'padded': bool, 'loss': float, 'offset': float, 'joint': str, 'moved': int}。
    """
    anno_dir = Path(anno_dir)
    cfg_path = anno_dir / 'char_cfg.yaml'
    cfg = yaml.safe_load(cfg_path.read_text())
    skeleton = cfg['skeleton']
    box_path = anno_dir / 'bounding_box.yaml'
    image_path = anno_dir / 'image.png'

    plain_box = yaml.safe_load(box_path.read_text()) if box_path.exists() else None
    image = cv2.imread(str(image_path)) if image_path.exists() else None
    if image is not None:
        image = _vendor_resized(image)

    # vendor 原始标注 = 已知安全基线（20 份尖刺样本用它均渲染正常）。
    # 每一步修复都不得让网格连通性比它更差，否则宁可少修 —— 网格断开会让
    # vendor arap.py 卡在 `while np.linalg.det(...) == 0.0` 扰动循环（实测 s07 >300s、983% CPU）。
    baseline = _disk_candidate(anno_dir, skeleton, repair=False)

    # 候选按偏好排序：墨迹框 > 按比例扩框 > 原框 > 基线
    options = []
    if image is not None and plain_box is not None:
        # 干净且已连通的 vendor 标注不需要重跑 segment、扩框候选和 mesh 检查。
        # SKIP_RATIO 与 rebuild_mask 的门槛一致，避免改变既有修复判定。
        missing = _ink(baseline['texture']) & ~(baseline['mask'] > 0)
        connected = ndimage.label(baseline['mask'] > 0)[1] <= 1
        if missing.sum() < SKIP_RATIO * baseline['mask'].size and connected:
            options.append((baseline, 'plain'))
        else:
            height, width = image.shape[:2]
            # plain_box 就是 vendor 已经产出的检测框；复用其 texture/mask，避免
            # 为普通候选再次调用 vendor segment()，只对真正扩框的候选重新分割。
            if baseline['texture'].shape[:2] == (
                    plain_box['bottom'] - plain_box['top'],
                    plain_box['right'] - plain_box['left']):
                plain_mask = rebuild_mask(baseline['texture'], baseline['mask'])
                plain_offset, plain_joint = worst_joint_offset(plain_mask, skeleton)
                plain = {'texture': baseline['texture'], 'mask': plain_mask,
                         'skeleton': list(skeleton), 'box': plain_box,
                         'loss': loss_in_image_frame(image, plain_box, plain_mask),
                         'offset': plain_offset, 'joint': plain_joint}
            else:
                # repair_or_reject 可能被重复调用，此时磁盘产物已使用扩框尺寸，
                # bounding_box.yaml 仍是旧框，不能把两套坐标强行当作同一候选。
                plain = _candidate(image, plain_box, skeleton)
            wider = [('ink', _ink_box(image, plain_box)),
                     ('pad', dict(zip(('top', 'bottom', 'left', 'right'),
                                      _pad_box(plain_box, height, width))))]
            seen = [plain_box]
            for tag, box in wider:
                if box in seen:
                    continue
                seen.append(box)
                cand = _candidate(image, box, skeleton, ref_box=plain_box)
                # 扩框只在能救回更多笔画时才入候选：彩色/纹理背景（garlic）扩框后
                # segment() 会抓错区域、丢失率反而上升。不比关节偏移：它由 F1 吸附归零、
                # 并由门禁兼顶；加进护栏会误杀真正需要扩框的样本（s09：29.5%→6.2%）。
                if cand['loss'] < plain['loss']:
                    options.append((cand, tag))
            options.append((plain, 'plain'))
    else:
        # 缺 image.png / bounding_box.yaml（旧产物或人工构造）时退回只修 mask
        repaired = _disk_candidate(anno_dir, skeleton, repair=True)
        options.append((repaired, 'plain'))

    # 在候选里挑全图丢失率最低的，而不是按固定优先级取第一个：实测少数样本
    # （s06 1.7% vs 墨迹框 2.4%、s17 2.4% vs 2.6%）按比例扩框反而更干净。
    # next 仍是惰性的，所以通常只对排在最前的那个候选算一次 mesh_unreachable。
    options.sort(key=lambda o: o[0]['loss'])
    if len(options) == 1 and options[0][0] is baseline:
        chosen, crop = options[0]
    else:
        floor = mesh_unreachable(baseline['mask'])
        chosen, crop = next(((c, t) for c, t in options if _mesh_ok(c, floor)),
                            (baseline, 'baseline'))

    if not (chosen['mask'] > 0).any():
        raise NeedsCorrection('NO_CONTOUR', 'mask empty after repair')

    offset, joint = chosen['offset'], chosen['joint']
    snapped, moved = project_joints_to_axis(chosen['skeleton'], chosen['mask'])
    # 外推必须在吸附之后：吸附保证末端关节已落在 mask 内，才有可沿着走的起点。
    # 放在门禁判定之前无害——门禁用的 offset 早在候选阶段（吸附前）算好。
    snapped, extended = extend_limb_tips(snapped, chosen['mask'])

    # 先写回（含吸附后的骨架），再判门禁：确认页需要 mask 与 joints 互相一致
    height, width = chosen['mask'].shape[:2]
    cv2.imwrite(str(anno_dir / 'texture.png'),
                cv2.cvtColor(chosen['texture'], cv2.COLOR_BGR2BGRA))   # vendor 断言 texture 必须 RGBA
    Image.fromarray(chosen['mask']).save(anno_dir / 'mask.png')
    cfg.update({'skeleton': snapped, 'height': height, 'width': width})
    cfg_path.write_text(yaml.safe_dump(cfg))
    (anno_dir / 'joint_overlay.png').unlink(missing_ok=True)   # 已与新标注不符，留着会误导

    if offset > MAX_JOINT_OFFSET:
        raise NeedsCorrection(
            'SKELETON_MISFIT',
            f'joint {joint} is {offset:.1%} of diagonal away from the character mask')
    return {'padded': crop in ('ink', 'pad'), 'crop': crop, 'loss': chosen['loss'],
            'offset': offset, 'joint': joint, 'moved': moved, 'extended': extended}
