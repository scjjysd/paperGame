# Character Render Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 降低角色任务的 TorchServe 常驻内存和双动作重复初始化成本，在不改变处理结果与 API 契约的前提下，让 `man1.jpg` 稳定完成并输出阶段性能日志。

**Architecture:** Compose 通过项目启动脚本把可选 `.env` worker 数映射成 TorchServe 的 `default_workers_per_model`；留空时不写该属性。角色管线在同一 Scene/View/AnimatedDrawing 中顺序渲染多个动作，动作之间只替换 retargeter 并重置时间，静态 mesh、ARAP 和 OpenGL 资源只初始化一次。

**Tech Stack:** Python 3.9、pytest、AnimatedDrawings、NumPy/SciPy、PyOpenGL/OSMesa、Docker Compose、POSIX shell、TorchServe。

**Spec:** `docs/superpowers/specs/2026-09-18-character-render-performance-design.md`

## Global Constraints

- 不修改 `vendor/AnimatedDrawings/animated_drawings/**` Python 源码。
- 不改变 API 契约、Redis 状态机、动作配方、帧数、产物路径或 `force=true` 行为。
- `run`、`jump` 必须在一个子进程内严格顺序执行，不创建动作级线程、进程或异步任务。
- 保留 `app.services.render_scene.render_animation()` 单动作入口。
- 保持 Python 3.9 兼容，不使用 `X | Y` 类型注解。
- `.env` 已被 Git 忽略；本机写入 `TORCHSERVE_WORKERS_PER_MODEL=2`，不得提交 `.env`。
- 新日志使用现有 logging，不向 API 响应增加字段。
- 执行前使用 `superpowers:using-git-worktrees`；不得移动、删除或提交用户的 `testdata/myson/`。

## File Map

- Create `docker/torchserve-entrypoint.sh`: 校验 worker 数并生成运行时配置。
- Create `tests/test_torchserve_config.py`: 测试脚本和 Compose 映射。
- Modify `docker-compose.yml`: 注入变量并挂载启动脚本。
- Modify local `.env`: 设置 `2`，不提交。
- Modify `app/services/render_scene.py`: 批量顺序渲染、状态切换和规模日志。
- Create `tests/test_render_batch.py`: 生命周期与规模计算测试。
- Modify `app/services/character_pipeline.py`: 接入批量渲染并记录阶段耗时。
- Create `tests/test_character_pipeline_timing.py`: 管线边界与日志测试。
- Create `tests/test_render_batch_pixels.py`: opt-in Mesa 像素回归。
- Modify `README.md`: 配置语义和实测结果。

---

### Task 1: TorchServe Worker Runtime Configuration

**Files:**
- Create: `docker/torchserve-entrypoint.sh`
- Create: `tests/test_torchserve_config.py`
- Modify: `docker-compose.yml:35-78`
- Modify locally, do not commit: `.env`

**Interfaces:**
- Consumes: `/home/torchserve/config.properties` and optional `TORCHSERVE_WORKERS_PER_MODEL`.
- Produces: `/tmp/torchserve-config.properties`; only a non-empty value adds `default_workers_per_model=N`.
- Test overrides: `TORCHSERVE_BIN`, `TORCHSERVE_BASE_CONFIG`, `TORCHSERVE_RUNTIME_CONFIG`.

- [ ] **Step 1: Write failing executable tests**

Create `tests/test_torchserve_config.py`:

