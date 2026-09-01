# 涂鸦角色动画管线（方案 A）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把一张类人形涂鸦 PNG 变成 run / jump 两套透明 PNG 精灵表 + 元数据，达到 P0 尖刺验收线（20 样本 ≥ 16 成功，单张 ≤ 60 秒）。

**架构：** AnimatedDrawings 自部署。TorchServe（Docker）负责涂鸦分析（检测/分割/骨架），AnimatedDrawings 渲染器用 BVH 动捕重定向生成透明 GIF，自写的精灵表后处理器把 GIF 拆帧拼成横向透明 PNG 精灵表。组件间以文件 + JSON 交接。设计见 `docs/superpowers/specs/2026-09-01-doodle-animation-pipeline-design.md`。

**技术栈：** Python 3.8、AnimatedDrawings（vendor 源码）、TorchServe/Docker、OpenCV、Pillow、PyYAML、pytest、CMU 公开动捕库 BVH 资产。

## 关键接口事实（已对源码核实，勿再猜测）

- `examples/image_to_annotations.py::image_to_annotations(img_fn, out_dir)`：POST `http://localhost:8080/predictions/drawn_humanoid_detector` 与 `drawn_humanoid_pose_estimator`；产出 `mask.png`、`texture.png`、`char_cfg.yaml`（含 16 关节 `skeleton`、`height`、`width`）、`bounding_box.yaml`、`joint_overlay.png`。**失败路径是 `assert False`/`raise Exception`（无人形、多骨架、无轮廓）**，包装层必须捕获并转 `needs_correction`。
- 渲染入口：`from animated_drawings import render; render.start(cfg_yaml)`，`controller.MODE: video_render`，`OUTPUT_VIDEO_PATH` 以 `.gif` 结尾即输出透明 GIF（RGBA 帧，alpha 保留）。
- 场景配置结构：`scene.ANIMATED_CHARACTERS[0]` 含 `character_cfg` / `motion_cfg` / `retarget_cfg`；无头渲染加 `view: { USE_MESA: True }`。
- motion 配置字段（参考 `examples/config/motion/dab.yaml`）：`filepath`（BVH 路径）、`start_frame_idx`、`end_frame_idx`、`groundplane_joint`、`forward_perp_joint_vectors`、`scale`、`up`。
- 默认 retarget 配置：`examples/config/retarget/fair1_ppf.yaml`（与仓库自带 `examples/bvh/fair1/*.bvh` 配套）。

## 文件结构

```text
server/
  docker-compose.yml                      # 已有：TorchServe 管理
  scripts/
    setup-vendor.sh                       # 已有：克隆 AnimatedDrawings
    setup_env.sh                          # 创建：venv + pip install -e vendor
    download_samples.py                   # 创建：拉取官方示例涂鸦做测试素材
  requirements-dev.txt                    # 创建：pytest、opencv-python、pillow、pyyaml、requests
  app/
    assets/motions/
      run.bvh  run.yaml                   # 创建：跑动动作资产与 motion 配置
      jump.bvh  jump.yaml                 # 创建：跳跃动作资产与 motion 配置
    services/
      annotations.py                      # 创建：包装 image_to_annotations，异常转 needs_correction
      sprite_sheet.py                     # 创建：GIF 拆帧 → 透明 PNG 精灵表 + 元数据
      character_pipeline.py               # 创建：render() 门面，编排 ①→②→③
      render_scene.py                     # 创建：生成渲染场景 YAML 并调用 render.start
  tests/
    test_sprite_sheet.py                  # 创建：合成帧测试（不需要 Docker）
    test_annotations.py                   # 创建：失败路径分类测试
    test_character_pipeline_contract.py   # 创建：契约测试
  out/                                    # 尖刺产物，不入库
docs/animation-spike-results.md           # 创建：20 样本验收记录
```

---

### 任务 1：本地 Python 环境与 TorchServe 环境验证

**交付物：** `setup_env.sh` 一键装好宿主侧环境；官方示例涂鸦在本地跑出透明 `video.gif`。

