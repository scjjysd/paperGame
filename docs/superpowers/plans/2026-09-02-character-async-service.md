# 角色异步服务化（P0 任务 3）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把单机 `CharacterPipeline.render_character()` 服务化为 FastAPI + Redis 队列 + 独立 worker 容器的异步 HTTP 服务，全部组件由 docker-compose 编排。

**架构：** api 容器（上传/轮询/静态伺服）与 worker 容器（BRPOP 消费、子进程渲染）共享同一镜像；渲染在子进程内执行以隔离 chdir 污染与 GLFW 崩溃；容器内用 Mesa 软渲染（`USE_MESA=True`），vendor 硬编码的 `localhost:8080` 由 entrypoint 里的 socat 转发到 torchserve 容器。规格见 `docs/superpowers/specs/2026-09-02-character-async-service-design.md`。

**技术栈：** FastAPI、uvicorn、redis-py、Redis 7、Pydantic v2、pytest + fakeredis + httpx、Docker（python:3.9-slim + OSMesa）、既有管线（server/app/services/）。

## 全局约束（每个任务都受约束）

- **脚本/服务硬分离**：`server/scripts/` 是验证脚本区（可 import app）；`server/app/` 是服务区（禁止 import scripts）。
- **不改 vendor**：`server/vendor/AnimatedDrawings` 零改动。
- **路径纪律**：跨进程/容器传递的路径一律绝对化（chdir 教训）；容器内约定 `OUT_ROOT=/data/out`、资产在 `/app/server/app/assets/motions`。
- **契约键名**：`spriteSheetUrl/frameCount/fps/frameWidth/frameHeight/footAnchor{x,y}`，与既有管线输出逐字一致；错误码 `FILE_TOO_LARGE/NOT_AN_IMAGE/UNSUPPORTED_FORMAT/JOB_NOT_FOUND/QUEUE_UNAVAILABLE/RENDER_TIMEOUT/RENDER_CRASHED/ASSET_MISSING/INTERNAL`。
- **回归线**：既有 8 个管线测试（test_sprite_sheet/test_annotations/test_character_pipeline_contract）必须保持全绿。
- **已核实的接口事实**：`render_character(input_path, motions=('run','jump'))` 产物在 `out_root/<stem>/anno`、`out_root/<stem>/<motion>/<motion>.png`（stem 由输入文件名决定，输入存为 `input.png` 则 stem='input'）；`render_animation(..., use_mesa=False)` 有该参数但门面未透传；vendor `image_to_annotations` POST 硬编码 `http://localhost:8080`；MesaView 自行设置 `PYOPENGL_PLATFORM=osmesa` 与 `MESA_GL_VERSION_OVERRIDE=3.3`；vendor setup.py 钉死 numpy==1.24.4/Pillow==10.1.0/opencv-python==4.6.0.66 等。

## 文件结构

```text
server/
  Dockerfile                        # api+worker 共享镜像（任务 1）
  .dockerignore                     # 排除 .git/.venv/out（任务 1）
  requirements-service.txt          # 镜像内运行时依赖（任务 1）
  requirements-dev.txt              # 修改：追加 fastapi/uvicorn/redis/python-multipart/fakeredis/httpx（任务 3）
  docker/entrypoint.sh              # socat 8080 转发 + exec CMD（任务 1）
  docker-compose.yml                # 修改：新增 redis/api/worker（任务 7）
  scripts/
    render_smoke.py                 # 任务 1：容器内 Mesa 渲染尖刺脚本
    smoke_e2e.sh                    # 任务 8：全栈端到端冒烟
    run_worker_host.sh              # 仅 Mesa 尖刺失败时创建（回退分支）
  app/
    contracts.py                    # Pydantic 契约模型 + derive_job_id（任务 3）
    main.py                         # create_app 工厂 + /healthz + /artifacts 静态挂载（任务 5）
    api/__init__.py
    api/characters.py               # POST/GET 路由（任务 5）
    workers/__init__.py
    workers/render_runner.py        # 子进程入口：渲染→契约布局→result.json（任务 6）
    workers/character_worker.py     # BRPOP 主循环 + 超时/重试/映射（任务 6）
    services/job_store.py           # Redis 状态存储 + result.json 兜底（任务 4）
    services/render_scene.py        # 修改：RENDER_USE_MESA env 默认值（任务 2）
  tests/
    test_render_scene_mesa_env.py   # 任务 2
    test_contracts.py               # 任务 3
    test_job_store.py               # 任务 4
    test_characters_api.py          # 任务 5
    test_character_worker.py        # 任务 6
docs/async-service-smoke-results.md # 任务 8：冒烟与回归记录
```

**执行顺序**：1 → 2 → 3 → 4 → 5 → 6 → 7 → 8。任务 1 是 go/no-go 门：Mesa 失败走"回退分支"（见任务 1 步骤 6），任务 2-6、8 不变，仅任务 7 的 compose 形态调整。

---

### 任务 1：服务镜像 + 容器内 Mesa 渲染尖刺（go/no-go）

**交付物：** 可构建的共享镜像；容器内以 OSMesa 渲染出透明 GIF 的实证；或触发回退分支的实证。

**文件：**
- 创建：`server/requirements-service.txt`
- 创建：`server/Dockerfile`
- 创建：`server/.dockerignore`
- 创建：`server/docker/entrypoint.sh`
- 创建：`server/scripts/render_smoke.py`

- [ ] **步骤 1：创建 `server/requirements-service.txt`**（vendor 运行时子集，跳过 torchserve/Flask/scikit-learn——worker 不需要，省约 800MB torch 下载；加服务依赖）

```text
numpy==1.24.4
scipy==1.10.0
scikit-image==0.19.3
shapely==1.8.5.post1
opencv-python==4.6.0.66
Pillow==10.1.0
glfw==2.5.5
PyOpenGL==3.1.6
PyYAML==6.0.1
requests==2.31.0
tqdm==4.66.3
fastapi>=0.110,<1.0
uvicorn>=0.29
redis>=5.0,<6.0
python-multipart>=0.0.9
```

- [ ] **步骤 2：创建 `server/.dockerignore`**

```text
.venv/
out/
__pycache__/
*.pyc
vendor/AnimatedDrawings/.git/
tests/
```

- [ ] **步骤 3：创建 `server/docker/entrypoint.sh`**（vendor 硬编码 localhost:8080，容器内用 socat 把 8080 转发到上游 TorchServe；上游地址由 `TORCHSERVE_UPSTREAM` 控制，compose 内默认 `torchserve:8080`）

```sh
#!/bin/sh
set -e
UPSTREAM="${TORCHSERVE_UPSTREAM:-torchserve:8080}"
socat TCP-LISTEN:8080,fork,reuseaddr "TCP:${UPSTREAM}" &
exec "$@"
```

