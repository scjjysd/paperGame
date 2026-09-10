# 关卡解析服务端实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 落地 Level API v1 的 P0+P1+P2 服务端能力，把手绘关卡照片异步转换为严格对齐的拉正背景、权威关卡 JSON、识别诊断和可玩性结论。

**架构：** 在现有 FastAPI、Redis List/Hash 和独立 worker/runner 模式旁新增关卡任务链路；OpenCV 是几何唯一真源，LLM 仅复核已有候选并可无损降级。`level_parser.parse()` 依次编排拉正、候选检测、语义复核、出生点与几何校验、可玩性分析，并以 `result.json` 原子发布终态。

**技术栈：** Python 3.9、FastAPI、Pydantic v2、Redis/fakeredis、Pillow 10.1.0、OpenCV 4.6.0.66、NumPy 1.24.4、requests 2.31.0、pytest。

---

## 范围与约束

- 协议唯一真源：`level-image-to-json-api.md`；设计裁定：`docs/superpowers/specs/2026-09-08-level-parser-design.md`。
- 本计划只做 P0+P1+P2；不做鉴权、限流、对象存储、隐私删除、长期历史归档和 Unity 客户端。
- 不修改 `vendor/`，不升级或新增依赖；`app/` 不得 import `scripts/`；跨进程路径全部绝对化。
- 所有最终几何只来自 OpenCV 像素证据；LLM 不能创建、移动或延长坐标；可玩性只能诊断，不能改几何。
- Python 3.9 注解使用 `Optional[T]`、`Tuple[...]`，不使用 `T | None`。
- 每个任务严格红—绿—重构；测试命令均在 `server/` 下运行，并使用 `python -m pytest`。
- 计划中的 commit 是执行阶段建议；只有用户明确授权提交时才执行。

## 文件结构

### 修改

- `server/app/services/job_store.py`：参数化队列、终态集合、重跑产物，并支持进度字段，角色默认行为不变。
- `server/app/main.py`：创建独立 `level_store` 并挂载 `/v1/levels` 路由。
- `server/docker-compose.yml`：增加独立 `level-worker` 服务及可选 LLM 环境变量。
- `README.md`、`CLAUDE.md`：更新关卡解析进度、运行和验证命令。
- `.ai/workflow-records/feat-level-parser.yml`：登记计划路径和最终验证结果。

### 创建

- `server/app/level_contracts.py`：Level v1 Pydantic 契约、能力参数、错误信封、幂等 ID。
- `server/app/api/levels.py`：上传校验、EXIF 归一化、幂等入队和状态查询。
- `server/app/services/level_rectify.py`：纸张候选、四角排序、透视拉正和方向判定。
- `server/app/services/level_detect.py`：墨迹、平台线、旗帜候选与端点精修。
- `server/app/services/level_semantic.py`：OpenAI 兼容候选复核、Schema 校验和降级。
- `server/app/services/playability.py`：出生/终点承载定位、有向可达图和六类警告。
- `server/app/services/level_parser.py`：流水线门面、几何校验、产物与终态信封。
- `server/app/workers/level_worker.py`、`server/app/workers/level_runner.py`：独立消费、子进程隔离、重试与原子发布。
- `server/scripts/gen_level_samples.py`：固定 seed 合成样本和构造真值。
- `server/scripts/smoke_levels_e2e.sh`：全栈上传、轮询、产物及幂等冒烟。
- `server/tests/test_level_contracts.py`：契约、跨字段验证、幂等与固定终态样例测试。
- `server/tests/test_levels_api.py`：上传边界、状态查询、幂等和 force 测试。
- `server/tests/test_level_rectify.py`：合成透视、纸张失败和方向歧义测试。
- `server/tests/test_level_detect.py`：平台、碎线、平行线、旗帜和黄金真值测试。
- `server/tests/test_level_semantic.py`：LLM mock、坏响应和降级测试。
- `server/tests/test_playability.py`：可达性与六种警告测试。
- `server/tests/test_level_parser.py`：门面三种业务终态、产物和几何不变性测试。
- `server/tests/test_level_worker.py`、`server/tests/test_level_runner.py`：退出码、重试、终态发布和进度测试。
- `testdata/levels/contracts/*.json`：四种合法终态及两种非法 level 固定样例。
- `testdata/levels/synthetic/*`：固定 seed 合成照片、拉正图和真值。

## 共享接口（后续任务必须保持一致）

```python
# app/level_contracts.py
SCHEMA_VERSION = '1.0'
ALGORITHM_VERSION = 'level-parser-1.0.0'
ALGORITHM_MAJOR_VERSION = '1'
DEFAULT_PLAYABILITY_PROFILE = PlayabilityProfile(
    profileVersion='unity-c1-test-1', maxJumpRisePixels=150,
    maxJumpDistancePixels=230, characterWidthPixels=32,
    characterHeightPixels=58, landingTolerancePixels=6)

def canonical_profile_json(profile: PlayabilityProfile) -> bytes: ...
def derive_level_job_id(content: bytes, profile: PlayabilityProfile) -> str: ...

# app/services/level_rectify.py
def rectify(input_path: Path, job_dir: Path) -> RectifyResult: ...

# app/services/level_detect.py
def detect(rectified_path: Path, job_dir: Path) -> DetectionResult: ...

# app/services/level_semantic.py
def review(rectified_path: Path, detection: DetectionResult,
           client: Optional[SemanticClient] = None) -> SemanticResult: ...

# app/services/playability.py
def analyze(level: Level, profile: PlayabilityProfile) -> PlayabilityAnalysis: ...

# app/services/level_parser.py
def parse(job_dir: Path, progress: Optional[Callable[[str], None]] = None,
          semantic_client: Optional[SemanticClient] = None) -> dict: ...
```