**文件：**
- 创建：`server/requirements-dev.txt`
- 创建：`server/scripts/setup_env.sh`

- [ ] **步骤 1：编写 `server/requirements-dev.txt`**

```text
pytest>=7.0
opencv-python>=4.5
numpy>=1.21,<1.24
pillow>=9.0
pyyaml>=6.0
requests>=2.27
scikit-image>=0.19
scipy>=1.7
```

- [ ] **步骤 2：编写 `server/scripts/setup_env.sh`**

```bash
#!/usr/bin/env bash
# 创建宿主侧 Python 环境并以可编辑模式安装 vendor 的 AnimatedDrawings。
set -euo pipefail
SERVER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR_DIR="${SERVER_DIR}/vendor/AnimatedDrawings"

[ -d "${VENDOR_DIR}" ] || { echo "先运行 scripts/setup-vendor.sh" >&2; exit 1; }

python3 -m venv "${SERVER_DIR}/.venv"
source "${SERVER_DIR}/.venv/bin/activate"
pip install --upgrade pip
pip install -e "${VENDOR_DIR}"
pip install -r "${SERVER_DIR}/requirements-dev.txt"
echo "完成。激活环境：source ${SERVER_DIR}/.venv/bin/activate"
```

- [ ] **步骤 3：执行环境搭建**

运行：
```bash
cd server
bash scripts/setup-vendor.sh
bash scripts/setup_env.sh
docker compose up -d --build     # 首次 5-7 分钟
```

- [ ] **步骤 4：验证 TorchServe 健康**

运行：`curl http://localhost:8080/ping`
预期：`{"status": "Healthy"}`（首次启动需等 1-2 分钟模型加载）

- [ ] **步骤 5：端到端验证官方示例**

运行：
```bash
source .venv/bin/activate
cd vendor/AnimatedDrawings/examples
python image_to_animation.py drawings/garlic.png garlic_out
```
预期：`garlic_out/video.gif` 生成，为透明背景动画。若报 `USE_MESA`/OpenGL 错误，在 `render.start` 使用的 config 中确认 `view: { USE_MESA: True }`（macOS 本地窗口模式通常无需此项）。

- [ ] **步骤 6：Commit**

```bash
git add server/requirements-dev.txt server/scripts/setup_env.sh
git commit -m "feat: add spike python environment setup"
```

### 任务 2：精灵表后处理器（sprite_sheet.py，TDD）

**交付物：** 透明 GIF → 横向透明 PNG 精灵表 + 元数据，全部测试通过。不依赖 Docker。

**文件：**
- 创建：`server/app/services/sprite_sheet.py`
- 测试：`server/tests/test_sprite_sheet.py`

- [ ] **步骤 1：编写失败测试 `server/tests/test_sprite_sheet.py`**

```python
from pathlib import Path
from PIL import Image
import pytest

from app.services.sprite_sheet import build_sprite_sheet


@pytest.fixture
def three_frame_gif(tmp_path):
    """3 帧 RGBA GIF：每帧左上角有一个 40x40 的不透明方块。"""
    frames = []
    for i in range(3):
        img = Image.new('RGBA', (100, 120), (0, 0, 0, 0))
        for x in range(40):
            for y in range(40):
                img.putpixel((x + i * 5, y), (255, 0, 0, 255))  # 每帧方块横移 5px
        frames.append(img)
    p = tmp_path / 'anim.gif'
    frames[0].save(p, save_all=True, append_images=frames[1:], duration=83, disposal=2, loop=0)
    return p


def test_build_sprite_sheet_returns_transparent_png_and_metadata(three_frame_gif, tmp_path):
    out_png = tmp_path / 'run.png'
    meta = build_sprite_sheet(gif_path=three_frame_gif, out_path=out_png, fps=12)

    assert meta['frameCount'] == 3
    assert meta['fps'] == 12
    assert meta['frameWidth'] > 0 and meta['frameHeight'] > 0

    sheet = Image.open(out_png)
    assert sheet.mode == 'RGBA'
    assert sheet.size == (meta['frameWidth'] * 3, meta['frameHeight'])


def test_foot_anchor_is_bottom_of_content(three_frame_gif, tmp_path):
    out_png = tmp_path / 'run.png'
    meta = build_sprite_sheet(gif_path=three_frame_gif, out_path=out_png, fps=12)
    # 方块底边在 y=39，脚底锚点应指向内容底部区域而非画布外
    assert 30 <= meta['footAnchor']['y'] <= meta['frameHeight']
    assert 0 < meta['footAnchor']['x'] < meta['frameWidth']


def test_single_frame_gif(tmp_path):
    img = Image.new('RGBA', (50, 60), (0, 0, 0, 0))
    img.putpixel((10, 10), (0, 0, 255, 255))
    p = tmp_path / 'one.gif'
    img.save(p)
    meta = build_sprite_sheet(gif_path=p, out_path=tmp_path / 'o.png', fps=12)
    assert meta['frameCount'] == 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd server && source .venv/bin/activate && python -m pytest tests/test_sprite_sheet.py -v`