- [ ] **步骤 4：创建 `server/Dockerfile`**（工作目录布局与宿主一致：`/app/server` ↔ `server/`，保证 `render_scene.REPO_ROOT=parents[3]`、`annotations` 的 vendor examples 路径推导在容器内同样成立）

```dockerfile
FROM python:3.9-slim

# opencv-python 需要 libgl1/libglib2.0-0；Mesa 软渲染需要 libosmesa6；socat 供 entrypoint 转发
RUN apt-get update && apt-get install -y --no-install-recommends \
    libosmesa6 libosmesa6-dev libgl1 libglib2.0-0 socat curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/server

COPY requirements-service.txt .
RUN pip install --no-cache-dir -r requirements-service.txt

COPY vendor/AnimatedDrawings ./vendor/AnimatedDrawings
RUN pip install --no-cache-dir -e ./vendor/AnimatedDrawings --no-deps

COPY app ./app
COPY scripts ./scripts
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV RENDER_USE_MESA=true \
    OUT_ROOT=/data/out \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/server

VOLUME ["/data/out"]
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

注意：CMD 引用的 `app.main` 在任务 5 才存在；本任务构建镜像不报错（CMD 仅运行时执行，尖刺用显式命令覆盖）。

- [ ] **步骤 5：创建 `server/scripts/render_smoke.py`**（容器内尖刺：analyze 经 socat 打到宿主 TorchServe，Mesa 渲染 run 动作，校验 GIF 非空且透明）

```python
"""容器内 Mesa 渲染尖刺：go/no-go 判据。用法（容器内）：python scripts/render_smoke.py <input_png> <out_dir>"""
import sys
from pathlib import Path

from PIL import Image

from app.services.annotations import analyze
from app.services.render_scene import VENDOR, render_animation