### 任务 1：参数化共享 JobStore

**文件：**
- 修改：`server/app/services/job_store.py`
- 修改：`server/tests/test_job_store.py`

- [ ] **步骤 1：编写失败的参数化隔离测试**

在 `test_job_store.py` 新增：

```python
def test_custom_queue_terminal_states_and_rerun_artifacts(tmp_path):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    store = JobStore('redis://unused', tmp_path, client=r,
                     queue_key='pq:levels',
                     terminal_states=frozenset({'ready', 'needs_fix', 'needs_review', 'failed'}),
                     rerun_artifacts=('result.json', 'rectified.png'))
    store.create('level_a')
    store.enqueue('level_a')
    assert r.llen('pq:characters') == 0
    assert store.dequeue(timeout=1) == 'level_a'
    store.set_status('level_a', 'needs_fix', result={'status': 'needs_fix'})
    store.set_status('level_a', 'processing')
    assert store.get('level_a')['status'] == 'needs_fix'

    job_dir = tmp_path / 'level_a'
    job_dir.mkdir()
    (job_dir / 'result.json').write_text('{}')
    (job_dir / 'rectified.png').write_bytes(b'png')
    (job_dir / 'input.png').write_bytes(b'input')
    store.reset('level_a')
    assert not (job_dir / 'result.json').exists()
    assert not (job_dir / 'rectified.png').exists()
    assert (job_dir / 'input.png').exists()
```

同时新增 `test_set_progress_preserves_status`，断言 `set_progress('level_a', 'rectifying_paper')` 只写 `stage` 和 `updatedAt`。

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_job_store.py -q`

预期：FAIL，`JobStore.__init__()` 不接受参数或缺少 `set_progress`。

- [ ] **步骤 3：实现最少参数化**

将构造函数扩为：

```python
def __init__(self, redis_url: str, jobs_root, client=None,
             queue_key: str = QUEUE_KEY,
             terminal_states=frozenset(TERMINAL_STATES),
             rerun_artifacts=RERUN_ARTIFACTS):
    self.r = client if client is not None else redis.Redis.from_url(redis_url, decode_responses=True)
    self.jobs_root = Path(jobs_root)
    self.queue_key = queue_key
    self.terminal_states = frozenset(terminal_states)
    self.rerun_artifacts = tuple(rerun_artifacts)
```

`enqueue/dequeue/set_status/reset` 改用实例属性；新增：

```python
def set_progress(self, job_id: str, stage: str) -> None:
    key = JOB_KEY.format(job_id)
    self.r.hset(key, mapping={'stage': stage, 'updatedAt': _now()})
    self.r.expire(key, TTL_SECONDS)
```

- [ ] **步骤 4：运行定向与角色回归**

运行：`python -m pytest tests/test_job_store.py tests/test_characters_api.py tests/test_character_worker.py -q`

预期：全部 PASS；角色仍只使用 `pq:characters` 且原终态不可覆盖。

- [ ] **步骤 5：Commit（仅在已获授权时）**

```bash
git add server/app/services/job_store.py server/tests/test_job_store.py
git commit -m "refactor: 参数化异步任务存储"
```

### 任务 2：建立 Level v1 契约与固定样例

**文件：**
- 创建：`server/app/level_contracts.py`
- 创建：`server/tests/test_level_contracts.py`
- 创建：`testdata/levels/contracts/ready.json`
- 创建：`testdata/levels/contracts/needs-fix.json`
- 创建：`testdata/levels/contracts/needs-review.json`
- 创建：`testdata/levels/contracts/failed.json`
- 创建：`testdata/levels/contracts/invalid-out-of-bounds.json`
- 创建：`testdata/levels/contracts/invalid-background-size.json`

- [ ] **步骤 1：写六份固定协议样例**

合法样例逐字采用 `level-image-to-json-api.md` 第 6、8、9、10 节结构；非法样例从 ready 的 `result.level` 派生：一个把平台端点设为 `canvas.width`，另一个把 `background.width` 改为 `canvas.width + 1`。所有 URL 使用 `/artifacts/level_fixture/...`，SHA-256 使用 64 个小写十六进制字符。

- [ ] **步骤 2：编写失败的契约测试**

```python
@pytest.mark.parametrize('name,model', [
    ('ready.json', LevelReady), ('needs-fix.json', LevelNeedsFix),
    ('needs-review.json', LevelNeedsReview), ('failed.json', LevelFailed),
])
def test_terminal_fixture_matches_contract(name, model):
    model.model_validate_json((FIXTURES / name).read_text())

@pytest.mark.parametrize('name', ['invalid-out-of-bounds.json',
                                  'invalid-background-size.json'])