```python
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'docker' / 'torchserve-entrypoint.sh'


def _run(tmp_path, workers):
    base = tmp_path / 'base.properties'
    runtime = tmp_path / 'runtime.properties'
    captured = tmp_path / 'captured.properties'
    base.write_text('load_models=all\n')
    fake = tmp_path / 'torchserve'
    fake.write_text(
        '#!/bin/sh\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--ts-config" ]; then cp "$2" "$CAPTURE_CONFIG"; exit 0; fi\n'
        '  shift\n'
        'done\n'
        'exit 9\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ,
           'TORCHSERVE_BIN': str(fake),
           'TORCHSERVE_BASE_CONFIG': str(base),
           'TORCHSERVE_RUNTIME_CONFIG': str(runtime),
           'CAPTURE_CONFIG': str(captured),
           'TORCHSERVE_WORKERS_PER_MODEL': workers}
    result = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True)
    return result, captured.read_text() if captured.exists() else ''


@pytest.mark.parametrize('workers', ['', '1', '2', '08'])
def test_entrypoint_accepts_empty_or_positive_integer(tmp_path, workers):
    result, config = _run(tmp_path, workers)
    assert result.returncode == 0, result.stderr
    expected = '' if workers == '' else 'default_workers_per_model={}\n'.format(workers)
    assert config == 'load_models=all\n' + expected


@pytest.mark.parametrize('workers', ['0', '-1', '1.5', 'two', ' 2'])
def test_entrypoint_rejects_invalid_worker_count(tmp_path, workers):
    result, config = _run(tmp_path, workers)
    assert result.returncode != 0
    assert 'TORCHSERVE_WORKERS_PER_MODEL' in result.stderr
    assert config == ''


def test_compose_wires_optional_worker_count_and_readonly_entrypoint():
    compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    service = compose['services']['torchserve']
    assert service['entrypoint'] == ['/torchserve-entrypoint.sh']
    assert service['environment']['TORCHSERVE_WORKERS_PER_MODEL'] == '${TORCHSERVE_WORKERS_PER_MODEL:-}'
    assert './docker/torchserve-entrypoint.sh:/torchserve-entrypoint.sh:ro' in service['volumes']
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_torchserve_config.py -v`

Expected: FAIL because the script and Compose wiring do not exist.

- [ ] **Step 3: Implement the runtime entrypoint**

Create `docker/torchserve-entrypoint.sh`:

```sh
#!/bin/sh
set -eu
base_config="${TORCHSERVE_BASE_CONFIG:-/home/torchserve/config.properties}"
runtime_config="${TORCHSERVE_RUNTIME_CONFIG:-/tmp/torchserve-config.properties}"
torchserve_bin="${TORCHSERVE_BIN:-/opt/conda/bin/torchserve}"
workers="${TORCHSERVE_WORKERS_PER_MODEL:-}"
cp "$base_config" "$runtime_config"
if [ -n "$workers" ]; then
    case "$workers" in
        *[!0-9]*) echo "TORCHSERVE_WORKERS_PER_MODEL 必须是正整数，当前值：$workers" >&2; exit 2 ;;
    esac
    if [ "$workers" -le 0 ]; then
        echo "TORCHSERVE_WORKERS_PER_MODEL 必须大于 0，当前值：$workers" >&2
        exit 2
    fi
    printf 'default_workers_per_model=%s\n' "$workers" >> "$runtime_config"
    echo "TorchServe 每模型 worker 数：$workers"
else
    echo "TorchServe 每模型 worker 数：未配置，使用原生默认"
fi
exec "$torchserve_bin" --start --foreground --disable-token-auth --ts-config "$runtime_config"
```

Run: `chmod +x docker/torchserve-entrypoint.sh`

- [ ] **Step 4: Wire Compose without rebuilding TorchServe**

Add to `torchserve`:

```yaml
    entrypoint: ["/torchserve-entrypoint.sh"]
    environment:
      NVIDIA_VISIBLE_DEVICES: ${NVIDIA_VISIBLE_DEVICES:-all}
      TORCHSERVE_WORKERS_PER_MODEL: ${TORCHSERVE_WORKERS_PER_MODEL:-}
    volumes:
      - ./docker/torchserve-entrypoint.sh:/torchserve-entrypoint.sh:ro
```

Add comments that `N` applies separately to both models and blank preserves the native default. Do not use `:-12`.

- [ ] **Step 5: Set the approved local value**

Add to ignored `.env`:

```dotenv
# 每个模型分别启动 N 个 worker；2 表示检测 2 + 姿态 2，共 4 个。
# 留空保持 TorchServe 原生默认。
TORCHSERVE_WORKERS_PER_MODEL=2
```

Run: `git check-ignore -v .env && git status --short`

Expected: `.env` does not appear in status.