def main() -> int:
    input_png, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    analyze(input_png, out_dir / 'anno')
    gif = render_animation(out_dir / 'anno',
                           Path('/app/server/app/assets/motions/run.yaml'),
                           out_dir / 'smoke.gif',
                           use_mesa=True)
    img = Image.open(gif).convert('RGBA')
    corner = img.getpixel((0, 0))
    assert corner[3] == 0, f'GIF 角落不透明: {corner}'
    n_frames = getattr(img, 'n_frames', 1)
    print(f'MESA_SMOKE_PASS frames={n_frames} size={img.size} gif_bytes={gif.stat().st_size}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **步骤 6：构建镜像并运行尖刺（go/no-go 判定点）**

前置：宿主 TorchServe 已启动（`curl http://localhost:8080/ping` 返回 Healthy）。

```bash
cd /Users/scjjysd/IdeaProjects/jjhks/server
docker build -t paper-game/server:local .
docker run --rm \
  -v "$PWD/out:/data/out" \
  -v "$PWD/../testdata:/data/testdata:ro" \
  -e TORCHSERVE_UPSTREAM=host.docker.internal:8080 \
  --add-host=host.docker.internal:host-gateway \
  paper-game/server:local \
  python scripts/render_smoke.py /data/testdata/characters/garlic.png /data/out/mesa-spike
```

预期（**GO**）：打印 `MESA_SMOKE_PASS frames=13 ...`，`out/mesa-spike/smoke.gif` 生成。

失败排查顺序（各试一次）：① 报 `GL.glGetString` 返回 None / OSMesa 创建失败 → Dockerfile 增装 `libgl1-mesa-dri` 重新构建；② analyze 连接拒绝 → 检查 socat 日志与 `--add-host`；③ 仍失败 → **NO-GO，走回退分支**：创建 `server/scripts/run_worker_host.sh`（内容：`#!/usr/bin/env bash` + `cd "$(dirname "$0")/.." && source .venv/bin/activate && exec python -m app.workers.character_worker`，env 用宿主默认 `RENDER_USE_MESA` 不设、`OUT_ROOT=$PWD/out`、`REDIS_URL=redis://localhost:6379/0`），任务 7 的 compose 去掉 worker 服务（api+redis 保留），并把结论记录到任务 8 的冒烟文档。

- [ ] **步骤 7：Commit**

```bash
git add server/Dockerfile server/.dockerignore server/requirements-service.txt server/docker/entrypoint.sh server/scripts/render_smoke.py
git commit -m "feat: add server image with in-container mesa render smoke"
```

### 任务 2：RENDER_USE_MESA 环境开关（render_scene 改造，TDD）

**交付物：** `render_animation` 的 `use_mesa=None` 时从环境变量取默认值；场景配置构建抽成可单测的纯函数。

**文件：**
- 修改：`server/app/services/render_scene.py`
- 测试：`server/tests/test_render_scene_mesa_env.py`

- [ ] **步骤 1：编写失败测试 `server/tests/test_render_scene_mesa_env.py`**

```python
from pathlib import Path

import yaml

from app.services.render_scene import build_scene_cfg


def _cfg(tmp_path, monkeypatch, env_value):
    if env_value is None:
        monkeypatch.delenv('RENDER_USE_MESA', raising=False)
    else:
        monkeypatch.setenv('RENDER_USE_MESA', env_value)
    anno = tmp_path / 'anno'
    anno.mkdir()
    motion = tmp_path / 'm.yaml'
    motion.write_text(yaml.safe_dump({'filepath': str(tmp_path / 'm.bvh')}))
    (tmp_path / 'm.bvh').write_text('dummy')
    return build_scene_cfg(anno, motion, tmp_path / 'o.gif', use_mesa=None)


def test_env_true_enables_mesa(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, 'true')
    assert cfg['view'] == {'USE_MESA': True}


def test_env_1_enables_mesa(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, '1')
    assert cfg['view'] == {'USE_MESA': True}


def test_env_absent_disables_mesa(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, None)
    assert 'view' not in cfg


def test_explicit_param_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv('RENDER_USE_MESA', 'true')
    anno = tmp_path / 'anno'
    anno.mkdir()
    motion = tmp_path / 'm.yaml'
    motion.write_text(yaml.safe_dump({'filepath': str(tmp_path / 'm.bvh')}))
    (tmp_path / 'm.bvh').write_text('dummy')
    cfg = build_scene_cfg(anno, motion, tmp_path / 'o.gif', use_mesa=False)
    assert 'view' not in cfg
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd /Users/scjjysd/IdeaProjects/jjhks/server && source .venv/bin/activate && python -m pytest tests/test_render_scene_mesa_env.py -v`
预期：FAIL（`ImportError: cannot import name 'build_scene_cfg'`）

- [ ] **步骤 3：修改 `server/app/services/render_scene.py`**——把 `render_animation` 中的 cfg 字典构建抽为 `build_scene_cfg`，`use_mesa=None` 时读环境变量；`render_animation` 其余逻辑不变（含 `_resolve_motion_cfg` 调用、chdir、GIF 存在性校验）：

```python
import os

def _mesa_default() -> bool:
    return os.environ.get('RENDER_USE_MESA', '').strip().lower() in ('1', 'true', 'yes')


def build_scene_cfg(char_anno_dir, motion_cfg_fn, out_gif, use_mesa=None) -> dict:
    """构建 AnimatedDrawings 渲染场景配置。use_mesa=None 时由 RENDER_USE_MESA 环境变量决定。"""
    char_anno_dir, out_gif = Path(char_anno_dir), Path(out_gif)
    if use_mesa is None:
        use_mesa = _mesa_default()
    cfg = {
        'scene': {'ANIMATED_CHARACTERS': [{
            'character_cfg': str(char_anno_dir / 'char_cfg.yaml'),
            'motion_cfg': str(_resolve_motion_cfg(Path(motion_cfg_fn))),
            'retarget_cfg': RETARGET_CFG,
        }]},
        'controller': {'MODE': 'video_render', 'OUTPUT_VIDEO_PATH': str(out_gif)},
    }
    if use_mesa:
        cfg['view'] = {'USE_MESA': True}
    return cfg


def render_animation(char_anno_dir, motion_cfg_fn, out_gif, use_mesa=None) -> Path:
    out_gif = Path(out_gif)
    out_gif.parent.mkdir(parents=True, exist_ok=True)
    cfg = build_scene_cfg(char_anno_dir, motion_cfg_fn, out_gif, use_mesa=use_mesa)

    scene_yaml = out_gif.with_suffix('.scene.yaml')
    scene_yaml.write_text(yaml.safe_dump(cfg))

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

同时把模块顶部原有的 `import os`（若仍在函数体内）统一移到文件头；`_resolve_motion_cfg`、`REPO_ROOT`、`VENDOR`、`RETARGET_CFG` 保持原样。

- [ ] **步骤 4：运行测试验证通过 + 回归**

运行：`python -m pytest tests/test_render_scene_mesa_env.py tests/test_sprite_sheet.py -v`
预期：全部 PASS（既有调用方 `use_mesa` 缺省行为不变：宿主无该环境变量时为 False）

- [ ] **步骤 5：Commit**

```bash
git add server/app/services/render_scene.py server/tests/test_render_scene_mesa_env.py
git commit -m "feat: drive mesa rendering via RENDER_USE_MESA env"
```

### 任务 3：契约模型 contracts.py（TDD）

**交付物：** Pydantic 契约模型 + `derive_job_id`；宿主环境补齐服务依赖。

**文件：**
- 修改：`server/requirements-dev.txt`
- 创建：`server/app/contracts.py`
- 测试：`server/tests/test_contracts.py`

- [ ] **步骤 1：`server/requirements-dev.txt` 追加服务与测试依赖并安装**

追加行：

```text
fastapi>=0.110,<1.0
uvicorn>=0.29
redis>=5.0,<6.0
python-multipart>=0.0.9
fakeredis>=2.21
httpx>=0.27
```

运行：`cd server && source .venv/bin/activate && pip install -r requirements-dev.txt`
预期：安装成功；`python -c "import fastapi, redis, fakeredis, pydantic; print(pydantic.VERSION)"` 打印 2.x 版本号。

- [ ] **步骤 2：编写失败测试 `server/tests/test_contracts.py`**

```python
import hashlib

import pytest
from pydantic import ValidationError

from app.contracts import (
    AnimationMeta, CharacterReady, FootAnchor, JobAccepted, JobFailed,
    NeedsCorrection, derive_job_id,
)


def test_derive_job_id_deterministic_and_prefixed():
    content = b'fake-png-bytes'
    jid = derive_job_id(content)
    assert jid == 'char_' + hashlib.sha256(content).hexdigest()[:12]
    assert derive_job_id(content) == jid


def test_derive_job_id_different_content_different_id():
    assert derive_job_id(b'a') != derive_job_id(b'b')


def test_animation_meta_contract_keys():
    meta = AnimationMeta(
        spriteSheetUrl='/artifacts/char_x/run.png', frameCount=13, fps=12,
        frameWidth=481, frameHeight=655, footAnchor=FootAnchor(x=240, y=655))
    dumped = meta.model_dump()
    assert set(dumped) == {'spriteSheetUrl', 'frameCount', 'fps', 'frameWidth', 'frameHeight', 'footAnchor'}
    assert dumped['footAnchor'] == {'x': 240, 'y': 655}


def test_character_ready_requires_both_motions():
    meta = dict(spriteSheetUrl='/a/run.png', frameCount=13, fps=12,
                frameWidth=481, frameHeight=655, footAnchor={'x': 240, 'y': 655})
    ready = CharacterReady(status='ready', characterId='char_x',
                           animations={'run': meta, 'jump': {**meta, 'spriteSheetUrl': '/a/jump.png'}})
    assert set(ready.animations) == {'run', 'jump'}
    with pytest.raises(ValidationError):
        CharacterReady(status='ready', characterId='char_x', animations={'run': meta})


def test_needs_correction_requires_16_joints():
    joints = [{'name': f'j{i}', 'loc': [i, i], 'parent': None} for i in range(16)]
    nc = NeedsCorrection(status='needs_correction', reason='NO_HUMANOID',
                         maskUrl='/artifacts/char_x/anno/mask.png', joints=joints)
    assert len(nc.joints) == 16
    with pytest.raises(ValidationError):
        NeedsCorrection(status='needs_correction', reason='NO_HUMANOID',
                        maskUrl='/m.png', joints=joints[:15])


def test_job_accepted_and_failed_shapes():
    assert JobAccepted(jobId='char_x').model_dump() == {'jobId': 'char_x'}
    assert JobFailed(status='failed', code='RENDER_TIMEOUT').model_dump() == {'status': 'failed', 'code': 'RENDER_TIMEOUT'}
```

- [ ] **步骤 3：运行测试验证失败**

运行：`python -m pytest tests/test_contracts.py -v`
预期：FAIL（`ModuleNotFoundError: No module named 'app.contracts'`）

- [ ] **步骤 4：实现 `server/app/contracts.py`**

```python
"""前后端共享契约（P0 任务 1 要求）：Pydantic 模型 + 错误码。Unity GameContracts.cs 将来照此镜像。"""
import hashlib
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, field_validator

JobState = Literal['queued', 'processing', 'needs_correction', 'ready', 'failed']
ErrorCode = Literal['FILE_TOO_LARGE', 'NOT_AN_IMAGE', 'UNSUPPORTED_FORMAT', 'JOB_NOT_FOUND',
                    'QUEUE_UNAVAILABLE', 'RENDER_TIMEOUT', 'RENDER_CRASHED', 'ASSET_MISSING', 'INTERNAL']
CorrectionReason = Literal['NO_HUMANOID', 'NO_SKELETON', 'MULTIPLE_SKELETONS', 'NO_CONTOUR', 'ANALYZE_FAILED']
NON_TERMINAL_STATES = ('queued', 'processing')
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def derive_job_id(content: bytes) -> str:
    return 'char_' + hashlib.sha256(content).hexdigest()[:12]


class FootAnchor(BaseModel):
    x: int
    y: int


class AnimationMeta(BaseModel):
    spriteSheetUrl: str
    frameCount: int
    fps: int
    frameWidth: int
    frameHeight: int
    footAnchor: FootAnchor


class CharacterReady(BaseModel):
    status: Literal['ready'] = 'ready'
    characterId: str
    animations: Dict[str, AnimationMeta]

    @field_validator('animations')
    @classmethod
    def _both_motions(cls, v):
        if set(v) != {'run', 'jump'}:
            raise ValueError("animations must contain exactly 'run' and 'jump'")
        return v


class Joint(BaseModel):
    name: str
    loc: List[int]
    parent: Optional[str] = None


class NeedsCorrection(BaseModel):
    status: Literal['needs_correction'] = 'needs_correction'
    reason: CorrectionReason
    maskUrl: str
    joints: List[Joint]

    @field_validator('joints')
    @classmethod
    def _sixteen_joints(cls, v):
        if len(v) != 16:
            raise ValueError(f'joints must have exactly 16 items, got {len(v)}')
        return v


class JobFailed(BaseModel):
    status: Literal['failed'] = 'failed'
    code: ErrorCode


class JobAccepted(BaseModel):
    jobId: str


class ErrorBody(BaseModel):
    code: ErrorCode
```

注意：与 `app.services.annotations.NeedsCorrection`（异常类）重名——契约模型在 API 层 import 时用 `from app import contracts` 前缀访问（`contracts.NeedsCorrection`），避免同名混淆；这是有意为之的两个不同层对象。

- [ ] **步骤 5：运行测试验证通过**

运行：`python -m pytest tests/test_contracts.py -v`
预期：6 个用例全部 PASS

- [ ] **步骤 6：Commit**

```bash
git add server/requirements-dev.txt server/app/contracts.py server/tests/test_contracts.py
git commit -m "feat: define async service pydantic contracts"
```

### 任务 4：任务状态存储 job_store.py（fakeredis TDD）

**交付物：** Redis 队列/状态封装 + `result.json` 兜底读取。

**文件：**
- 创建：`server/app/services/job_store.py`
- 测试：`server/tests/test_job_store.py`

- [ ] **步骤 1：编写失败测试 `server/tests/test_job_store.py`**

```python
import json

import fakeredis
import pytest

from app.services.job_store import QUEUE_KEY, JobStore


@pytest.fixture
def store(tmp_path):
    return JobStore('redis://unused', tmp_path, client=fakeredis.FakeStrictRedis(decode_responses=True))


def test_create_sets_queued_with_ttl(store):
    store.create('char_a')
    data = store.get('char_a')
    assert data['status'] == 'queued'
    assert 'updatedAt' in data
    assert store.r.ttl('job:char_a') > 0


def test_enqueue_dequeue_roundtrip(store):
    store.enqueue('char_a')
    store.enqueue('char_b')
    assert store.r.llen(QUEUE_KEY) == 2
    assert store.dequeue(timeout=1) == 'char_a'   # FIFO：LPUSH + BRPOP
    assert store.dequeue(timeout=1) == 'char_b'
    assert store.dequeue(timeout=1) is None


def test_set_status_stores_result_json(store):
    store.create('char_a')
    payload = {'status': 'ready', 'characterId': 'char_a', 'animations': {}}
    store.set_status('char_a', 'ready', result=payload)
    data = store.get('char_a')
    assert data['status'] == 'ready'
    assert json.loads(data['result']) == payload


def test_get_falls_back_to_result_json_snapshot(store, tmp_path):
    # Redis 无记录（TTL 过期模拟），但 runner 落盘的 result.json 存在
    job_dir = tmp_path / 'char_snap'
    job_dir.mkdir()
    payload = {'status': 'needs_correction', 'reason': 'NO_HUMANOID', 'maskUrl': '/m.png', 'joints': []}
    (job_dir / 'result.json').write_text(json.dumps(payload))
    data = store.get('char_snap')
    assert data['status'] == 'needs_correction'
    assert json.loads(data['result']) == payload


def test_get_unknown_returns_none(store):
    assert store.get('char_missing') is None
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_job_store.py -v`
预期：FAIL（`No module named 'app.services.job_store'`）

- [ ] **步骤 3：实现 `server/app/services/job_store.py`**

```python
"""任务状态存储：Redis Hash（TTL 24h）+ 队列 List；result.json 为 Redis 过期后的兜底。"""
import json
import time
from pathlib import Path
from typing import Optional

import redis

QUEUE_KEY = 'pq:characters'
JOB_KEY = 'job:{}'
TTL_SECONDS = 24 * 3600


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())


