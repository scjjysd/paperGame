# Character Pipeline Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 加固角色生成管线的超时与诊断能力，使慢修复、ARAP 丢 pin、帧数折叠和健康检查日志噪声都能被准确识别。

**Architecture:** 保持现有 API -> Redis -> character worker -> render runner -> application service 分层不变。所有算法诊断放在项目应用服务适配层，vendor 源码保持只读；超时作为基础设施异常交回 worker 重试，诊断日志不进入 API 契约。

**Tech Stack:** Python 3.9、pytest、NumPy/SciPy、Pillow、FastAPI/Uvicorn、Docker Compose YAML。

**Spec:** `docs/superpowers/specs/2026-09-19-character-pipeline-diagnostics-design.md`

## Global Constraints

- 不重建或重启当前 Docker 服务。
- 不调整 TorchServe worker 数。
- 不改变 `force=true` 行为。
- 不修改 `vendor/AnimatedDrawings` 源码。
- 不改 ARAP 数学、网格密度、动作配方、动作帧数或精灵表契约。
- 不改变角色 worker 单任务串行消费与子进程隔离。
- 保持 Python 3.9 兼容和现有 DDD 调用方向。
- 新日志只用于诊断，不进入 API 响应或持久化业务契约。
- 所有生产代码修改严格执行 RED -> GREEN；先运行新增测试确认因缺少行为而失败。

---

### Task 1: 标注修复子阶段计时

**Files:**
- Modify: `app/services/annotation_repair.py`
- Modify: `tests/test_annotation_repair.py`

**Interfaces:**
- Consumes: 现有 `repair_or_reject(anno_dir) -> Dict`、`_candidate()`、`mesh_unreachable()`、`_mesh_ok()`。
- Produces: `_timed_repair_stage(anno_dir: Path, stage: str)` 上下文管理器；日志格式 `标注修复计时：anno=%s stage=%s elapsed=%.3fs`。

- [ ] **Step 1: 写快速路径失败测试**

在 `tests/test_annotation_repair.py` 添加使用现有 `_write_anno`/`_drawing` fixture 的测试：创建干净、单连通标注并提供 `image.png` 与 `bounding_box.yaml`，设置 `caplog` 到 `INFO`，调用 `repair_or_reject()`。断言日志包含 `stage=baseline`、`stage=clean_check`、`stage=skeleton`、`stage=write`，且不包含 `stage=mesh.` 或 `stage=candidate.`。

- [ ] **Step 2: 运行快速路径测试确认 RED**

Run: `.venv/bin/pytest -q tests/test_annotation_repair.py::test_repair_logs_fast_path_stage_timings`

Expected: FAIL，因为当前没有 `标注修复计时` 子阶段日志。

- [ ] **Step 3: 写候选与 mesh 惰性计时失败测试**

复用现有需要扩框的 fixture，monkeypatch `_candidate` 与 `mesh_unreachable` 记录调用次数。断言实际运行的 `candidate.plain`、`candidate.ink`、`candidate.pad` 和 `mesh.baseline`/`mesh.<crop>` 有计时日志，同时每个算法调用次数与修改前一致，未选中的惰性 mesh 候选没有日志。

- [ ] **Step 4: 运行候选路径测试确认 RED**

Run: `.venv/bin/pytest -q tests/test_annotation_repair.py::test_repair_logs_only_executed_candidate_and_mesh_stages`

Expected: FAIL，因为当前没有候选和 mesh 计时日志。

- [ ] **Step 5: 实现最小计时器与阶段包裹**

在 `annotation_repair.py` 导入 `logging`、`time` 和 `contextmanager`，定义：

```python
logger = logging.getLogger(__name__)

@contextmanager
def _timed_repair_stage(anno_dir: Path, stage: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        logger.info('标注修复计时：anno=%s stage=%s elapsed=%.3fs',
                    anno_dir, stage, time.perf_counter() - started)
```

只包裹已有调用，不复制计算。为候选 mesh 检查增加局部 helper，使 generator 实际求值某候选时才进入 `mesh.<tag>` 上下文；快速路径不得计算 floor 或候选 mesh。

- [ ] **Step 6: 运行 Task 1 测试与现有标注回归**

Run: `.venv/bin/pytest -q tests/test_annotation_repair.py`

Expected: PASS，且无新增算法调用。

- [ ] **Step 7: 提交 Task 1**

```bash
git add app/services/annotation_repair.py tests/test_annotation_repair.py
git commit -m "feat: trace annotation repair stages"
```

---

### Task 2: 图像分析总超时与基础设施重试语义