- [ ] **Step 6: Verify tests and rendered Compose**

```bash
python -m pytest tests/test_torchserve_config.py -v
docker compose config > /tmp/jjhks-compose-rendered.yml
rg -n "TORCHSERVE_WORKERS_PER_MODEL|torchserve-entrypoint" /tmp/jjhks-compose-rendered.yml
```

Expected: tests PASS and rendered value is `2`.

- [ ] **Step 7: Commit**

```bash
git add docker/torchserve-entrypoint.sh docker-compose.yml tests/test_torchserve_config.py
git commit -m "feat: configure TorchServe model workers"
```

---

### Task 2: Reusable Sequential Render Lifecycle

**Files:**
- Modify: `app/services/render_scene.py`
- Create: `tests/test_render_batch.py`

**Interfaces:**
- Consumes: `Sequence[Tuple[str, Path, Path]]` as `(name, motion_cfg, output_gif)`.
- Produces: `render_animations(...) -> Dict[str, Path]` preserving input order.
- Keeps: `render_animation(...) -> Path` unchanged.
- Internal test seam: `_load_vendor_components()` returns vendor Config, controller, AnimatedDrawing, Scene and View classes.

- [ ] **Step 1: Write failing input-contract tests**

```python
from pathlib import Path
import pytest
from app.services import render_scene


def test_render_animations_rejects_empty_motion_list(tmp_path):
    with pytest.raises(ValueError, match='至少一个动作'):
        render_scene.render_animations(tmp_path, [])


def test_render_animations_rejects_duplicate_motion_names(tmp_path):
    motion = tmp_path / 'm.yaml'
    with pytest.raises(ValueError, match='动作名不能重复'):
        render_scene.render_animations(
            tmp_path,
            [('run', motion, tmp_path / 'a.gif'),
             ('run', motion, tmp_path / 'b.gif')])
```

Run: `python -m pytest tests/test_render_batch.py -v`

Expected: FAIL because `render_animations` does not exist.

- [ ] **Step 2: Add validation and a lazy vendor seam**

Add to `render_scene.py`:

```python
import logging
import time
from typing import Dict, List, Sequence, Tuple

logger = logging.getLogger(__name__)
MotionRender = Tuple[str, Path, Path]


def _validate_motions(motions: Sequence[MotionRender]) -> List[MotionRender]:
    items = [(name, Path(cfg).resolve(), Path(output).resolve())
             for name, cfg, output in motions]
    if not items:
        raise ValueError('批量渲染至少需要一个动作')
    names = [item[0] for item in items]
    if len(names) != len(set(names)):
        raise ValueError('批量渲染动作名不能重复')
    return items


def _load_vendor_components():
    from animated_drawings.config import Config
    from animated_drawings.controller.video_render_controller import VideoRenderController
    from animated_drawings.model.animated_drawing import AnimatedDrawing
    from animated_drawings.model.scene import Scene
    from animated_drawings.view.view import View
    return Config, VideoRenderController, AnimatedDrawing, Scene, View
```

Initially validate then raise `NotImplementedError`. Verify the two validation tests pass.

- [ ] **Step 3: Add fake-component lifecycle tests**

Define fakes in `tests/test_render_batch.py` that record these events: `view.create`, `drawing.create`, `drawing.time`, `drawing.update`, `drawing.runtime-check`, `drawing.retarget`, `scene.time`, `render.start`, `render.end`, `view.cleanup`. Patch `_load_vendor_components()` and scene YAML preparation.

Required assertions:

```python
assert drawing_create_events == [('drawing.create', 'run')]
assert render_start_events == [('render.start', 'run'), ('render.start', 'jump')]
assert reset_index < update_index < jump_retarget_index
assert ('scene.time', 0.0) in events[jump_retarget_index:]
assert events.count('view.cleanup') == 1
```

Add separate success, first-action failure, and second-action failure tests; all failure paths must assert one cleanup.

Run: `python -m pytest tests/test_render_batch.py -v`

Expected: validation tests PASS; lifecycle tests FAIL at `NotImplementedError`.