class JobStore:
    def __init__(self, redis_url: str, jobs_root, client=None):
        self.r = client if client is not None else redis.Redis.from_url(redis_url, decode_responses=True)
        self.jobs_root = Path(jobs_root)

    # -- 队列 --
    def enqueue(self, job_id: str) -> None:
        self.r.lpush(QUEUE_KEY, job_id)

    def dequeue(self, timeout: int = 5) -> Optional[str]:
        item = self.r.brpop(QUEUE_KEY, timeout=timeout)
        return item[1] if item else None

    # -- 状态 --
    def create(self, job_id: str) -> None:
        key = JOB_KEY.format(job_id)
        self.r.hset(key, mapping={'status': 'queued', 'updatedAt': _now()})
        self.r.expire(key, TTL_SECONDS)

    def set_status(self, job_id: str, status: str, result: Optional[dict] = None) -> None:
        key = JOB_KEY.format(job_id)
        mapping = {'status': status, 'updatedAt': _now()}
        if result is not None:
            mapping['result'] = json.dumps(result, ensure_ascii=False)
        self.r.hset(key, mapping=mapping)
        self.r.expire(key, TTL_SECONDS)

    def get(self, job_id: str) -> Optional[dict]:
        data = self.r.hgetall(JOB_KEY.format(job_id))
        if data:
            return data
        snapshot = self.jobs_root / job_id / 'result.json'
        if snapshot.exists():
            payload = json.loads(snapshot.read_text())
            return {'status': payload['status'], 'result': json.dumps(payload, ensure_ascii=False)}
        return None
```

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/test_job_store.py -v`
预期：5 个用例全部 PASS

- [ ] **步骤 5：Commit**

```bash
git add server/app/services/job_store.py server/tests/test_job_store.py
git commit -m "feat: add redis job store with snapshot fallback"
```

### 任务 5：FastAPI 应用（main.py + api/characters.py，TDD）