**Files:**
- Modify: `app/services/annotations.py`
- Modify: `tests/test_annotations.py`
- Modify: `docker-compose.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: `analyze(img_path, out_dir) -> Dict`、render runner 的通用异常返回码 2、character worker 的最多两次尝试。
- Produces: `AnalysisTimeout(TimeoutError)`；`ANALYZE_TIMEOUT_SECONDS` 正浮点配置，默认 `30` 秒。

- [ ] **Step 1: 写配置解析和超时分类失败测试**

在 `tests/test_annotations.py` 导入模块而非只导入函数。monkeypatch vendor `image_to_annotations` 为阻塞函数，设置 `ANALYZE_TIMEOUT_SECONDS=0.01`，断言 `analyze()` 抛 `AnalysisTimeout`，且不是 `NeedsCorrection`。参数化非法值 `0`、`-1`、`abc`，断言 `ValueError` 明确包含环境变量名。

- [ ] **Step 2: 运行超时测试确认 RED**

Run: `.venv/bin/pytest -q tests/test_annotations.py -k 'timeout or invalid_analysis_timeout'`

Expected: FAIL，因为当前没有 `AnalysisTimeout` 和配置解析。

- [ ] **Step 3: 写计时器恢复和降级失败测试**

保存测试前 handler/timer；正常 fake 完成后断言 handler 与剩余 timer 被恢复。monkeypatch 当前线程为非主线程或 signal 能力检查为 false，断言调用继续完成并记录 WARNING，而不是安装 signal handler。

- [ ] **Step 4: 运行计时器生命周期测试确认 RED**

Run: `.venv/bin/pytest -q tests/test_annotations.py -k 'restores or unsupported_signal'`

Expected: FAIL，因为当前不存在计时器上下文。

- [ ] **Step 5: 实现总超时适配器**

在 `annotations.py` 增加 `_analysis_timeout_seconds()` 和 `_analysis_deadline(seconds)`。主线程且平台具备 `SIGALRM`/`ITIMER_REAL` 时用 `signal.setitimer()` 包裹一次完整 vendor 调用；退出时先取消本计时器，再恢复旧 handler，并按已耗时间扣减旧 timer 的剩余秒数。非主线程或不支持的平台记录 WARNING 并直接执行。异常处理顺序必须是：

```python
except AnalysisTimeout:
    raise
except (AssertionError, Exception) as exc:
    raise NeedsCorrection(_classify(str(exc)), str(exc)) from exc