- [ ] **Step 4: Implement the reusable lifecycle**

Implementation sequence in `render_animations()`:

1. Write one scene YAML per action with existing `build_scene_cfg()`.
2. Load all vendor `Config` objects before mutating them.
3. Save the first character tuple, set `first.scene.animated_characters = []`, then create an empty Scene.
4. Create one View. After `_load_vendor_components()` returns the concrete vendor class, define `_LoggedAnimatedDrawing` through a local class factory that subclasses that concrete class; instantiate it once and add it through `scene.add_child()`. Do not monkeypatch the vendor module.
5. Use `_SequentialVideoRenderController`, which closes writer/progress bar but does not clean the View.
6. Before later actions call `_switch_motion()`:

```python
def _switch_motion(drawing, scene, motion_cfg, retarget_cfg) -> None:
    drawing.set_time(0.0)
    drawing.update()
    drawing.retarget_cfg = retarget_cfg
    drawing._modify_retargeting_cfg_for_character()
    drawing._initialize_retargeter_bvh(motion_cfg, retarget_cfg)
    scene.set_time(0.0)
    drawing.set_time(0.0)
    drawing.update()
```

7. Render one action at a time and verify its GIF exists before recording it.
8. Call `view.cleanup()` once in the outer `finally`.

Controller cleanup:

```python
class _SequentialVideoRenderController(VideoRenderController):
    def _cleanup_after_run_loop(self) -> None:
        logger.info('顺序渲染完成：%d 帧', self.frames_rendered)
        self.progress_bar.close()
        self.video_writer.cleanup()
```

- [ ] **Step 5: Run focused regressions**

```bash
python -m pytest tests/test_render_batch.py tests/test_render_scene_mesa_env.py tests/test_vendor_paths.py -v
```

Expected: PASS; single-action behavior remains green.

- [ ] **Step 6: Commit**

```bash
git add app/services/render_scene.py tests/test_render_batch.py
git commit -m "feat: reuse character render state across motions"
```

---

### Task 3: Mesh, ARAP, and Per-Motion Timing Logs

**Files:**
- Modify: `app/services/render_scene.py`
- Modify: `tests/test_render_batch.py`

**Interfaces:**
- Produces: `_mesh_metrics(vertices, triangles, pin_count) -> Dict[str, int]`.
- Logs before ARAP: mask size, vertices, triangles, unique edges, pins and estimated dense bytes.
- Logs after ARAP: effective pins and actual A1/A2 shapes and bytes.

- [ ] **Step 1: Write failing deterministic metric tests**

Add to `tests/test_render_batch.py`:

```python
import numpy as np


def test_mesh_metrics_calculates_unique_edges_and_dense_bytes():
    vertices = np.zeros((4, 2), dtype=np.float32)
    triangles = [np.array([0, 1, 2]), np.array([1, 2, 3])]
    metrics = render_scene._mesh_metrics(vertices, triangles, pin_count=2)
    assert metrics['vertices'] == 4
    assert metrics['triangles'] == 2
    assert metrics['edges'] == 5
    assert metrics['a1_rows'] == 14
    assert metrics['a1_cols'] == 8
    assert metrics['a1_bytes'] == 14 * 8 * 4
    assert metrics['g_bytes'] == 10 * 8 * 4
    assert metrics['a2_bytes'] == 7 * 4 * 4
    assert metrics['normal1_bytes'] == 8 * 8 * 4
    assert metrics['normal2_bytes'] == 4 * 4 * 4
```

Add `caplog` assertions to the lifecycle test:

```python
assert '动作 run retarget' in caplog.text
assert '动作 run 渲染' in caplog.text
assert '动作 jump retarget' in caplog.text
assert '动作 jump 渲染' in caplog.text
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_render_batch.py -v`

Expected: FAIL because metrics and timing messages are absent.

- [ ] **Step 3: Implement size estimation from the existing mesh**