**交付物：** `POST /v1/characters`、`GET /v1/characters/{jobId}`、`GET /healthz`、`/artifacts` 静态伺服；上传校验、幂等、五态查询。

**文件：**
- 创建：`server/app/main.py`
- 创建：`server/app/api/__init__.py`（空文件）
- 创建：`server/app/api/characters.py`
- 测试：`server/tests/test_characters_api.py`

- [ ] **步骤 1：编写失败测试 `server/tests/test_characters_api.py`**

```python
import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.contracts import derive_job_id
from app.services.job_store import JobStore

PNG_1PX = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4'
    '890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('OUT_ROOT', str(tmp_path))
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    from app.main import create_app
    app = create_app()
    app.state.store = JobStore('redis://unused', tmp_path / 'jobs',
                               client=fakeredis.FakeStrictRedis(decode_responses=True))
    return TestClient(app)


def _upload(client, content: bytes, filename='c.png', content_type='image/png'):
    return client.post('/v1/characters',
                       files={'file': (filename, content, content_type)})


def test_healthz(client):
    assert client.get('/healthz').json() == {'status': 'ok'}


def test_upload_valid_returns_202_and_enqueues(client, tmp_path):
    resp = _upload(client, PNG_1PX)
    assert resp.status_code == 202
    job_id = resp.json()['jobId']
    assert job_id == derive_job_id(PNG_1PX)
    assert (tmp_path / 'jobs' / job_id / 'input.png').read_bytes() == PNG_1PX
    assert client.app.state.store.r.llen('pq:characters') == 1
    status = client.get(f'/v1/characters/{job_id}').json()
    assert status['status'] == 'queued'


def test_upload_idempotent_no_double_enqueue(client):
    first = _upload(client, PNG_1PX).json()['jobId']
    second = _upload(client, PNG_1PX).json()['jobId']
    assert first == second
    assert client.app.state.store.r.llen('pq:characters') == 1


def test_upload_too_large(client):
    resp = _upload(client, b'\x89PNG' + b'x' * (10 * 1024 * 1024))
    assert resp.status_code == 400
    assert resp.json()['code'] == 'FILE_TOO_LARGE'


def test_upload_not_an_image(client):
    resp = _upload(client, b'hello world')
    assert resp.status_code == 400
    assert resp.json()['code'] == 'NOT_AN_IMAGE'


def test_upload_unsupported_format_gif(client):
    resp = _upload(client, b'GIF89a....')
    assert resp.status_code == 400
    assert resp.json()['code'] == 'UNSUPPORTED_FORMAT'


def test_get_unknown_job_404(client):
    resp = client.get('/v1/characters/char_nope')
    assert resp.status_code == 404
    assert resp.json()['code'] == 'JOB_NOT_FOUND'


def test_get_ready_returns_full_contract(client, tmp_path):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    store = client.app.state.store
    payload = {'status': 'ready', 'characterId': job_id, 'animations': {
        'run': {'spriteSheetUrl': f'/artifacts/{job_id}/run.png', 'frameCount': 13, 'fps': 12,
                'frameWidth': 481, 'frameHeight': 655, 'footAnchor': {'x': 240, 'y': 655}},
        'jump': {'spriteSheetUrl': f'/artifacts/{job_id}/jump.png', 'frameCount': 12, 'fps': 12,
                 'frameWidth': 481, 'frameHeight': 655, 'footAnchor': {'x': 240, 'y': 655}}}}
    store.set_status(job_id, 'ready', result=payload)
    resp = client.get(f'/v1/characters/{job_id}')
    assert resp.status_code == 200
    assert resp.json() == payload


def test_get_needs_correction_shape(client):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    joints = [{'name': f'j{i}', 'loc': [i, i], 'parent': None} for i in range(16)]
    payload = {'status': 'needs_correction', 'reason': 'NO_HUMANOID',
               'maskUrl': f'/artifacts/{job_id}/anno/mask.png', 'joints': joints}
    client.app.state.store.set_status(job_id, 'needs_correction', result=payload)
    body = client.get(f'/v1/characters/{job_id}').json()
    assert body['reason'] == 'NO_HUMANOID'
    assert len(body['joints']) == 16


def test_artifacts_static_serving(client, tmp_path):
    job_id = _upload(client, PNG_1PX).json()['jobId']
    sheet = tmp_path / 'jobs' / job_id / 'run.png'
    sheet.write_bytes(PNG_1PX)
    resp = client.get(f'/artifacts/{job_id}/run.png')
    assert resp.status_code == 200
    assert resp.content == PNG_1PX
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_characters_api.py -v`
预期：FAIL（`No module named 'app.main'`）

- [ ] **步骤 3：实现 `server/app/main.py`**

```python
"""FastAPI 应用工厂：路由 + /artifacts 静态伺服 + /healthz。"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.characters import router
from app.services.job_store import JobStore

SERVER_DIR = Path(__file__).resolve().parents[1]


def create_app() -> FastAPI:
    app = FastAPI(title='paper-game server', version='v1')
    out_root = Path(os.environ.get('OUT_ROOT', str(SERVER_DIR / 'out'))).resolve()
    jobs_root = out_root / 'jobs'
    jobs_root.mkdir(parents=True, exist_ok=True)
    app.state.out_root = out_root
    app.state.store = JobStore(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), jobs_root)
    app.mount('/artifacts', StaticFiles(directory=str(jobs_root)), name='artifacts')
    app.include_router(router)

    @app.get('/healthz')
    def healthz():
        return {'status': 'ok'}

    return app


app = create_app()
```

- [ ] **步骤 4：实现 `server/app/api/characters.py`**