def test_invalid_level_fixture_is_rejected(name):
    with pytest.raises(ValidationError):
        Level.model_validate_json((FIXTURES / name).read_text())

def test_level_job_id_is_stable_and_profile_sensitive():
    a = derive_level_job_id(b'image', DEFAULT_PLAYABILITY_PROFILE)
    same = derive_level_job_id(b'image', PlayabilityProfile(**DEFAULT_PLAYABILITY_PROFILE.model_dump()))
    changed = derive_level_job_id(b'image', DEFAULT_PLAYABILITY_PROFILE.model_copy(
        update={'maxJumpDistancePixels': 231}))
    assert a == same
    assert a.startswith('level_') and len(a) == 18
    assert changed != a
```

另测：平台 ID 唯一、`start.x <= end.x`、零长度、goal 矩形完整在画布内、背景尺寸匹配、`PlayabilityProfile` 正数/非负边界、固定坐标系和至少一个平台。

- [ ] **步骤 3：运行测试验证失败**

运行：`python -m pytest tests/test_level_contracts.py -q`

预期：FAIL，`app.level_contracts` 尚不存在。

- [ ] **步骤 4：实现 Pydantic 契约与幂等哈希**

定义 `Point/Region/Canvas/CoordinateSystem/Background/PlayerStart/Platform/Level`；`Level` 用 `@model_validator(mode='after')` 执行跨字段校验。定义 `Warning/PlayabilityAnalysis/Artifacts/ReviewCandidate/Review/ErrorDetail` 和四种终态信封。规范化能力参数：

```python
def canonical_profile_json(profile: PlayabilityProfile) -> bytes:
    return json.dumps(profile.model_dump(), sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


def derive_level_job_id(content: bytes, profile: PlayabilityProfile) -> str:
    digest = hashlib.sha256(content + canonical_profile_json(profile)
                            + ALGORITHM_MAJOR_VERSION.encode('ascii')).hexdigest()
    return 'level_' + digest[:12]
```

HTTP 错误信封实现协议第 11 节的 `{"error": {code,message,retryable,requestId,details?}}`；P3 的 401/403/429 不实现，但保留协议模型可解析这些代码。

- [ ] **步骤 5：运行测试验证通过**

运行：`python -m pytest tests/test_level_contracts.py -q`

预期：全部 PASS。

- [ ] **步骤 6：Commit（仅在已获授权时）**

```bash
git add server/app/level_contracts.py server/tests/test_level_contracts.py testdata/levels/contracts
git commit -m "feat: 定义 Level API v1 契约"
```

### 任务 3：实现关卡上传与状态查询 API

**文件：**
- 创建：`server/app/api/levels.py`
- 创建：`server/tests/test_levels_api.py`
- 修改：`server/app/main.py`

- [ ] **步骤 1：编写失败的 API 测试**

fixture 创建 `app.state.level_store = JobStore(..., queue_key='pq:levels', terminal_states=LEVEL_TERMINAL_STATES, rerun_artifacts=LEVEL_RERUN_ARTIFACTS)`。用 Pillow 在内存生成 800×800 PNG/JPEG，并覆盖：

```python
def test_upload_valid_returns_202_and_enqueues(client, png_800):
    resp = upload(client, png_800)
    assert resp.status_code == 202
    body = resp.json()
    assert body['jobId'].startswith('level_')
    assert body['status'] == 'queued'
    assert body['statusUrl'] == f"/v1/levels/{body['jobId']}"
    assert client.app.state.level_store.r.llen('pq:levels') == 1


def test_same_image_same_profile_is_idempotent(client, png_800): ...
def test_different_profile_changes_job_id(client, png_800): ...
def test_force_resets_level_artifacts_and_requeues(client, png_800): ...
def test_get_processing_includes_progress_stage(client, png_800): ...
def test_get_terminal_returns_saved_envelope(client, png_800): ...
```

边界断言：>10MiB→413、GIF/非图片→415、截断 PNG→422、799 或 12001 单边→422、>40M 像素→422、动画 PNG→422、坏 profile JSON/缺字段/负数→400、`schemaVersion != 1.0`→400、未知任务→404、Redis 异常→503。另用带 EXIF Orientation=6 的 JPEG 断言保存后的 `input.png` 已转正且无动画帧。

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_levels_api.py -q`

预期：FAIL，路由返回 404。

- [ ] **步骤 3：实现上传边界和独立 store**

`levels.py` 使用 `File/Form/Query`；读取最多 `MAX_UPLOAD_BYTES + 1`，按 PNG/JPEG 魔数判断，Pillow `Image.open` + `verify` 后重新打开，拒绝 `n_frames != 1`，执行 `ImageOps.exif_transpose(...).convert('RGB' 或 'RGBA')` 并统一保存为 `input.png`。在解码前设置 Pillow decompression-bomb 防线，解码后再校验单边和总像素。

`main.py` 保留 `app.state.store` 给角色，并新增：

```python
app.state.level_store = JobStore(redis_url, jobs_root,
    queue_key='pq:levels', terminal_states=LEVEL_TERMINAL_STATES,
    rerun_artifacts=LEVEL_RERUN_ARTIFACTS)
app.include_router(levels_router)
```

queued/processing 响应从 hash 组装 `jobId/status/progress.stage/createdAt/updatedAt`；终态直接解析 `result`。创建时间由 JobStore `create` 同时写 `createdAt`，需补充任务 1 的角色回归断言，且 `_now()` 输出带 `Z` 的 UTC ISO 8601。

- [ ] **步骤 4：运行 API 与角色回归**

运行：`python -m pytest tests/test_levels_api.py tests/test_characters_api.py tests/test_job_store.py -q`

预期：全部 PASS；两个 API 分别只进入自己的队列。

- [ ] **步骤 5：Commit（仅在已获授权时）**

```bash
git add server/app/api/levels.py server/app/main.py server/app/services/job_store.py server/tests/test_levels_api.py server/tests/test_job_store.py
git commit -m "feat: 新增关卡异步任务 API"
```

### 任务 4：生成可重复的合成关卡样本

**文件：**
- 创建：`server/scripts/gen_level_samples.py`
- 创建：`server/tests/test_gen_level_samples.py`
- 创建：`testdata/levels/synthetic/manifest.json`
- 创建：`testdata/levels/synthetic/*.png`
- 创建：`testdata/levels/synthetic/*.json`

- [ ] **步骤 1：编写失败的生成器自检**

```python
def test_generation_is_deterministic(tmp_path):
    generate_dataset(tmp_path, seed=20260908, count=10)
    first = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in tmp_path.iterdir()}
    generate_dataset(tmp_path, seed=20260908, count=10)
    second = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in tmp_path.iterdir()}
    assert first == second


def test_manifest_covers_required_variants(tmp_path):
    generate_dataset(tmp_path, seed=20260908, count=10)
    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    assert len(manifest['samples']) == 10
    assert {'front', 'perspective', 'rotated', 'uneven_light'} <= {
        s['variant'] for s in manifest['samples']}
    for sample in manifest['samples']:
        assert sample['platforms']
        assert len(sample['paperCorners']) == 4
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_gen_level_samples.py -q`

预期：FAIL，生成器模块不存在。

- [ ] **步骤 3：实现固定 seed 构造器**

脚本先在白纸画黑色/蓝色横线和红色旗帜，再应用已知单应矩阵、0°/90°旋转和线性亮度梯度。每份真值写 `paperCorners/platforms/goalRegion/expectedStatus/variant`；`manifest.json` 记录 seed、生成器版本和文件 SHA-256。执行入口：

```python
if __name__ == '__main__':
    generate_dataset(Path(__file__).resolve().parents[2]
                     / 'testdata/levels/synthetic', seed=20260908, count=10)
```

- [ ] **步骤 4：生成入库数据并验证**

运行：`python scripts/gen_level_samples.py && python -m pytest tests/test_gen_level_samples.py -q`

预期：10 份样本生成，测试 PASS；再次运行 `git diff --exit-code -- ../testdata/levels/synthetic` 时字节不变化。

- [ ] **步骤 5：Commit（仅在已获授权时）**

```bash
git add server/scripts/gen_level_samples.py server/tests/test_gen_level_samples.py testdata/levels/synthetic
git commit -m "test: 新增关卡解析合成样本集"
```

### 任务 5：实现纸张检测与透视拉正

**文件：**
- 创建：`server/app/services/level_rectify.py`
- 创建：`server/tests/test_level_rectify.py`

- [ ] **步骤 1：编写失败的四角与透视测试**

```python
def test_order_corners_returns_tl_tr_br_bl():
    shuffled = np.array([[900, 700], [100, 100], [100, 700], [900, 100]], np.float32)
    assert order_corners(shuffled).tolist() == [
        [100, 100], [900, 100], [900, 700], [100, 700]]


def test_rectify_recovers_synthetic_canvas(tmp_path, perspective_sample):
    result = rectify(perspective_sample.image, tmp_path)
    expected = perspective_sample.truth
    assert result.width == expected['rectifiedSize']['width']
    assert result.height == expected['rectifiedSize']['height']
    assert max_corner_error(result.corners, expected['paperCorners']) <= 4
    assert (tmp_path / 'rectified.png').exists()
    assert (tmp_path / 'paper-mask.png').exists()
    transform = json.loads((tmp_path / 'transform.json').read_text())
    assert len(transform['matrix']) == 3
```

另测：无纸→`PAPER_NOT_FOUND`、两个近似等面积四边形→`PAPER_AMBIGUOUS`、边缘被截断→`PAPER_OCCLUDED`、长边方向明确时输出横屏、近方形且方向证据不足→`ORIENTATION_AMBIGUOUS`。

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_level_rectify.py -q`

预期：FAIL，模块不存在。

- [ ] **步骤 3：实现最小拉正流程**

定义 `RectifyIssue(reason, candidates)` 和 `RectifyResult(rectified_path, width, height, corners, matrix)`。在最长边缩到 1600px 的副本上：灰度→Gaussian blur→Canny→闭运算→`findContours`→面积≥图像 25%→`approxPolyDP` 四边形；候选比例还原到原图，再按 TL/TR/BR/BL 排序并计算输出尺寸与 `getPerspectiveTransform`。候选面积接近、边界截断或方向置信度不足抛业务 issue。

阈值旁写清：基于 10 份固定合成样本和黄金图片标定；加入真实拍照集后必须重标定；`ponytail:` 说明当前只支持单张近矩形纸，升级路径是学习式文档角点检测。

- [ ] **步骤 4：运行拉正测试**

运行：`python -m pytest tests/test_level_rectify.py -q`

预期：全部 PASS，透视角点误差≤4px，产物尺寸与真值一致。

- [ ] **步骤 5：Commit（仅在已获授权时）**

```bash
git add server/app/services/level_rectify.py server/tests/test_level_rectify.py
git commit -m "feat: 实现关卡纸张透视拉正"
```

### 任务 6：实现平台线与旗帜候选检测

**文件：**
- 创建：`server/app/services/level_detect.py`
- 创建：`server/tests/test_level_detect.py`

- [ ] **步骤 1：编写失败的平台检测测试**

```python
def test_detects_constructed_platform_endpoints(tmp_path, front_sample):
    result = detect(front_sample.rectified, tmp_path)
    pairs = match_by_y(result.platform_candidates, front_sample.truth['platforms'])
    errors = [endpoint_error(actual, expected) for actual, expected in pairs]
    assert np.median(errors) <= 3
    assert np.percentile(errors, 95) <= 8
    assert (tmp_path / 'ink-mask.png').exists()


def test_merges_collinear_fragments_but_not_parallel_lines(tmp_path): ...
def test_preserves_small_slope_instead_of_flattening(tmp_path): ...
def test_rejects_page_edge_shadow_and_short_text_strokes(tmp_path): ...
def test_detects_single_red_flag_candidate(tmp_path): ...
def test_multiple_flags_remain_separate_candidates(tmp_path): ...
```

黄金测试直接对 `testdata/levels/golden/level1-background.png` 调 `detect`（跳过拉正），与 `level1.json` 的 7 条线做最优一一匹配，断言检出 7 条、无额外高置信候选、端点中位误差≤`max(3px, short_side*0.004)`、P95≤`max(8px, short_side*0.01)`。

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_level_detect.py -q`

预期：FAIL，模块不存在。

- [ ] **步骤 3：实现检测与精修**

定义 `PointCandidate/PlatformCandidate/GoalCandidate/DetectionResult` dataclass。平台流程：局部光照背景估计→black-hat 与自适应阈值合并→水平形态学开运算→概率 Hough/轮廓候选→角度、长度、宽高比、墨迹覆盖率过滤→按 Y/角度/间隙保守合并→在原始 ink mask 的窄带中精修两端和中心线。ID 按 `(平均y, start.x, end.x)` 稳定排序后生成 `line_001`。

旗帜流程：红色 HSV 双区间 mask→连通域→寻找邻接近竖直墨迹杆→输出完整包围框，ID 稳定生成 `goal_001`。不能把旗杆加入平台候选。

所有阈值注释记录合成+黄金样本标定依据与重标定条件；`ponytail:` 说明 v1 只处理近水平直平台，曲线升级需轮廓折线模型。

- [ ] **步骤 4：运行检测回归**

运行：`python -m pytest tests/test_level_detect.py -q`

预期：全部 PASS，黄金样本达到第 1 步误差门槛。

- [ ] **步骤 5：Commit（仅在已获授权时）**

```bash
git add server/app/services/level_detect.py server/tests/test_level_detect.py
git commit -m "feat: 检测关卡平台与旗帜候选"
```

### 任务 7：实现 LLM 候选语义复核与降级

**文件：**
- 创建：`server/app/services/level_semantic.py`
- 创建：`server/tests/test_level_semantic.py`

- [ ] **步骤 1：编写失败的语义复核测试**

```python
def test_unconfigured_llm_uses_opencv_candidates(monkeypatch, detection):
    for key in ('LEVEL_LLM_BASE_URL', 'LEVEL_LLM_API_KEY', 'LEVEL_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    result = review(Path('rectified.png'), detection)
    assert result.source == 'opencv'
    assert [x.candidate_id for x in result.platforms] == ['line_001']


def test_llm_can_only_select_existing_candidate_ids(tmp_path, detection):
    client = FakeClient({'lineClassifications': [
        {'candidateId': 'invented', 'label': 'platform', 'confidence': .99}],
        'goalClassifications': [], 'sceneIssues': [], 'decision': 'accepted'})
    result = review(tmp_path / 'rectified.png', detection, client=client)
    assert result.source == 'opencv'
    assert result.degraded_reason == 'INVALID_LLM_RESPONSE'
```

另测：三环境变量齐全才启用；请求失败/超时/非 JSON/Schema 缺字段/未知 label 均降级；合法响应可剔除阴影候选但坐标对象仍逐字来自 detection；`decision=needs_review`、多个 goal 分数接近和场景问题均产生明确 review reason；审计文件不含 API key。

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_level_semantic.py -q`

预期：FAIL，模块不存在。

- [ ] **步骤 3：实现严格候选分类器**

定义 `SemanticClient` protocol、`RequestsSemanticClient` 和 `SemanticResult`。默认客户端仅在三个环境变量齐全时创建，POST `{base_url.rstrip('/')}/chat/completions`，请求含降采样叠加图 data URI、候选 JSON、协议 16.3 提示词、`temperature: 0.1` 和严格 response schema。超时使用 `(5, 30)`。

先用 Pydantic 校验响应，再验证所有 candidateId 属于输入集合；输出仅保存候选 ID，最终几何从 `DetectionResult` 映射，不接受模型坐标字段。审计文件写 `model/promptVersion/requestCandidateIds/response/degradedReason`，用临时文件后 `replace` 原子写入。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/test_level_semantic.py -q`

预期：全部 PASS；无网络、无环境变量时测试仍离线通过。

- [ ] **步骤 5：Commit（仅在已获授权时）**

```bash
git add server/app/services/level_semantic.py server/tests/test_level_semantic.py
git commit -m "feat: 增加关卡候选语义复核"
```

### 任务 8：实现像素空间可玩性分析

**文件：**
- 创建：`server/app/services/playability.py`
- 创建：`server/tests/test_playability.py`

- [ ] **步骤 1：编写失败的可达图测试**

```python
def test_finds_path_without_mutating_geometry(playable_level, profile):
    before = playable_level.model_dump_json()
    result = analyze(playable_level, profile)
    assert result.playability == 'playable'
    assert result.path == ['platform_001', 'platform_002', 'platform_004']
    assert playable_level.model_dump_json() == before


def test_unreachable_returns_no_path_warning(unreachable_level, profile):
    result = analyze(unreachable_level, profile)
    assert result.playability == 'unreachable'
    assert result.path == []
    assert 'NO_PATH_TO_GOAL' in {w.code for w in result.warnings}
```

分别构造并断言 `START_NOT_SUPPORTED`、`GOAL_NOT_SUPPORTED`、`JUMP_GAP_TOO_HIGH`、`JUMP_GAP_TOO_WIDE`、`LANDING_AREA_TOO_SHORT`、`NO_PATH_TO_GOAL`；警告包含相关平台 ID 以及适用的 required/available 像素值。另测向下跳不套用上升高度限制、轻微斜线以目标着陆点 Y 计算、同输入结果字节稳定。

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_playability.py -q`

预期：FAIL，模块不存在。

- [ ] **步骤 3：实现承载判断和 BFS**

出生平台要求出生点 X 落在线段水平区间（考虑 tolerance）且脚底 Y 距线段插值 Y 不超过 tolerance。终点平台取 goalRegion 底边邻近且水平投影重叠的平台。边 `A -> B`：目标着陆有效长度≥角色宽；上升量≤maxJumpRise；两平台最近水平间隔≤maxJumpDistance+landingTolerance。用按平台 ID 排序的 BFS 获得稳定最短路径。

`ponytail:` 注明该模型是轴向能力包络，不模拟抛物线与障碍碰撞；真实物理误判出现时升级为共享 Unity 轨迹采样器，但不得在此调整平台。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/test_playability.py -q`

预期：全部 PASS，六个警告码均有独立覆盖。

- [ ] **步骤 5：Commit（仅在已获授权时）**

```bash
git add server/app/services/playability.py server/tests/test_playability.py
git commit -m "feat: 增加关卡可玩性诊断"
```

### 任务 9：编排解析门面并发布权威产物

**文件：**
- 创建：`server/app/services/level_parser.py`
- 创建：`server/tests/test_level_parser.py`

- [ ] **步骤 1：编写失败的门面终态测试**

通过 monkeypatch 注入前面模块的确定结果：

```python
def test_parse_ready_writes_aligned_authoritative_artifacts(tmp_path, parser_stubs):
    payload = parse(tmp_path, progress=parser_stubs.progress)
    assert payload['status'] == 'ready'
    level = Level.model_validate(payload['result']['level'])
    with Image.open(tmp_path / 'rectified.png') as image:
        assert image.size == (level.canvas.width, level.canvas.height)
    assert json.loads((tmp_path / 'level.json').read_text()) == level.model_dump()
    assert (tmp_path / 'analysis.json').exists()
    assert (tmp_path / 'overlay.png').exists()
    assert parser_stubs.stages == [
        'validating_upload', 'rectifying_paper', 'detecting_platforms',
        'detecting_goal', 'semantic_review', 'validating_geometry',
        'analyzing_playability', 'publishing_artifacts']


def test_unplayable_is_needs_fix_and_keeps_exact_geometry(tmp_path, parser_stubs): ...
def test_ambiguous_goal_is_needs_review_without_level_json(tmp_path, parser_stubs): ...
```

另测所有 review reason：纸张失败、无平台、无旗、双旗接近、找不到出生平台、低置信度；验证 stable platform ID、所有坐标范围、重复平台拒绝、背景 SHA-256、出生点选画布下半区最左且可容纳角色的平台并按安全距离内缩；叠加图只画调试层，不改 `rectified.png`；同输入两次 `level.json/analysis.json` 字节一致。

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_level_parser.py -q`

预期：FAIL，门面模块不存在。

- [ ] **步骤 3：实现出生点、几何校验与信封组装**

`parse` 从 `job_dir/input.png` 和 `request.json` 读取输入；依次调用共享接口。出生点安全内缩为 `max(characterWidthPixels // 2, 8)`，置信度取承载平台置信度；此阈值在合成+黄金样本验证并注明真实角色宽度变化时重标定。

先构建 `Level` 让 Pydantic 执行边界/唯一/背景约束，再补检测特有门禁：ink coverage、近重叠重复线和最低发布置信度。歧义在可玩性前返回 `needs_review` 且不创建 `level.json`。可靠几何始终写 `level.json`，可达→ready，不可达→needs_fix。

JSON 使用 `sort_keys=True, separators=(',', ':'), ensure_ascii=False`；所有最终文件通过同目录临时文件 `replace` 原子发布。overlay 从 rectified 副本绘制 1–2px 平台、ID、出生点、终点和可玩路径。

- [ ] **步骤 4：运行门面及模块回归**

运行：`python -m pytest tests/test_level_parser.py tests/test_level_rectify.py tests/test_level_detect.py tests/test_level_semantic.py tests/test_playability.py -q`

预期：全部 PASS。

- [ ] **步骤 5：Commit（仅在已获授权时）**

```bash
git add server/app/services/level_parser.py server/tests/test_level_parser.py
git commit -m "feat: 编排关卡解析流水线"
```

### 任务 10：实现独立关卡 runner 与 worker

**文件：**
- 创建：`server/app/workers/level_runner.py`
- 创建：`server/app/workers/level_worker.py`
- 创建：`server/tests/test_level_runner.py`
- 创建：`server/tests/test_level_worker.py`

- [ ] **步骤 1：编写失败的 runner 测试**

```python
def test_runner_writes_business_terminal_and_returns_zero(tmp_path, monkeypatch):
    job_dir = make_job(tmp_path)
    payload = {'jobId': job_dir.name, 'status': 'needs_review',
               'schemaVersion': '1.0', 'algorithmVersion': ALGORITHM_VERSION,
               'review': {'reason': 'PAPER_NOT_FOUND', 'message': '未检测到纸张。',
                          'candidates': [], 'suggestions': []}, 'artifacts': {}}
    monkeypatch.setattr(level_runner, 'parse', lambda *a, **k: payload)
    assert level_runner.run(job_dir) == 0
    assert json.loads((job_dir / 'result.json').read_text()) == payload


def test_runner_infrastructure_exception_returns_nonzero_without_snapshot(...): ...
```

断言 runner progress 回调写入同一个 level store 的 stage，且 `result.json` 使用临时文件原子替换。

- [ ] **步骤 2：编写失败的 worker 测试**

镜像角色测试但使用 level 语义：ready/needs_fix/needs_review 均一次完成不重试；崩溃重试 1 次后 failed `PROCESSING_CRASHED`；超时后 failed `PROCESSING_TIMEOUT`；损坏/未知 status 的 result.json 视为 crash；`_run_subprocess` 命令为 `python -m app.workers.level_runner <abs_job_dir>`，默认 timeout=90。

- [ ] **步骤 3：运行测试验证失败**

运行：`python -m pytest tests/test_level_runner.py tests/test_level_worker.py -q`

预期：FAIL，模块不存在。

- [ ] **步骤 4：实现 runner 和 worker**

`level_runner.run` 调 `parse`，业务终态都退出 0；未处理异常打印 traceback 并退出 2。worker 创建参数化 JobStore，`BRPOP pq:levels`，先写 processing，再最多执行两次子进程；读取结果后先由终态 Pydantic 联合模型校验再写 Redis。基础设施 failed 信封使用协议第 10 节 `error` 结构。

- [ ] **步骤 5：运行 worker 与角色链路回归**

运行：`python -m pytest tests/test_level_runner.py tests/test_level_worker.py tests/test_character_worker.py tests/test_render_runner.py -q`

预期：全部 PASS；角色退出码与错误码不变。

- [ ] **步骤 6：Commit（仅在已获授权时）**

```bash
git add server/app/workers/level_runner.py server/app/workers/level_worker.py server/tests/test_level_runner.py server/tests/test_level_worker.py
git commit -m "feat: 增加关卡解析 worker"
```

### 任务 11：接入容器并完成全栈冒烟

**文件：**
- 修改：`server/docker-compose.yml`
- 创建：`server/scripts/smoke_levels_e2e.sh`
- 创建：`server/tests/test_smoke_levels_script.py`

- [ ] **步骤 1：编写失败的静态配置测试**

```python
def test_compose_has_level_worker():
    compose = yaml.safe_load((SERVER / 'docker-compose.yml').read_text())
    service = compose['services']['level-worker']
    assert service['command'] == ['python', '-m', 'app.workers.level_worker']
    assert service['environment']['OUT_ROOT'] == '/data/out'
    assert service['volumes'] == ['./out:/data/out']


def test_smoke_script_checks_contract_artifacts():
    text = (SERVER / 'scripts/smoke_levels_e2e.sh').read_text()
    assert 'SMOKE_LEVELS_E2E_PASS' in text
    assert 'rectifiedImageUrl' in text
    assert 'levelJsonUrl' in text
    assert 'analysisJsonUrl' in text
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_smoke_levels_script.py -q`

预期：FAIL，service 和脚本不存在。

- [ ] **步骤 3：增加 level-worker 服务与冒烟脚本**

`level-worker` 复用 `paper-game/server:local`，挂同一 `./out:/data/out`，依赖 redis healthy 和 api started；环境传 `REDIS_URL/OUT_ROOT/LEVEL_LLM_BASE_URL/LEVEL_LLM_API_KEY/LEVEL_LLM_MODEL`，后 3 个默认空。`stop_grace_period: 200s`，覆盖 90s×2+余量。

冒烟脚本默认上传 `../testdata/levels/synthetic/front.png`，轮询 ready，下载并用 Pillow/JSON 检查：背景尺寸==canvas、全部坐标在界内、三份契约产物可下载、再次上传得到同 jobId，最终打印：

```text
SMOKE_LEVELS_E2E_PASS jobId=... elapsed=..s
```

- [ ] **步骤 4：运行静态测试与全栈冒烟**

运行：

```bash
python -m pytest tests/test_smoke_levels_script.py -q
docker compose up -d --build redis api level-worker
bash scripts/smoke_levels_e2e.sh
```

预期：测试 PASS，脚本输出 `SMOKE_LEVELS_E2E_PASS`。若当前机器无法运行 Docker，记录具体阻塞，不得声称完成了全栈冒烟。

- [ ] **步骤 5：Commit（仅在已获授权时）**

```bash
git add server/docker-compose.yml server/scripts/smoke_levels_e2e.sh server/tests/test_smoke_levels_script.py
git commit -m "feat: 接入关卡解析全栈服务"
```

### 任务 12：更新项目文档并执行完整验收

**文件：**
- 修改：`README.md`
- 修改：`CLAUDE.md`
- 修改：`.ai/workflow-records/feat-level-parser.yml`

- [ ] **步骤 1：更新进度与操作文档**

把“任务 4-5 未动工”更新为已落地；补 `/v1/levels` 上传/轮询示例、`level-worker` 容器、`smoke_levels_e2e.sh`、产物布局、LLM 三个可选环境变量、纯 OpenCV 降级行为、算法版本和 v1 已知边界。保留 Unity 任务 6-7 未实现的事实。

workflow record 设置：

```yaml
plan: docs/superpowers/plans/2026-09-08-level-parser.md
verification: pending
code_review: pending
status: in_progress
```

- [ ] **步骤 2：运行完整离线回归**

运行：

```bash
python -m pytest tests/ -q --ignore=tests/test_spike_batch.py
```

预期：全部 PASS；TorchServe 未在线时仅项目既有允许 skip 的用例 skip，关卡新增测试不依赖 TorchServe 或外网。

- [ ] **步骤 3：运行固定数据验收**

运行：

```bash
python scripts/gen_level_samples.py
python -m pytest tests/test_level_detect.py -q -k "golden or endpoint or stable"
```

预期：合成数据无字节变化；黄金图片检出 7 条平台；中位端点误差≤`max(3px, 短边0.4%)`，P95≤`max(8px, 短边1%)`；JSON 重跑字节稳定。

- [ ] **步骤 4：执行代码审查和完成前验证**

调用 `requesting-code-review`，重点检查：几何是否可能被 LLM/可玩性修改、终态是否可覆盖、图片炸弹防线、路径是否绝对化、产物是否原子发布、角色链路是否回归。修复确认问题后调用 `verification-before-completion` 并重跑受影响测试与完整离线回归。

- [ ] **步骤 5：回写验证证据**

仅在所有离线测试和实际执行的冒烟通过后，将 workflow record 更新为：

```yaml
verification: passed
code_review: passed
status: completed
```

若 Docker 冒烟未运行，`verification` 保持 `partial` 并写明阻塞原因；不得标为 passed。

- [ ] **步骤 6：Commit（仅在已获授权时）**

```bash
git add README.md CLAUDE.md .ai/workflow-records/feat-level-parser.yml
git commit -m "docs: 更新关卡解析服务说明"
```

## 自检结果

- **规格覆盖：** 任务 1–3 覆盖异步基础设施、契约、上传校验、幂等、状态查询和 progress；任务 4–6 覆盖合成真值、拉正、平台与旗帜几何；任务 7 覆盖 LLM 严格复核和降级；任务 8 覆盖出生/终点承载与六类可玩性警告；任务 9 覆盖出生点、几何门禁、三类业务终态及全部产物；任务 10–11 覆盖子进程、重试、容器和冒烟；任务 12 覆盖文档、固定指标、完整回归与审查。P3 排除项未混入。
- **占位符扫描：** 无“待定/TODO/类似任务/适当处理”等不可执行描述；每个实现任务都有具体失败测试、实现接口、命令和预期结果。
- **类型一致性：** 全计划统一使用 `PlayabilityProfile`、`Level`、`DetectionResult`、`SemanticResult`、`PlayabilityAnalysis`、`parse(job_dir, progress, semantic_client)`；任务队列固定为 `pq:levels`；业务终态固定为 ready/needs_fix/needs_review/failed。
- **风险校正：** 协议要求 `force=true` 创建新运行实例，而当前角色模式复用 jobId；已批准设计明确选择 v1 单机复用并标注 P3 升级路径，实施按设计执行。协议要求“跳跃参数变化不得触发重新识别”与当前幂等键含 profile 存在张力；已批准设计明确 profile 参与 jobId，本计划不添加独立重分析接口。