```python
def _mesh_metrics(vertices, triangles, pin_count: int) -> Dict[str, int]:
    edges = set()
    for v0, v1, v2 in triangles:
        edges.update((tuple(sorted((int(v0), int(v1)))),
                      tuple(sorted((int(v1), int(v2)))),
                      tuple(sorted((int(v2), int(v0))))))
    vertex_count, edge_count = len(vertices), len(edges)
    a1_rows, a1_cols = 2 * (edge_count + pin_count), 2 * vertex_count
    a2_rows, a2_cols = edge_count + pin_count, vertex_count
    return {
        'vertices': vertex_count, 'triangles': len(triangles),
        'edges': edge_count, 'pins': pin_count,
        'a1_rows': a1_rows, 'a1_cols': a1_cols,
        'a1_bytes': a1_rows * a1_cols * 4,
        'g_bytes': (2 * edge_count) * a1_cols * 4,
        'a2_bytes': a2_rows * a2_cols * 4,
        'normal1_bytes': a1_cols * a1_cols * 4,
        'normal2_bytes': a2_cols * a2_cols * 4,
    }
```

Override `_LoggedAnimatedDrawing._generate_mesh()`: call `super()`, then log mask dimensions and these metrics before ARAP construction. After `super().__init__()` returns, log `arap.pin_num`, `A1.shape/nbytes`, and `A2.shape/nbytes`. Do not recompute contours, mesh, or ARAP for logging.

- [ ] **Step 4: Add retarget and render timers**

Use `time.perf_counter()` around initial construction, `_switch_motion()`, and each controller `run()`:

```python
logger.info('动作 %s retarget 完成，耗时 %.3f 秒', motion_name, elapsed)
logger.info('动作 %s 渲染完成，耗时 %.3f 秒', motion_name, elapsed)
```

The first action must emit the same `动作 run retarget` key even though it occurs during static construction.

- [ ] **Step 5: Verify and commit**

```bash
python -m pytest tests/test_render_batch.py tests/test_render_scene_mesa_env.py -v
git add app/services/render_scene.py tests/test_render_batch.py
git commit -m "feat: log render stages and mesh size"
```

---

### Task 4: Character Pipeline Integration and Stage Timing

**Files:**
- Modify: `app/services/character_pipeline.py`
- Create: `tests/test_character_pipeline_timing.py`
- Modify: `tests/test_character_pipeline_contract.py`

**Interfaces:**
- Consumes: `render_animations(char_anno_dir, motions, use_mesa=None, retarget_cfg=None)`.
- Produces unchanged `status/animations` response with `run` and `jump`.
- Logs analyze, repair, each synth, batch render, each sprite and total.

- [ ] **Step 1: Write a failing batch-boundary test**

Create `tests/test_character_pipeline_timing.py`:

```python
import logging
from app.services import character_pipeline
from app.services.character_pipeline import CharacterPipeline


def test_render_character_uses_one_ordered_batch_and_logs_stages(tmp_path, monkeypatch, caplog):
    calls = []

    def fake_analyze(image, anno):
        anno.mkdir(parents=True)
        (anno / 'char_cfg.yaml').write_text('skeleton: []\n')

    def fake_synth(char_cfg, motion, out_dir):
        calls.append(('synth', motion))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / (motion + '.yaml')
        path.write_text('filepath: fake.bvh\n')
        return path

    def fake_render(anno, motions, **kwargs):
        calls.append(('render', [item[0] for item in motions]))
        return {name: output for name, cfg, output in motions}

    monkeypatch.setattr(character_pipeline, 'analyze', fake_analyze)
    monkeypatch.setattr(character_pipeline, 'repair_or_reject',
                        lambda anno: calls.append('repair'))
    monkeypatch.setattr(character_pipeline, 'synth_motion', fake_synth)
    monkeypatch.setattr(character_pipeline, 'render_animations', fake_render)
    monkeypatch.setattr(character_pipeline, 'compute_content_bbox',
                        lambda gif: (0, 0, 10, 20))
    monkeypatch.setattr(character_pipeline, 'build_sprite_sheet',
                        lambda gif, output, **kwargs: {
                            'frameCount': 1, 'fps': 15,
                            'frameWidth': 12, 'frameHeight': 24,
                            'footAnchor': {'x': 6, 'y': 23}})
    caplog.set_level(logging.INFO)
    result = CharacterPipeline(tmp_path).render_character(tmp_path / 'hero.png')
    assert calls == ['repair', ('synth', 'run'), ('synth', 'jump'),
                     ('render', ['run', 'jump'])]
    assert list(result['animations']) == ['run', 'jump']
    for stage in ('analyze', 'repair', 'synth_motion.run', 'synth_motion.jump',
                  'render_animations', 'sprite_sheet.run',
                  'sprite_sheet.jump', 'total'):
        assert stage in caplog.text
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_character_pipeline_timing.py -v`