```python
"""角色任务接口：POST 上传入队（幂等），GET 轮询五态。"""
import json
from typing import Optional

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from app import contracts
from app.services.job_store import JobStore

router = APIRouter(prefix='/v1/characters', tags=['characters'])

PNG_MAGIC = b'\x89PNG'
JPEG_MAGIC = b'\xff\xd8'
GIF_MAGIC = b'GIF8'
WEBP_MAGIC = b'RIFF'


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=contracts.ErrorBody(code=code).model_dump())


def _sniff(head: bytes) -> Optional[str]:
    """返回 'ok' | 'unsupported' | 'not_image'。仅接受 PNG/JPEG（P0 锁定）。注意：Python 3.9 不支持 `str | None` 运行时注解，必须用 Optional。"""
    if head.startswith(PNG_MAGIC) or head.startswith(JPEG_MAGIC):
        return 'ok'
    if head.startswith(GIF_MAGIC) or head.startswith(WEBP_MAGIC):
        return 'unsupported'
    return 'not_image'


@router.post('', status_code=202)
async def create_character(request: Request, file: UploadFile = File(...)):
    store: JobStore = request.app.state.store
    content = await file.read(contracts.MAX_UPLOAD_BYTES + 1)
    if len(content) > contracts.MAX_UPLOAD_BYTES:
        return _error(400, 'FILE_TOO_LARGE')
    verdict = _sniff(content[:4])
    if verdict == 'unsupported':
        return _error(400, 'UNSUPPORTED_FORMAT')
    if verdict == 'not_image':
        return _error(400, 'NOT_AN_IMAGE')

    job_id = contracts.derive_job_id(content)
    existing = store.get(job_id)
    if existing is not None:
        # 非终态：不重复入队；终态：客户端轮询即可看到结果
        return contracts.JobAccepted(jobId=job_id).model_dump()

    job_dir = store.jobs_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / 'input.png').write_bytes(content)
    try:
        store.create(job_id)
        store.enqueue(job_id)
    except Exception as e:  # redis.ConnectionError 等：入队前探测失败
        if 'redis' in type(e).__module__:
            return _error(503, 'QUEUE_UNAVAILABLE')
        raise
    return contracts.JobAccepted(jobId=job_id).model_dump()


@router.get('/{job_id}')
def get_character(job_id: str, request: Request):
    store: JobStore = request.app.state.store
    data = store.get(job_id)
    if data is None:
        return _error(404, 'JOB_NOT_FOUND')
    if 'result' in data:
        return json.loads(data['result'])
    return {'status': data['status'], 'updatedAt': data.get('updatedAt')}
```

- [ ] **步骤 5：运行测试验证通过 + 回归**

运行：`python -m pytest tests/test_characters_api.py tests/test_contracts.py tests/test_job_store.py -v`
预期：全部 PASS

- [ ] **步骤 6：Commit**

```bash
git add server/app/main.py server/app/api server/tests/test_characters_api.py
git commit -m "feat: expose character job api with idempotent upload"
```

### 任务 6：worker（render_runner 子进程入口 + character_worker 主循环，TDD）

**交付物：** 子进程执行渲染并落 `result.json`；主循环 BRPOP + 120s 超时 + 基础设施故障重试 1 次 + 稳定错误码。

**文件：**
- 创建：`server/app/workers/__init__.py`（空文件）
- 创建：`server/app/workers/render_runner.py`
- 创建：`server/app/workers/character_worker.py`
- 测试：`server/tests/test_character_worker.py`

- [ ] **步骤 1：编写失败测试 `server/tests/test_character_worker.py`**（主循环用可注入 runner 与内存 store 测纯逻辑；runner 的产物搬运用真实 tmp 目录结构测）

```python
import json

import pytest

from app.workers.character_worker import process_job
from app.workers.render_runner import relocate_artifacts


class FakeStore:
    def __init__(self, tmp_path):
        self.jobs_root = tmp_path
        self.calls = []

    def set_status(self, job_id, status, result=None):
        self.calls.append((job_id, status, result))


def _job_dir(tmp_path, job_id='char_t'):
    d = tmp_path / job_id
    d.mkdir()
    return d


def _write_result(job_dir, payload):
    (job_dir / 'result.json').write_text(json.dumps(payload))


def test_done_ready_sets_status_from_result(tmp_path):
    job_dir = _job_dir(tmp_path)
    payload = {'status': 'ready', 'characterId': 'char_t', 'animations': {'run': {}, 'jump': {}}}
    _write_result(job_dir, payload)
    store = FakeStore(tmp_path)
    status = process_job(store, 'char_t', runner=lambda d, t: 'done', timeout=5)
    assert status == 'ready'
    assert store.calls[-1] == ('char_t', 'ready', payload)


def test_crash_retries_once_then_render_crashed(tmp_path):
    job_dir = _job_dir(tmp_path)
    store = FakeStore(tmp_path)
    attempts = []

    def runner(d, t):
        attempts.append(1)
        return 'crash'

    status = process_job(store, 'char_t', runner=runner, timeout=5)
    assert status == 'failed'
    assert len(attempts) == 2
    assert store.calls[-1][2] == {'status': 'failed', 'code': 'RENDER_CRASHED'}


def test_timeout_then_success_no_failed_state(tmp_path):
    job_dir = _job_dir(tmp_path)
    payload = {'status': 'ready', 'characterId': 'char_t', 'animations': {}}
    _write_result(job_dir, payload)   # 第二次尝试前 result 已落盘
    outcomes = iter(['timeout', 'done'])
    store = FakeStore(tmp_path)
    status = process_job(store, 'char_t', runner=lambda d, t: next(outcomes), timeout=5)
    assert status == 'ready'
    assert all(c[1] != 'failed' for c in store.calls)   # 重试成功后不应出现 failed 终态


def test_needs_correction_is_business_terminal_no_retry(tmp_path):
    job_dir = _job_dir(tmp_path)
    payload = {'status': 'needs_correction', 'reason': 'NO_HUMANOID', 'maskUrl': '/m.png', 'joints': []}
    _write_result(job_dir, payload)
    store = FakeStore(tmp_path)
    attempts = []

    def runner(d, t):
        attempts.append(1)
        return 'done'

    status = process_job(store, 'char_t', runner=runner, timeout=5)
    assert status == 'needs_correction'
    assert len(attempts) == 1   # 业务失败不重试


def test_relocate_artifacts_moves_pngs_and_anno(tmp_path):
    # 模拟 render_character 的产物结构：work/input/{run,jump}/x.png + work/input/anno
    job_dir = _job_dir(tmp_path)
    work = job_dir / 'work' / 'input'
    for m in ('run', 'jump'):
        (work / m).mkdir(parents=True)
        (work / m / f'{m}.png').write_bytes(b'png')
    (work / 'anno').mkdir(parents=True)
    (work / 'anno' / 'mask.png').write_bytes(b'mask')
    relocate_artifacts(job_dir)
    assert (job_dir / 'run.png').read_bytes() == b'png'
    assert (job_dir / 'jump.png').read_bytes() == b'png'
    assert (job_dir / 'anno' / 'mask.png').read_bytes() == b'mask'
    assert not (job_dir / 'work').exists()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_character_worker.py -v`
预期：FAIL（`No module named 'app.workers'`）

- [ ] **步骤 3：实现 `server/app/workers/render_runner.py`**