预期：FAIL（`ModuleNotFoundError: No module named 'app.services.sprite_sheet'`）

- [ ] **步骤 3：创建 `server/app/__init__.py`、`server/app/services/__init__.py`（空文件）与 `server/app/services/sprite_sheet.py`**

```python
"""透明 GIF -> 横向透明 PNG 精灵表。帧尺寸取所有帧内容包围盒的并集，逐帧居中粘贴。"""
from pathlib import Path
from typing import Dict

from PIL import Image


def _load_frames(gif_path: Path):
    gif = Image.open(gif_path).convert('RGBA')
    frames = []
    try:
        while True:
            frames.append(gif.copy())
            gif.seek(gif.tell() + 1)
    except EOFError:
        pass
    return frames


def build_sprite_sheet(gif_path, out_path, fps: int = 12) -> Dict:
    gif_path, out_path = Path(gif_path), Path(out_path)
    frames = _load_frames(gif_path)
    if not frames:
        raise ValueError(f'GIF contains no frames: {gif_path}')

    # 所有帧内容包围盒的并集（跨帧角色位移不裁切）
    left, top, right, bottom = None, None, None, None
    for f in frames:
        bbox = f.getbbox()  # 非透明像素包围盒
        if bbox is None:
            continue
        left = bbox[0] if left is None else min(left, bbox[0])
        top = bbox[1] if top is None else min(top, bbox[1])
        right = bbox[2] if right is None else max(right, bbox[2])
        bottom = bbox[3] if bottom is None else max(bottom, bbox[3])
    if left is None:
        raise ValueError(f'GIF frames are fully transparent: {gif_path}')

    frame_w, frame_h = right - left, bottom - top
    sheet = Image.new('RGBA', (frame_w * len(frames), frame_h), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f.crop((left, top, right, bottom)), (i * frame_w, 0))
    sheet.save(out_path)

    # 脚底锚点：帧内水平居中、垂直贴内容底部
    return {
        'spriteSheetUrl': out_path.name,
        'frameCount': len(frames),
        'fps': fps,
        'frameWidth': frame_w,
        'frameHeight': frame_h,
        'footAnchor': {'x': frame_w // 2, 'y': frame_h},
    }
```

- [ ] **步骤 4：运行测试验证通过**

运行：`cd server && python -m pytest tests/test_sprite_sheet.py -v`
预期：3 个用例全部 PASS

- [ ] **步骤 5：Commit**

```bash
git add server/app server/tests/test_sprite_sheet.py
git commit -m "feat: add sprite sheet builder from transparent gif"
```

### 任务 3：涂鸦分析包装层（annotations.py，失败转 needs_correction）

**交付物：** `analyze(img, out_dir)` 成功返回标注目录；无人形/多骨架等失败抛 `NeedsCorrection` 而非断言崩溃。