Expected: FAIL because the pipeline calls `render_animation()` twice and has no stage logs.

- [ ] **Step 3: Add timing and one batch call**

```python
import logging
import time
from app.services.render_scene import render_animation, render_animations

logger = logging.getLogger(__name__)


def _timed(stage, operation):
    started = time.perf_counter()
    try:
        return operation()
    finally:
        logger.info('角色管线阶段 %s 完成，耗时 %.3f 秒',
                    stage, time.perf_counter() - started)
```

Wrap analyze, repair, each synth, batch render, each sprite and total. Build motion triples in caller order, call `render_animations()` once, preserve frame normalization and the response shape, and keep single-motion `render()` on `render_animation()`.

- [ ] **Step 4: Verify regressions**

```bash
python -m pytest tests/test_character_pipeline_timing.py \
  tests/test_character_pipeline_contract.py tests/test_render_runner.py \
  tests/test_character_worker.py -v
```

Expected: PASS or existing TorchServe-dependent tests retain their skip behavior.

- [ ] **Step 5: Commit**

```bash
git add app/services/character_pipeline.py tests/test_character_pipeline_timing.py \
  tests/test_character_pipeline_contract.py
git commit -m "feat: render character motions in one batch"
```

---

### Task 5: Pixel-Equivalent Mesa Regression

**Files:**
- Create: `tests/test_render_batch_pixels.py`
- Modify when the exact regression reports lifecycle state leakage: `app/services/render_scene.py`

**Interfaces:**
- Baseline: two existing `render_animation(..., use_mesa=True)` calls.
- Candidate: one `render_animations(..., use_mesa=True)` call.
- Equality: decoded RGBA frames, duration, frame count, dimensions, sprite pixels and metadata.

- [ ] **Step 1: Add the opt-in integration regression**

Create `tests/test_render_batch_pixels.py`:

```python
import os
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from app.services.motion_2d import FPS, synth_motion
from app.services.render_scene import render_animation, render_animations
from app.services.sprite_sheet import build_sprite_sheet

ROOT = Path(__file__).resolve().parents[1]
ANNO = ROOT / 'vendor' / 'AnimatedDrawings' / 'examples' / 'characters' / 'char3'


def _gif_frames(path):
    image = Image.open(path)
    frames, durations = [], []
    try:
        while True:
            frames.append(np.asarray(image.convert('RGBA')).copy())
            durations.append(image.info.get('duration'))
            image.seek(image.tell() + 1)
    except EOFError:
        return frames, durations


@pytest.mark.skipif(os.environ.get('RUN_RENDER_INTEGRATION') != '1',
                    reason='set RUN_RENDER_INTEGRATION=1 inside Mesa image')
def test_batch_render_matches_two_single_renders_pixel_for_pixel(tmp_path):
    specs, baseline, batch = [], {}, {}
    for motion in ('run', 'jump'):
        motion_dir = tmp_path / motion
        cfg = synth_motion(ANNO / 'char_cfg.yaml', motion, motion_dir)
        baseline[motion] = tmp_path / 'baseline' / (motion + '.gif')
        batch[motion] = tmp_path / 'batch' / motion / (motion + '.gif')
        render_animation(ANNO, cfg, baseline[motion], use_mesa=True)
        specs.append((motion, cfg, batch[motion]))
    rendered = render_animations(ANNO, specs, use_mesa=True)
    assert rendered == batch
    for motion in ('run', 'jump'):
        old_frames, old_duration = _gif_frames(baseline[motion])
        new_frames, new_duration = _gif_frames(batch[motion])
        assert old_duration == new_duration
        assert len(old_frames) == len(new_frames)
        for old, new in zip(old_frames, new_frames):
            np.testing.assert_array_equal(old, new)
        old_sheet = tmp_path / ('old-' + motion + '.png')
        new_sheet = tmp_path / ('new-' + motion + '.png')
        old_meta = build_sprite_sheet(baseline[motion], old_sheet, fps=FPS)
        new_meta = build_sprite_sheet(batch[motion], new_sheet, fps=FPS)
        assert old_meta == new_meta
        np.testing.assert_array_equal(np.asarray(Image.open(old_sheet)),
                                      np.asarray(Image.open(new_sheet)))
```