```python
"""子进程入口：执行 render_character，产物搬运到契约布局，写 result.json。

退出码协议：0 = 业务终态已写入 result.json（ready/needs_correction/failed:ASSET_MISSING）；
非 0 = 基础设施故障（由主循环重试）。chdir 污染与 GLFW 崩溃被隔离在本子进程内。
"""
import json
import shutil
import sys
import traceback
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[2]
ASSETS_DIR = SERVER_DIR / 'app' / 'assets' / 'motions'
MOTIONS = ('run', 'jump')


def relocate_artifacts(job_dir: Path) -> None:
    """render_character 产物在 work/input/** 下，搬运到 job_dir 根的契约布局。"""
    work_char = job_dir / 'work' / 'input'
    for m in MOTIONS:
        src = work_char / m / f'{m}.png'
        if src.exists():
            shutil.move(str(src), str(job_dir / f'{m}.png'))
    anno_src = work_char / 'anno'
    if anno_src.exists():
        anno_dst = job_dir / 'anno'
        if anno_dst.exists():
            shutil.rmtree(anno_dst)
        shutil.move(str(anno_src), str(anno_dst))
    shutil.rmtree(job_dir / 'work', ignore_errors=True)


def _load_joints(char_cfg: Path):
    import yaml
    if not char_cfg.exists():
        return []
    cfg = yaml.safe_load(char_cfg.read_text())
    return [{'name': j['name'], 'loc': [int(x) for x in j['loc']], 'parent': j['parent']}
            for j in cfg['skeleton']]


def run(job_dir: Path) -> int:
    job_id = job_dir.name
    from app.services.annotations import NeedsCorrection
    from app.services.character_pipeline import CharacterPipeline

    pipeline = CharacterPipeline(ASSETS_DIR, job_dir / 'work')
    try:
        result = pipeline.render_character(job_dir / 'input.png')
    except NeedsCorrection as e:
        relocate_artifacts(job_dir)
        payload = {'status': 'needs_correction', 'reason': e.reason,
                   'maskUrl': f'/artifacts/{job_id}/anno/mask.png',
                   'joints': _load_joints(job_dir / 'anno' / 'char_cfg.yaml')}
    except FileNotFoundError as e:
        payload = {'status': 'failed', 'code': 'ASSET_MISSING'}
    except Exception:
        traceback.print_exc()
        return 2
    else:
        relocate_artifacts(job_dir)
        animations = {m: {**result['animations'][m],
                          'spriteSheetUrl': f'/artifacts/{job_id}/{m}.png'} for m in MOTIONS}
        payload = {'status': 'ready', 'characterId': job_id, 'animations': animations}

    (job_dir / 'result.json').write_text(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(run(Path(sys.argv[1]).resolve()))
```

- [ ] **步骤 4：实现 `server/app/workers/character_worker.py`**

```python
"""worker 主循环：BRPOP 消费 -> 子进程渲染 -> 终态写回。并发 = 1（规格锁定）。"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from app.services.job_store import JobStore

SERVER_DIR = Path(__file__).resolve().parents[2]
SUBPROCESS_TIMEOUT = 120   # 尖刺实测单任务 <=40s 的 3 倍余量
MAX_ATTEMPTS = 2           # 基础设施故障重试 1 次；业务失败不重试


def _run_subprocess(job_dir: Path, timeout: int) -> str:
    """返回 'done' | 'crash' | 'timeout'。"""
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'app.workers.render_runner', str(job_dir)],
            timeout=timeout, cwd=str(SERVER_DIR))
    except subprocess.TimeoutExpired:
        return 'timeout'
    if proc.returncode == 0 and (job_dir / 'result.json').exists():
        return 'done'
    return 'crash'


def process_job(store, job_id: str, runner: Callable = None, timeout: int = SUBPROCESS_TIMEOUT) -> str:
    runner = runner or _run_subprocess
    store.set_status(job_id, 'processing')
    job_dir = store.jobs_root / job_id
    outcome = 'crash'
    for _ in range(MAX_ATTEMPTS):
        outcome = runner(job_dir, timeout)
        if outcome == 'done':
            payload = json.loads((job_dir / 'result.json').read_text())
            store.set_status(job_id, payload['status'], result=payload)
            return payload['status']
    code = 'RENDER_TIMEOUT' if outcome == 'timeout' else 'RENDER_CRASHED'
    payload = {'status': 'failed', 'code': code}
    store.set_status(job_id, 'failed', result=payload)
    return 'failed'


def main() -> None:
    out_root = Path(os.environ.get('OUT_ROOT', str(SERVER_DIR / 'out'))).resolve()
    store = JobStore(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), out_root / 'jobs')
    print(f'worker listening on {store.r.connection_pool.connection_kwargs}', flush=True)
    while True:
        job_id = store.dequeue(timeout=5)
        if job_id is None:
            continue
        status = process_job(store, job_id)
        print(f'job {job_id} -> {status}', flush=True)


if __name__ == '__main__':
    main()
```

- [ ] **步骤 5：运行测试验证通过 + 回归**

运行：`python -m pytest tests/test_character_worker.py -v && python -m pytest tests/ -v --ignore=tests/test_spike_batch.py`
预期：新用例全 PASS；既有测试（sprite_sheet 3 + annotations 2 + contract 3 + mesa_env 4 + contracts 6 + job_store 5 + api 10）全绿，无回归。

- [ ] **步骤 6：宿主直跑 runner 冒烟一次（真实渲染，TorchServe 需在线）**

```bash
mkdir -p out/jobs/char_manualtest && cp ../testdata/characters/garlic.png out/jobs/char_manualtest/input.png
python -m app.workers.render_runner out/jobs/char_manualtest
cat out/jobs/char_manualtest/result.json | python -m json.tool | head -20
```
预期：退出码 0；result.json 含 `status=ready`、run/jump 双动画六键、`spriteSheetUrl=/artifacts/char_manualtest/run.png`。

- [ ] **步骤 7：Commit**

```bash
git add server/app/workers server/tests/test_character_worker.py
git commit -m "feat: add character worker with subprocess isolation and retry"
```

### 任务 7：compose 扩展（redis + api + worker）与全栈启动验证

**交付物：** `docker compose up -d --build` 拉起 4 容器全栈；api /healthz 绿；worker 日志显示监听。

**文件：**
- 修改：`server/docker-compose.yml`

- [ ] **步骤 1：改写 `server/docker-compose.yml`**（保留 torchserve 段原样，头部注释更新为全栈说明；新增锚点复用镜像）

在 `services:` 下新增（torchserve 段不动）：

```yaml
  redis:
    image: redis:7-alpine
    container_name: pg-redis
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  api:
    build:
      context: .
      dockerfile: Dockerfile
    image: paper-game/server:local
    container_name: pg-api
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    environment:
      REDIS_URL: redis://redis:6379/0
      OUT_ROOT: /data/out
    ports:
      - "8000:8000"
    volumes:
      - ./out:/data/out
    depends_on:
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/healthz"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 15s
    restart: unless-stopped

  worker:
    image: paper-game/server:local
    container_name: pg-worker
    command: ["python", "-m", "app.workers.character_worker"]
    environment:
      REDIS_URL: redis://redis:6379/0
      OUT_ROOT: /data/out
      RENDER_USE_MESA: "true"
      TORCHSERVE_UPSTREAM: torchserve:8080
    volumes:
      - ./out:/data/out
    depends_on:
      torchserve:
        condition: service_healthy
      redis:
        condition: service_healthy
      api:
        condition: service_started   # 确保镜像已构建（api 与 worker 同镜像）
    restart: unless-stopped

# GPU 扩展位：未来 Linux+NVIDIA 部署时创建 docker-compose.override.yml，
# 为 torchserve/worker 添加 gpu 资源声明（Mac 开发阶段用 Mesa 软渲染，无需 GPU）。
```