```

- [ ] **Step 6: 将配置传入 worker 并更新运行说明**

在 `docker-compose.yml` 的 `worker.environment` 增加：

```yaml
ANALYZE_TIMEOUT_SECONDS: ${ANALYZE_TIMEOUT_SECONDS:-30}
```

在 README 的角色 worker 配置说明中记录默认 30 秒、只约束 analyze 总阶段、超时由 worker 按基础设施错误重试。

- [ ] **Step 7: 运行 Task 2 测试与 worker 回归**

Run: `.venv/bin/pytest -q tests/test_annotations.py tests/test_render_runner.py tests/test_character_worker.py tests/test_torchserve_config.py`

Expected: PASS；需要真实 TorchServe 的原有集成测试在服务不可达时允许 SKIP。

- [ ] **Step 8: 提交 Task 2**

```bash
git add app/services/annotations.py tests/test_annotations.py docker-compose.yml README.md
git commit -m "feat: bound character analysis time"
```

---

### Task 3: ARAP 丢 pin 与动作帧数诊断

**Files:**
- Modify: `app/services/render_scene.py`
- Modify: `tests/test_render_batch.py`

**Interfaces:**
- Consumes: `_logged_animated_drawing_class()`、vendor `char_cfg.skeleton` 的归一化 `{name, loc}`、`arap.pin_mask`、controller `frames_rendered`、drawing 当前 `retargeter.bvh.frame_max_num`。
- Produces: `_log_dropped_pins(drawing) -> None` 和 `_gif_frame_count(path: Path) -> int` 诊断 helper；不改变 `render_animations()` 返回类型。

- [ ] **Step 1: 写丢 pin 诊断失败测试**

扩展 `test_logged_animated_drawing_logs_mesh_before_arap_and_actual_matrices_after` 的 fake ARAP，使 `pin_mask=np.array([True, False])`，骨架包含 `name`、`loc`。断言 WARNING 包含 `dropped=1/2`、被丢弃的关节名和坐标。另加字段缺失 fake，断言构造成功且只记录诊断失败 WARNING。

- [ ] **Step 2: 运行 pin 测试确认 RED**

Run: `.venv/bin/pytest -q tests/test_render_batch.py -k 'dropped_pin'`

Expected: FAIL，因为当前仅记录 effective pin 数。

- [ ] **Step 3: 实现防御式 pin 日志**

`_log_dropped_pins()` 用 `zip(skeleton, pin_mask)` 找出 false 项并输出一条 WARNING。整个诊断 helper 自己捕获 `AttributeError`、`KeyError`、`TypeError`、`ValueError`，记录说明后返回；不得吞掉 `super().__init__()` 的真实构造异常。坐标保持 vendor 当前归一化值，不反推像素，以免伪造精度。

- [ ] **Step 4: 写帧数一致性失败测试**

扩展 lifecycle fake：drawing 暴露 `retargeter.bvh.frame_max_num`，controller 可配置 `frames_rendered`，fake writer 使用 Pillow 写出实际 GIF。分别覆盖：三者相同时 INFO；声明/渲染为 8、GIF 因重复帧实际为 7 时 WARNING 包含三个数值和“编码器可能合并连续重复帧”。

- [ ] **Step 5: 运行帧数测试确认 RED**

Run: `.venv/bin/pytest -q tests/test_render_batch.py -k 'frame_count'`

Expected: FAIL，因为当前没有写盘后 GIF 帧数对照。

- [ ] **Step 6: 实现动作级帧数日志**

使用 Pillow `Image.open()` 与 `seek()` 或 `n_frames` 读取写盘后的 GIF 实际帧数。每个 controller 完成且确认文件存在后，立即读取当前 drawing 的 BVH 声明帧数、`controller.frames_rendered` 和 GIF 帧数；一致 INFO，不一致 WARNING。若 GIF 无法读取，保留现有失败语义，不将损坏产物加入返回值。

- [ ] **Step 7: 运行 Task 3 测试与像素回归**

Run: `.venv/bin/pytest -q tests/test_render_batch.py tests/test_render_batch_pixels.py tests/test_character_pipeline_timing.py`

Expected: PASS，像素与动作顺序契约不变。

- [ ] **Step 8: 提交 Task 3**

```bash
git add app/services/render_scene.py tests/test_render_batch.py
git commit -m "feat: diagnose render pin and frame loss"
```

---

### Task 4: Uvicorn access log 降噪

**Files:**
- Modify: `docker-compose.yml`
- Modify: `tests/test_torchserve_config.py`

**Interfaces:**
- Consumes: API 服务现有 `app.main.log_request` 中间件。
- Produces: API Uvicorn 命令包含 `--no-access-log`，不影响项目中间件业务日志。

- [ ] **Step 1: 写 Compose 契约失败测试**

在 `tests/test_torchserve_config.py` 增加 `test_api_disables_duplicate_uvicorn_access_log()`，加载 compose 并断言 `services.api.command` 含 `--no-access-log`，同时读取 `app/main.py` 或通过 TestClient/caplog 断言普通 `/v1/...` 请求仍由 `app.main` 记录。

- [ ] **Step 2: 运行测试确认 RED**

Run: `.venv/bin/pytest -q tests/test_torchserve_config.py::test_api_disables_duplicate_uvicorn_access_log`

Expected: FAIL，因为 API 命令当前未包含该参数。

- [ ] **Step 3: 修改 Compose 命令**

在 API 的 Uvicorn command 参数列表加入：

```yaml
- --no-access-log
```

不删除 `app.main.log_request`，不调整其 quiet path 列表。

- [ ] **Step 4: 运行 Task 4 测试**

Run: `.venv/bin/pytest -q tests/test_torchserve_config.py tests/test_logging_setup.py`

Expected: PASS。

- [ ] **Step 5: 提交 Task 4**

```bash
git add docker-compose.yml tests/test_torchserve_config.py
git commit -m "chore: suppress duplicate uvicorn access logs"
```

---

### Task 5: 全量验证与静态配置检查

**Files:**
- Verify only; do not modify production behavior unless a failing regression proves a scoped defect.

**Interfaces:**
- Consumes: Tasks 1-4 的全部提交。
- Produces: 全量测试和 Compose 解析证据。

- [ ] **Step 1: 运行全量测试**

Run: `.venv/bin/pytest -q`

Expected: PASS；如有依赖真实外部服务的既有测试，应按其既有约定 SKIP，而不是修改测试掩盖失败。

- [ ] **Step 2: 校验 Compose 配置**

Run: `docker compose config --quiet`

Expected: exit 0。

- [ ] **Step 3: 检查差异质量**

Run: `git diff --check HEAD~4..HEAD`

Expected: exit 0，无空白错误。确认 `vendor/AnimatedDrawings`、任务状态契约和 `force=true` 无改动。

- [ ] **Step 4: 汇总验证结果**

报告全量测试通过/跳过数量、Compose 校验结果，以及未执行真实 Docker 性能复测的原因：本轮明确不重建或重启容器。