**文件：**
- 创建：`server/app/services/annotations.py`
- 测试：`server/tests/test_annotations.py`

- [ ] **步骤 1：编写失败测试 `server/tests/test_annotations.py`**

```python
import sys
from pathlib import Path

import pytest

from app.services.annotations import NeedsCorrection, analyze

VENDOR_EXAMPLES = Path(__file__).parent.parent / 'vendor' / 'AnimatedDrawings' / 'examples'


@pytest.fixture(autouse=True)
def _vendor_on_path():
    sys.path.insert(0, str(VENDOR_EXAMPLES))
    yield
    sys.path.remove(str(VENDOR_EXAMPLES))


def test_analyze_missing_image_raises_needs_correction(tmp_path):
    with pytest.raises(NeedsCorrection) as e:
        analyze(tmp_path / 'not_exist.png', tmp_path / 'out')
    assert e.value.reason == 'NO_HUMANOID'


def test_analyze_valid_doodle_outputs_annotations(tmp_path):
    """需要 TorchServe 已启动（任务 1 步骤 4 通过）；否则跳过。"""
    import requests
    try:
        requests.get('http://localhost:8080/ping', timeout=2)
    except Exception:
        pytest.skip('TorchServe not running')

    img = VENDOR_EXAMPLES / 'drawings' / 'char2.png'
    out_dir = tmp_path / 'anno'
    result = analyze(img, out_dir)
    assert (out_dir / 'mask.png').exists()
    assert (out_dir / 'texture.png').exists()
    assert (out_dir / 'char_cfg.yaml').exists()
    assert result['skeleton_len'] == 16
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_annotations.py -v`
预期：FAIL（`No module named 'app.services.annotations'`）

- [ ] **步骤 3：实现 `server/app/services/annotations.py`**

```python
"""包装 vendor 的 image_to_annotations：把 assert/Exception 失败路径收敛为 NeedsCorrection。"""
import logging
import sys
from pathlib import Path
from typing import Dict

import yaml

REASONS = {
    'Could not detect any drawn humanoids': 'NO_HUMANOID',
    'Could not detect any skeletons': 'NO_SKELETON',
    'skeletons within the character bounding box': 'MULTIPLE_SKELETONS',
    'Found no contours': 'NO_CONTOUR',
}


class NeedsCorrection(Exception):
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f'{reason}: {detail}')


def _classify(msg: str) -> str:
    for k, v in REASONS.items():
        if k in msg:
            return v
    return 'ANALYZE_FAILED'


def analyze(img_path, out_dir) -> Dict:
    img_path, out_dir = Path(img_path), Path(out_dir)
    if not img_path.exists():
        raise NeedsCorrection('NO_HUMANOID', f'input image not found: {img_path}')

    # vendor 的 examples 目录不在 pip 包内，调用前确保在 sys.path
    from image_to_annotations import image_to_annotations  # noqa: E402

    try:
        image_to_annotations(str(img_path), str(out_dir))
    except (AssertionError, Exception) as e:  # vendor 用 assert False 报错
        raise NeedsCorrection(_classify(str(e)), str(e)) from e

    char_cfg = yaml.safe_load((out_dir / 'char_cfg.yaml').read_text())
    return {'skeleton_len': len(char_cfg['skeleton']),
            'height': char_cfg['height'], 'width': char_cfg['width']}
```