- [ ] **Step 2: Run in the server image**

```bash
docker compose build api
docker compose run --rm -e RUN_RENDER_INTEGRATION=1 \
  -v "$PWD/tests:/app/tests:ro" \
  worker python -m pytest tests/test_render_batch_pixels.py -v
```

Expected: PASS, or an exact pixel/state mismatch identifying lifecycle leakage. Do not add tolerance.

- [ ] **Step 3: Correct only lifecycle state when the exact regression fails**

Allowed corrections: restore old action to frame zero; create fresh MotionConfig/RetargetConfig; rerun runtime checks; reset Scene/drawing time; keep one View alive. Do not change mesh density, ARAP math, camera, retarget YAML, recipe, frames, or assertions.

- [ ] **Step 4: Re-run all affected tests**

```bash
docker compose build api
docker compose run --rm -e RUN_RENDER_INTEGRATION=1 \
  -v "$PWD/tests:/app/tests:ro" \
  worker python -m pytest tests/test_render_batch_pixels.py -v
python -m pytest tests/test_render_batch.py tests/test_character_pipeline_timing.py -v
```

Expected: exact RGBA test and focused unit tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_render_batch_pixels.py
git add app/services/render_scene.py
git commit -m "test: verify batch render pixel equivalence"
```

Skip the second `git add` when this task does not change `render_scene.py`.

---

### Task 6: Docker Performance Acceptance and Documentation

**Files:**
- Modify: `README.md`
- Generated, do not commit: `out/logs/**`, `out/jobs/**`, `/tmp/jjhks-render-stats.log`

**Interfaces:**
- Runtime API: `POST /v1/characters?force=true`, then poll `statusUrl`.
- Management API: `/models/drawn_humanoid_detector` and `/models/drawn_humanoid_pose_estimator`.
- Acceptance sample: `testdata/myson/man1.jpg`.

- [ ] **Step 1: Run the fast regression suite**

```bash
python -m pytest tests/ -q \
  --ignore=tests/test_spike_batch.py \
  --ignore=tests/test_render_batch_pixels.py
```

Expected: zero failures.

- [ ] **Step 2: Recreate runtime services**

```bash
docker compose up -d --force-recreate torchserve
docker compose build api
docker compose up -d --force-recreate api worker level-worker nginx
docker compose ps
```

Expected: TorchServe uses its existing image; server dependency/vendor layers hit cache; health checks pass.

- [ ] **Step 3: Verify model counts and idle memory**

```bash
curl -sS http://localhost:8081/models/drawn_humanoid_detector \
  | jq '.[0] | {minWorkers,maxWorkers,count:(.workers|length)}'
curl -sS http://localhost:8081/models/drawn_humanoid_pose_estimator \
  | jq '.[0] | {minWorkers,maxWorkers,count:(.workers|length)}'
docker stats --no-stream ad-torchserve pg-worker
```

Expected for both: `minWorkers=2`, `maxWorkers=2`, `count=2`. Record idle memory.

- [ ] **Step 4: Run one controlled character task**

Submit `testdata/myson/man1.jpg` to `POST http://localhost:8000/v1/characters?force=true`, extract `jobId`, and poll `GET /v1/characters/{jobId}` once per second. Print elapsed seconds at every state change.

Expected: terminal state `ready`. `RENDER_CRASHED`, timeout, OOM, or a changed business result fails acceptance.

- [ ] **Step 5: Capture peaks, stage logs, and OOM evidence**

While Step 4 is processing, sample every two seconds:

```bash
docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' \
  ad-torchserve pg-worker pg-api | tee -a /tmp/jjhks-render-stats.log
```

After completion, substitute the actual job ID:

```bash
rg -n "$job_id|角色管线阶段|网格规模|ARAP|动作 run|动作 jump" \
  out/logs/render-runner-$(date '+%Y-%m-%d').log
docker events --since 20m --until 0s --filter container=pg-worker \
  --format '{{.Time}} {{.Action}}' | rg 'oom' || true
```

Expected: all stages and metrics appear; no new OOM event.

- [ ] **Step 6: Verify blank restores native defaults, then restore `2`**

Set `TORCHSERVE_WORKERS_PER_MODEL=` in `.env`, recreate TorchServe, and query the detector. Expected: CPU-derived native count, not 2. Restore `TORCHSERVE_WORKERS_PER_MODEL=2`, recreate it, and verify both counts return to 2.

- [ ] **Step 7: Document configuration and measured results**

Update `README.md` with:

```dotenv
# 每个模型分别启动 N 个 worker；两个模型在 N=2 时总计 4 个。
# 留空不覆盖 TorchServe 原生默认。
TORCHSERVE_WORKERS_PER_MODEL=2
```

Also record: positive integers only; change via `docker compose up -d --force-recreate torchserve`; no TorchServe image rebuild; `1` for low memory and `2` for limited headroom; run/jump remain sequential but share initialization; actual idle memory, worker peak, duration, terminal state and OOM result; `force=true` remains for tests and explicit reruns.

- [ ] **Step 8: Verify and commit documentation**

```bash
python -m pytest tests/test_torchserve_config.py tests/test_render_batch.py \
  tests/test_character_pipeline_timing.py -v
git diff --check
git add README.md
git commit -m "docs: document character render tuning"
```

---

### Task 7: Final Regression and Scope Verification

**Files:**
- Verify only; modify only files implicated by an in-scope failure.

**Interfaces:**
- Confirms `docs/superpowers/specs/2026-09-18-character-render-performance-design.md` end to end.

- [ ] **Step 1: Run normal and pixel suites**

```bash
python -m pytest tests/ -q \
  --ignore=tests/test_spike_batch.py \
  --ignore=tests/test_render_batch_pixels.py
docker compose run --rm -e RUN_RENDER_INTEGRATION=1 \
  -v "$PWD/tests:/app/tests:ro" \
  worker python -m pytest tests/test_render_batch_pixels.py -v
```

Expected: zero normal failures and exact RGBA regression PASS.

- [ ] **Step 2: Run existing smoke tests**

```bash
bash scripts/smoke/smoke_e2e.sh testdata/characters/s01.png
bash scripts/smoke/smoke_levels_e2e.sh
```

Expected: `SMOKE_E2E_PASS` and `SMOKE_LEVELS_E2E_PASS`.

- [ ] **Step 3: Verify runtime health and counts**

Run `docker compose ps`, `curl -sS http://localhost:8080/ping`, and query both model management endpoints with the jq expression from Task 6 Step 3.

Expected: services healthy, ping healthy, both models report 2 workers.

- [ ] **Step 4: Verify scope**

```bash
git status --short
git diff HEAD~5 --stat
git diff HEAD~5 -- vendor/AnimatedDrawings/animated_drawings
git diff --check
```

Expected: no vendor Python changes; `.env`, `out/**`, and `testdata/myson/**` are not staged; no API, Redis, recipe, or level-parser changes.

- [ ] **Step 5: Handle verification corrections**

If an in-scope defect is found, add only its exact files, rerun the failing command, and commit with `fix: address render performance verification`. If none is found, do not create an empty commit.

- [ ] **Step 6: Request review and finish**

Use `superpowers:requesting-code-review`. Resolve findings through `superpowers:receiving-code-review`, rerun affected verification, then use `superpowers:finishing-a-development-branch` for integration choices.