同时更新文件头部注释：启动命令改为 `bash scripts/setup-vendor.sh && docker compose up -d --build`，探活增加 `curl http://localhost:8000/healthz`。

- [ ] **步骤 2：语法与配置校验**

运行：`docker compose config -q`
预期：无输出（配置合法）

- [ ] **步骤 3：全栈启动验证**

```bash
docker compose up -d --build
sleep 30
curl -sf http://localhost:8000/healthz          # {"status":"ok"}
curl -sf http://localhost:8080/ping             # {"status": "Healthy"}
docker logs pg-worker 2>&1 | tail -3            # "worker listening on ..."
docker compose ps                               # 4 容器 Up（torchserve/api healthy）
```

预期：全部通过。worker 日志无异常栈。

- [ ] **步骤 4：Commit**

```bash
git add server/docker-compose.yml
git commit -m "feat: orchestrate redis api worker in compose"
```

### 任务 8：端到端冒烟脚本 + 全量回归 + 结论记录

**交付物：** `scripts/smoke_e2e.sh` 全栈冒烟 PASS；全部测试绿；结果记录入档。

**文件：**
- 创建：`server/scripts/smoke_e2e.sh`
- 创建：`docs/async-service-smoke-results.md`

- [ ] **步骤 1：编写 `server/scripts/smoke_e2e.sh`**（脚本分区：真实 HTTP 全链路）

```bash
#!/usr/bin/env bash
# 全栈端到端冒烟：上传 -> 轮询 -> ready -> 下载精灵表验证 PNG。
# 前置：docker compose up -d --build 已完成且 4 容器健康。
set -euo pipefail
cd "$(dirname "$0")/.."

API="${API_BASE:-http://localhost:8000}"
SAMPLE="${1:-../testdata/characters/garlic.png}"
TIMEOUT_SEC=180

echo "== 健康检查 =="
curl -sf "$API/healthz" > /dev/null
curl -sf http://localhost:8080/ping > /dev/null

echo "== 上传 $SAMPLE =="
JOB_ID=$(curl -sf -F "file=@$SAMPLE" "$API/v1/characters" | python3 -c 'import sys,json; print(json.load(sys.stdin)["jobId"])')
echo "jobId=$JOB_ID"

echo "== 轮询（最长 ${TIMEOUT_SEC}s）=="
START=$(date +%s)
while true; do
  BODY=$(curl -sf "$API/v1/characters/$JOB_ID")
  STATUS=$(echo "$BODY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  ELAPSED=$(( $(date +%s) - START ))
  echo "  [${ELAPSED}s] status=$STATUS"
  case "$STATUS" in
    ready) break ;;
    needs_correction|failed) echo "FAIL: 终态异常 $BODY"; exit 1 ;;
  esac
  [ "$ELAPSED" -gt "$TIMEOUT_SEC" ] && { echo "FAIL: 超时"; exit 1; }
  sleep 5
done

echo "== 下载精灵表并验证 PNG =="
for M in run jump; do
  URL=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['animations']['$M']['spriteSheetUrl'])")
  curl -sf "$API$URL" -o "/tmp/smoke_$M.png"
  python3 -c "
from PIL import Image
im = Image.open('/tmp/smoke_$M.png')
assert im.mode == 'RGBA', im.mode
print('$M sheet:', im.size, im.mode)
"
done

echo "== 幂等验证：同图重复提交 =="
JOB_ID2=$(curl -sf -F "file=@$SAMPLE" "$API/v1/characters" | python3 -c 'import sys,json; print(json.load(sys.stdin)["jobId"])')
[ "$JOB_ID" = "$JOB_ID2" ] || { echo "FAIL: 幂等被破坏"; exit 1; }

echo "SMOKE_E2E_PASS jobId=$JOB_ID elapsed=${ELAPSED}s"
```

- [ ] **步骤 2：执行冒烟**

运行：`chmod +x scripts/smoke_e2e.sh && bash scripts/smoke_e2e.sh`
预期：末行打印 `SMOKE_E2E_PASS`，全程 ≤180s（参考尖刺单张 17-40s + 排队开销）。

- [ ] **步骤 3：宿主全量回归**

运行：`source .venv/bin/activate && python -m pytest tests/ -v --ignore=tests/test_spike_batch.py`
预期：全部 PASS（既有 8 个管线测试 + 本计划新增约 28 个用例）。
说明：`test_spike_batch.py` 为尖刺验收专用（宿主直调管线），不属于服务层回归，单独跑不阻塞。

- [ ] **步骤 4：编写 `docs/async-service-smoke-results.md`**

内容固定四节：① 冒烟结果（jobId、逐状态轮询时间线、精灵表尺寸）；② 回归结果（用例数、通过数）；③ Mesa 尖刺结论（GO / NO-GO 及回退分支是否启用）；④ 残留问题与下一步（对接 P0 任务 6 Unity 客户端联调、演示阶段对象存储升级）。

- [ ] **步骤 5：Commit**

```bash
git add server/scripts/smoke_e2e.sh docs/async-service-smoke-results.md
git commit -m "test: verify async character service end to end"
```

---

## 回退分支（仅任务 1 NO-GO 时启用）

worker 以宿主进程运行：compose 只含 torchserve+redis+api；`scripts/run_worker_host.sh` 拉起 worker（宿主 .venv，`RENDER_USE_MESA` 不设置即走 GLFW 本地渲染）；`OUT_ROOT` 宿主与容器统一指向 `server/out`（api 容器挂载 `./out`）。任务 2-6、8 的代码与测试完全不变，仅部署形态变化；冒烟脚本照常工作。

## 已知风险与对策

- **容器内 OSMesa 渲染失败**：任务 1 即为 go/no-go 尖刺，失败按回退分支执行，不阻塞后续任务开发（任务 2-6 全部宿主可测）。
- **vendor 依赖安装缓慢/超时**：镜像构建走 Docker Desktop 已配置的代理（host.docker.internal:7890）；pip 慢时加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。
- **GLFW/OpenGL 偶发全挂**（尖刺实测）：子进程隔离 + 重试 1 次已覆盖；若容器内 Mesa 复现同类崩溃，同样由重试兜底。
- **fakeredis 与真实 Redis 行为差异**（BRPOP 阻塞语义）：worker 主循环测试用注入 stub runner，不依赖 fakeredis 阻塞行为；真实阻塞路径由任务 8 冒烟覆盖。