> 注意：vendor 源码中多骨架的报错文案是 `Detected {n} skeletons within the character bounding box`，`_classify` 的匹配键需对照实际报错文案微调（跑步骤 4 时按真实输出修正 `REASONS`）。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/test_annotations.py -v`
预期：`test_analyze_missing_image` PASS；TorchServe 在线时第 2 个用例 PASS，否则 SKIPPED。若断言文案与 `REASONS` 不符，按真实报错修正后重跑。

- [ ] **步骤 5：Commit**

```bash
git add server/app/services/annotations.py server/tests/test_annotations.py
git commit -m "feat: wrap doodle analysis with needs_correction classification"
```

### 任务 4：场景渲染配置生成（render_scene.py）+ 官方动作首跑

**交付物：** 给定标注目录与动作名，生成场景 YAML 并渲染出透明 GIF。先用官方 `dab.yaml` 打通链路。

**文件：**
- 创建：`server/app/services/render_scene.py`

- [ ] **步骤 1：实现 `server/app/services/render_scene.py`**

```python
"""生成 AnimatedDrawings 渲染场景 YAML 并执行渲染，输出透明 GIF。"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]          # 仓库根
VENDOR = REPO_ROOT / 'server' / 'vendor' / 'AnimatedDrawings'
RETARGET_CFG = str(VENDOR / 'examples' / 'config' / 'retarget' / 'fair1_ppf.yaml')


def render_animation(char_anno_dir, motion_cfg_fn, out_gif, use_mesa=False) -> Path:
    char_anno_dir, out_gif = Path(char_anno_dir), Path(out_gif)
    out_gif.parent.mkdir(parents=True, exist_ok=True)

    cfg = {
        'scene': {'ANIMATED_CHARACTERS': [{
            'character_cfg': str(char_anno_dir / 'char_cfg.yaml'),
            'motion_cfg': str(motion_cfg_fn),
            'retarget_cfg': RETARGET_CFG,
        }]},
        'controller': {'MODE': 'video_render', 'OUTPUT_VIDEO_PATH': str(out_gif)},
    }
    if use_mesa:
        cfg['view'] = {'USE_MESA': True}

    # character_cfg 内的相对路径以 vendor 仓库根为基准，渲染需在该目录下执行
    scene_yaml = out_gif.with_suffix('.scene.yaml')
    scene_yaml.write_text(yaml.safe_dump(cfg))

    import os
    from animated_drawings import render
    cwd = os.getcwd()
    os.chdir(VENDOR)
    try:
        render.start(str(scene_yaml))
    finally:
        os.chdir(cwd)
    if not out_gif.exists():
        raise RuntimeError(f'render finished but gif missing: {out_gif}')
    return out_gif
```

> `char_cfg.yaml` 由任务 3 的 `analyze()` 以绝对路径写入（`out_dir` 传绝对路径即可），不受 `os.chdir` 影响。

- [ ] **步骤 2：手动首跑官方动作验证链路（不写单测，属集成验证）**

运行：
```bash
source .venv/bin/activate
python - <<'EOF'
from app.services.annotations import analyze
from app.services.render_scene import render_animation, VENDOR
out = '/tmp/spike/char1'
analyze(str(VENDOR / 'examples/drawings/char2.png'), out)
gif = render_animation(out, VENDOR / 'examples/config/motion/dab.yaml', '/tmp/spike/dab.gif')
print('OK:', gif)
EOF
```
预期：`/tmp/spike/dab.gif` 生成，打开为透明背景角色跳舞。

- [ ] **步骤 3：接精灵表后处理器，验证完整 ①→②→③**

运行：
```bash
python - <<'EOF'
from app.services.sprite_sheet import build_sprite_sheet
print(build_sprite_sheet('/tmp/spike/dab.gif', '/tmp/spike/dab_sheet.png', fps=12))
EOF
```
预期：打印 `frameCount`/`frameWidth`/`footAnchor` 等元数据，`/tmp/spike/dab_sheet.png` 为横向透明精灵表。

- [ ] **步骤 4：Commit**

```bash
git add server/app/services/render_scene.py
git commit -m "feat: add scene config generator for animated drawings render"
```

### 任务 5：run / jump 动作资产与 motion 配置

**交付物：** `server/app/assets/motions/` 下 run、jump 各一套（BVH + motion YAML），观感由人工评审确认。

**文件：**
- 创建：`server/app/assets/motions/run.bvh`、`run.yaml`
- 创建：`server/app/assets/motions/jump.bvh`、`jump.yaml`
- 创建：`server/app/assets/motions/README.md`

- [ ] **步骤 1：获取动捕数据（已定：CMU 公开动捕库，免登录）**

从 CMU Motion Capture Database 的公开 BVH 转换版本下载（如 cgspeed.com 的 BVH 转换包或 GitHub 上的 CMU BVH 镜像），挑选：
- `Running`：优先选**双腿交叉幅度小**的跑动循环；
- `Jump`：选原地起跳到落地的单段动作。
转换/保存为 `run.bvh` / `jump.bvh`，并在 `server/app/assets/motions/README.md` 记录每个文件的下载来源 URL 与原始动作编号（可追溯）。

- [ ] **步骤 2：用 Blender 查看两个 BVH 的帧数与骨骼命名，编写 `run.yaml`（`jump.yaml` 同构，仅帧区间不同）**

```yaml
filepath: /绝对路径/到/仓库根/server/app/assets/motions/run.bvh   # 渲染时会 chdir 到 vendor 目录，必须用绝对路径，由执行者按本机路径填写
start_frame_idx: 0
end_frame_idx: 24          # 按实际循环段截取：run 取一个完整循环约 0.8-1.0s
groundplane_joint: mixamorig:LeftFoot      # 按 BVH 实际骨骼名填写
forward_perp_joint_vectors:
  - - mixamorig:LeftShoulder
    - mixamorig:RightShoulder
  - - mixamorig:LeftUpLeg
    - mixamorig:RightUpLeg
scale: 0.025               # 与 dab.yaml 同量级起步，按渲染大小微调
up: +z                     # Blender 导出 BVH 通常是 +z，若角色躺倒则改 +y
```

- [ ] **步骤 3：逐个动作渲染评审**

运行（复用任务 4 的脚本模式，将 `motion_cfg` 换为 `run.yaml`、`jump.yaml`）：
```bash
python - <<'EOF'
from app.services.render_scene import render_animation
out = '/tmp/spike/char1'
render_animation(out, 'app/assets/motions/run.yaml', '/tmp/spike/run.gif')
render_animation(out, 'app/assets/motions/jump.yaml', '/tmp/spike/jump.gif')
EOF
```
预期与调参规则：
- 角色贴地滑动 → 调 `groundplane_joint`（换到脚部关节）；
- 角色大小离谱 → 调 `scale`（0.01-0.1 间二分）；
- 角色朝向/翻滚错误 → 切 `up` 或调整 `forward_perp_joint_vectors`；
- run 双腿挤成一团（已知正面纹理局限）→ 换更舒展的跑动资产，接受风格化观感并在尖刺记录中标注。

- [ ] **步骤 4：跑通帧数确认**

`run` 产出帧数约 10-14（循环），`jump` 约 8-12（起跳到落地）。超出则回到步骤 2 收紧 `start_frame_idx/end_frame_idx`。

- [ ] **步骤 5：Commit**

```bash
git add server/app/assets
git commit -m "feat: add run and jump motion assets"
```

### 任务 6：CharacterPipeline 门面与契约测试

**交付物：** `CharacterPipeline.render(input_path, motion)` 单一入口，`ready` 与 `needs_correction` 两条路径均有契约测试。

**文件：**
- 创建：`server/app/services/character_pipeline.py`
- 测试：`server/tests/test_character_pipeline_contract.py`

- [ ] **步骤 1：编写失败测试 `server/tests/test_character_pipeline_contract.py`**

```python
from pathlib import Path

import pytest

from app.services.character_pipeline import CharacterPipeline
from app.services.annotations import NeedsCorrection

VENDOR_EXAMPLES = Path(__file__).parent.parent / 'vendor' / 'AnimatedDrawings' / 'examples'


@pytest.fixture
def pipeline(tmp_path):
    return CharacterPipeline(
        assets_dir=Path(__file__).parent.parent / 'app' / 'assets' / 'motions',
        out_root=tmp_path,
    )


def test_render_invalid_input_returns_needs_correction(pipeline, tmp_path):
    with pytest.raises(NeedsCorrection):
        pipeline.render(tmp_path / 'missing.png', motion='run')


def test_render_ready_contains_contract_fields(pipeline):
    """需要 TorchServe 已启动；否则跳过。"""
    import requests
    try:
        requests.get('http://localhost:8080/ping', timeout=2)
    except Exception:
        pytest.skip('TorchServe not running')

    img = VENDOR_EXAMPLES / 'drawings' / 'char2.png'
    result = pipeline.render(img, motion='run')
    assert result['status'] == 'ready'
    anim = result['animations']['run']
    for key in ('spriteSheetUrl', 'frameCount', 'fps', 'frameWidth', 'frameHeight', 'footAnchor'):
        assert key in anim
    assert anim['fps'] == 12
    assert Path(result['animations']['run']['spriteSheetUrl']).exists()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_character_pipeline_contract.py -v`
预期：FAIL（`No module named 'app.services.character_pipeline'`）

- [ ] **步骤 3：实现 `server/app/services/character_pipeline.py`**

```python
"""角色动画管线门面：PNG -> 透明 PNG 精灵表 + 元数据。"""
from pathlib import Path
from typing import Dict

from app.services.annotations import analyze
from app.services.render_scene import render_animation
from app.services.sprite_sheet import build_sprite_sheet

FPS = 12


class CharacterPipeline:
    def __init__(self, assets_dir, out_root):
        self.assets_dir = Path(assets_dir)
        self.out_root = Path(out_root)

    def render(self, input_path, motion: str) -> Dict:
        """motion in {'run', 'jump'}。失败时抛 NeedsCorrection（由 API 层转 needs_correction）。"""
        motion_bvh = self.assets_dir / f'{motion}.bvh'
        motion_cfg = self.assets_dir / f'{motion}.yaml'
        if not motion_cfg.exists() or not motion_bvh.exists():
            raise FileNotFoundError(f'motion assets missing: {motion}')

        out_dir = self.out_root / Path(input_path).stem / motion
        out_dir.mkdir(parents=True, exist_ok=True)

        analyze(input_path, out_dir / 'anno')
        gif = render_animation(out_dir / 'anno', motion_cfg, out_dir / f'{motion}.gif')
        meta = build_sprite_sheet(gif, out_dir / f'{motion}.png', fps=FPS)

        return {
            'status': 'ready',
            'animations': {motion: {**meta, 'spriteSheetUrl': str(out_dir / f'{motion}.png')}},
        }
```

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/test_character_pipeline_contract.py -v`
预期：`test_render_invalid_input` PASS；TorchServe 在线时第 2 个用例 PASS，否则 SKIPPED。

- [ ] **步骤 5：run/jump 双动作串跑一次**

运行：
```bash
python - <<'EOF'
from pathlib import Path
from app.services.character_pipeline import CharacterPipeline
from app.services.render_scene import VENDOR
p = CharacterPipeline('app/assets/motions', 'out/spike')
img = VENDOR / 'examples/drawings/char2.png'
print(p.render(img, 'run')['animations']['run'])
print(p.render(img, 'jump')['animations']['jump'])
EOF
```
预期：`out/spike/char2/run/run.png` 与 `out/spike/char2/jump/jump.png` 生成，元数据打印正常。

- [ ] **步骤 6：Commit**

```bash
git add server/app/services/character_pipeline.py server/tests/test_character_pipeline_contract.py
git commit -m "feat: add character pipeline facade with contract tests"
```

### 任务 7：20 样本验收与尖刺结论

**交付物：** `docs/animation-spike-results.md`：逐样本记录 + 16/20、60 秒两项门槛结论 + 降级决策。

**文件：**
- 创建：`server/scripts/download_samples.py`
- 创建：`server/tests/test_spike_batch.py`
- 创建：`docs/animation-spike-results.md`

- [ ] **步骤 1：编写 `server/scripts/download_samples.py` 拉取官方示例作基础样本**

```python
"""下载 AnimatedDrawings 官方示例涂鸦到 testdata/characters/，作为验收样本的起点。
真实验收需补充儿童实画样本至 20 张（授权后放入同目录，命名 s01.png-s20.png）。"""
import urllib.request
from pathlib import Path

BASE = 'https://raw.githubusercontent.com/facebookresearch/AnimatedDrawings/main/examples/drawings/'
NAMES = ['char1.png', 'char2.png', 'char3.png', 'char4.png', 'garlic.png', 'squid.png']

out = Path(__file__).resolve().parents[2] / 'testdata' / 'characters'
out.mkdir(parents=True, exist_ok=True)
for n in NAMES:
    urllib.request.urlretrieve(BASE + n, out / n)
    print('downloaded', out / n)
```

- [ ] **步骤 2：编写批量验收测试 `server/tests/test_spike_batch.py`**

```python
import time
from pathlib import Path

import pytest

from app.services.character_pipeline import CharacterPipeline
from app.services.annotations import NeedsCorrection

SAMPLES = sorted((Path(__file__).parent.parent.parent / 'testdata' / 'characters').glob('*.png'))
TIMEOUT_SEC = 60


@pytest.mark.skipif(not SAMPLES, reason='samples not downloaded')
def test_spike_batch_meets_acceptance_bar():
    pipeline = CharacterPipeline(
        Path(__file__).parent.parent / 'app' / 'assets' / 'motions',
        Path(__file__).parent.parent / 'out' / 'spike',
    )
    results = []
    for img in SAMPLES:
        t0 = time.time()
        try:
            pipeline.render(img, 'run')
            pipeline.render(img, 'jump')
            results.append((img.name, 'success', round(time.time() - t0, 1)))
        except NeedsCorrection as e:
            results.append((img.name, f'needs_correction:{e.reason}', round(time.time() - t0, 1)))
        except Exception as e:
            results.append((img.name, f'failed:{type(e).__name__}', round(time.time() - t0, 1)))

    for r in results:
        print(r)
    success = [r for r in results if r[1] == 'success' and r[2] <= TIMEOUT_SEC]
    assert len(SAMPLES) >= 20, f'only {len(SAMPLES)} samples, need 20'
    assert len(success) >= 16, f'acceptance bar not met: {len(success)}/20'
```

- [ ] **步骤 3：补齐真实样本并运行验收**

运行：
```bash
python scripts/download_samples.py
# 手工补充儿童实画样本至 testdata/characters/ 共 20 张（需家长授权）
python -m pytest tests/test_spike_batch.py -v -s
```
预期：打印逐样本结果；达标则 PASS，不达标则 FAIL（预期内的失败，结论写入步骤 4）。

- [ ] **步骤 4：编写 `docs/animation-spike-results.md`**

内容固定为四节：① 逐样本结果表（文件名/结果/耗时/失败原因）；② 成功率与耗时对 16/20、60 秒门槛的结论；③ 典型失败样例与 `needs_correction` 分类分布；④ 降级决策：达标 → 进入任务 3（异步服务化）；不达标 → 启用引导画人 + 预置角色。

- [ ] **步骤 5：Commit**

```bash
git add server/scripts/download_samples.py server/tests/test_spike_batch.py docs/animation-spike-results.md testdata
git commit -m "test: record 20-sample spike acceptance results"
```

---

## 执行顺序依赖

任务 1 → 2（可离线并行）→ 3 → 4 → 5 → 6 → 7。任务 2 不依赖 Docker，可与任务 1 的镜像构建并行。

## 已知风险与对策（尖刺期间按此处置，不扩散范围）

- **正面纹理做侧向跑双腿挤压**：换双腿舒展的跑动资产；仍不理想则在尖刺记录中标注为风格化，由 20 样本评审裁决。
- **CMU BVH 骨骼命名差异**：以实际导出的关节名为准修正 `run.yaml`/`jump.yaml` 的关节名，不修改任何代码。
- **TorchServe 内存不足静默失败**：`docker compose logs torchserve` 查 `OutOfMemory`，Docker Desktop 内存调到 16GB。
