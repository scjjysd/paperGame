# 关卡解析服务端设计（Level API v1 落地）

> 日期：2026-09-08
>
> 状态：已批准（对应 P0 任务 4+5：关卡视觉解析 + 可玩性校验）
>
> 依据：`level-image-to-json-api.md`（协议，唯一真源）+ `level-image-to-json-server-handoff.md`（交接文档）

## Context 清单

- 已读取：`level-image-to-json-api.md` — Level API v1 协议：五态状态机、响应信封、level JSON Schema、幂等规则、错误码
- 已读取：`level-image-to-json-server-handoff.md` — 服务端交接：OpenCV/LLM 职责边界、处理流水线、验收指标、P0-P3 实施阶段
- 已读取：`C1Levels/level1.json` + `level1-background.png` — Unity 侧在用的静态样例（人工校准的 7 条平台 + 75×90 终点区域，1245×810），作为黄金真值
- 已读取：`server/app/contracts.py`、`app/api/characters.py`、`app/services/job_store.py`、`app/workers/character_worker.py`、`app/workers/render_runner.py` — 需要镜像的既有异步任务模式
- 适用硬约束：vendor 零改动；Python 3.9（`Optional[X]` 而非 `X | None`）；依赖钉死不升级（numpy 1.24.4 / Pillow 10.1.0 / opencv 4.6.0.66）；`app/` 禁止 import `scripts/`；跨进程路径绝对化；阈值常数须注明验证样本与重标定条件
- 影响模块：`app/main.py`（挂路由）、`app/services/job_store.py`（参数化）、新增 `app/level_contracts.py`、`app/api/levels.py`、`app/services/level_*.py`、`app/services/playability.py`、`app/workers/level_worker.py`、`app/workers/level_runner.py`、`docker-compose.yml`（levels worker 服务）
- 待确认事项：无（关键决策均已确认，见下节）

## 1. 已确认决策

| 决策点 | 结论 |
|---|---|
| 实施范围 | P0+P1+P2 全量（异步任务+拉正+平台检测+旗帜+出生点+可玩性）；P3 生产化（鉴权/限流/对象存储/隐私删除）不在本次 |
| 纸张拉正 | 在本仓库用 OpenCV 重写（Unity 仓库的 `Tools/PaperLevel/` 不搬代码），按交接文档 7.2 基线流程 |
| 测试样本 | 合成样本（固定 seed 脚本生成，真值由构造给出）进 CI；`C1Levels/level1-background.png`+`level1.json` 作黄金真值对拍 |
| LLM 语义复核 | 直接接入，OpenAI 兼容端点（base_url/api_key/model 全环境变量），不可用时降级纯 OpenCV |
| 任务基础设施 | 方案 A：JobStore 参数化（queue_key/terminal_states/rerun_artifacts，默认值=角色现状），其余平行镜像角色链路 |

## 2. 总体架构与数据流

```
Unity/浏览器                      api 容器（同一 FastAPI 进程，新增 /v1/levels 路由）
    │ POST /v1/levels ────────────▶ 魔数+大小+尺寸+EXIF 校验
    │                              jobId = level_<sha256(文件字节+规范化参数+算法主版本)[:12]>
    │ ◀── 202 {jobId,statusUrl} ── LPUSH pq:levels
    │
level_worker 容器（复用 api 镜像，独立进程）BRPOP pq:levels
    │  spawn 子进程 python -m app.workers.level_runner <job_dir>
    │  退出码协议同角色：0 = 业务终态已写 result.json；非 0 = 基础设施故障，重试 1 次后 failed
    │  子进程超时 90s（纯 CPU 检测，无渲染方差），超时/崩溃可硬杀，隔离 OpenCV 原生段错误
    │
level_runner（子进程）调 level_parser.parse()：
    上传复检 → 拉正 → 平台检测 → 旗帜检测 → LLM 语义复核 → 出生点推断 → 几何校验 → 可玩性
    → 写 result.json（五态之一）+ 产物
    │
Unity 轮询 GET /v1/levels/{jobId} → Redis Hash（TTL 24h）→ 过期后 result.json 快照重建
    │ GET /artifacts/{jobId}/rectified.png 等
```

与角色链路三处刻意不同：

1. 子进程的理由是超时硬杀 + OpenCV C 层崩溃隔离（不是 chdir/GLFW——关卡解析无此问题，但保持同构最省心且防御真实风险）；
2. 幂等键掺入 `playabilityProfile`（协议 13 节：它影响最终状态，参与哈希）；
3. worker 超时 120s → 90s。

## 3. 识别管线

门面 `level_parser.parse(job_dir) -> dict`（写 result.json 的载荷），内部按序调用五个模块，各模块单一职责、文件交接、可独立重跑：

| 模块 | 职责 | 产物 |
|---|---|---|
| `level_rectify.py` | 纸张四角检测（缩放副本做轮廓+四边形拟合，比例还原回原图）→ 角点排序 TL/TR/BR/BL → 原图坐标透视矩阵 → 输出拉正图 → 按纸张长边定横屏方向（方向置信度不足 → needs_review） | `rectified.png`、`transform.json`、`paper-mask.png` |
| `level_detect.py` | 拉正图灰度化+局部光照补偿 → black-hat/自适应阈值 → 水平结构元形态学开运算 → 候选线段（角度/长度/宽高比/墨迹覆盖率过滤）→ 共线碎片保守合并 → 端点与中心线精修；旗帜候选（红 HSV 区域 + 近竖线 + 相邻轮廓） | `ink-mask.png`、platformCandidates、goalCandidates |
| `level_semantic.py` | OpenAI 兼容端点复核候选：输入=叠加编号的降采样拉正图 + 候选列表 + 严格 JSON Schema；输出=lineClassifications/goalClassifications/sceneIssues/decision | 分类结果 + 审计记录（模型名/提示词版本/原始响应落盘） |
| `level_parser.py`（门面部分） | 出生点推断（画布下半区最左可容纳角色宽度的平台，X 左端内缩安全距离，Y 平台中心线，`source: inferred`）+ 几何校验（画布边界/平台非零长且 `start.x<=end.x`/id 唯一/最低墨迹覆盖率/高度重叠去重/背景尺寸==画布） | `level.json`（仅可发布时）、`overlay.png` |
| `playability.py` | 平台间有向可达图（maxJumpRisePixels/maxJumpDistancePixels/landingTolerancePixels vs 端点几何）→ 从出生平台到终点平台是否可达 + 警告码 | `analysis.json` |

### 语义复核降级链（交接文档 6.3）

- LLM 未配置（无环境变量）或调用失败 → 纯 OpenCV 几何判定，照常出 ready/needs_fix；
- 旗帜语义置信度不足或多个候选接近 → `needs_review(AMBIGUOUS_GOAL)`，绝不猜；
- 纸张拉正失败 → `needs_review(PAPER_NOT_FOUND / PAPER_AMBIGUOUS / PAPER_OCCLUDED)`，不得让 LLM 猜透视坐标；
- 无平台 → `needs_review(NO_PLATFORM_DETECTED)`。

LLM 只能从 OpenCV 候选 ID 中选择分类，不能产出/移动/延长任何坐标。服务端对 LLM 响应做 Schema 校验，失败视同「不可用」走降级链，不做自然语言正则提取。

## 4. 契约、状态与幂等

### 4.1 level_contracts.py

- Pydantic 模型镜像协议附录 A：`schemaVersion/coordinateSystem/canvas/background/playerStart/platforms/goalRegion`，含 JSON Schema 无法表达的跨字段校验（全部坐标在画布内、`background.width==canvas.width`、`start.x<=end.x` 且两端点不同、平台 id 唯一）；
- 响应信封五态模型（ready/needs_fix/needs_review/failed + progress）；
- `algorithmVersion = 'level-parser-1.0.0'`（任何改变拉正/端点/状态判定的更新必须升版本）；
- 错误码实现协议 11 节无鉴权子集：INVALID_REQUEST / INVALID_PLAYABILITY_PROFILE / FILE_TOO_LARGE / UNSUPPORTED_IMAGE_FORMAT / IMAGE_DECODE_FAILED / JOB_NOT_FOUND / QUEUE_UNAVAILABLE / INTERNAL（401/403/429 属 P3）。

### 4.2 状态存储（JobStore 参数化）

`JobStore.__init__` 新增三个参数，默认值即角色现状（角色侧零行为变化）：

```python
queue_key: str = 'pq:characters'
terminal_states: frozenset = TERMINAL_STATES            # 角色: ready/needs_correction/failed
rerun_artifacts: tuple = RERUN_ARTIFACTS                # 角色: result.json/run.png/... /anno
```

levels 侧传：`queue_key='pq:levels'`、`terminal_states={'ready','needs_fix','needs_review','failed'}`、`rerun_artifacts=('result.json','rectified.png','paper-mask.png','ink-mask.png','overlay.png','level.json','analysis.json','transform.json','llm-audit.json')`（`input.png` 不在清理之列——同 jobId 必为同文件字节，API 上传时覆写）。

`jobId = 'level_' + sha256(文件字节 + 规范化 playabilityProfile JSON + 算法主版本)[:12]`。参数 JSON 按固定键序+数值格式规范化后再入哈希。

### 4.3 progress

runner 各阶段直连 Redis（REDIS_URL 环境变量）写 `stage` 字段到 job Hash；API 的 queued/processing 响应带 `progress.stage`（协议 5.2 九个值），`percent` 不实现——协议已声明「客户端不得依赖完整枚举」，`ponytail:` 标注天花板。

### 4.4 force

与角色一致：reset 清旧终态+产物，状态回 queued 重新入队。协议 13 节「历史结果不得静默覆盖」在 v1 单机文件存储下不成立——`ponytail:` 标注，版本化归档留给 P3 对象存储。

### 4.5 上传校验（API 层）

魔数判 JPEG/PNG（复用 characters 的 `_sniff` 模式）→ 大小 ≤10MiB → Pillow 解码 → EXIF 方向应用后重存 → 单边 800~12000px、总像素 ≤40M → 违例分别返回 415/413/422。`playabilityProfile` 字段校验（缺必填/越界 → 400 INVALID_PLAYABILITY_PROFILE）。

## 5. LLM 集成

环境变量：`LEVEL_LLM_BASE_URL`、`LEVEL_LLM_API_KEY`、`LEVEL_LLM_MODEL`。三者齐全才启用；任一缺失 → 降级链。

- 协议：OpenAI Chat Completions 兼容（图片走 `image_url` base64 data URI），HTTPS；
- 提示词模板按协议 16.3（候选分类器，非设计师；禁止输出坐标）；
- `temperature` 低值（0.1）；
- 审计：模型名、提示词版本、原始结构化响应写 `llm-audit.json`；
- 依赖：容器内已有 `requests`（TorchServe 生态）；不新增依赖。

## 6. 产物布局（job_dir）

```
out/jobs/{level_jobId}/
├── input.png              原始上传（EXIF 处理后）
├── rectified.png          拉正背景（契约产物，Unity 直接用）
├── paper-mask.png         纸张检测遮罩（调试）
├── ink-mask.png           笔迹二值图（调试）
├── overlay.png            检测叠加图（细线+编号，1~2px 线宽，调试专用）
├── level.json             v1 权威关卡数据（仅 ready/needs_fix 生成）
├── analysis.json          置信度/警告/算法版本/可玩性诊断
├── transform.json         透视矩阵与尺寸
├── llm-audit.json         LLM 审计（未启用则无此文件）
└── result.json            响应信封载荷（状态机真源）
```

## 7. 测试与验收

1. **合成样本**：`scripts/gen_level_samples.py` 固定 seed，白纸+黑横线+红旗 → 透视变换+旋转+光照梯度扰动；真值（平台端点/旗帜区域/期望状态）由构造直接给出。10 张入库 `testdata/levels/synthetic/` + 真值 JSON，CI 离线可跑（无 TorchServe/Redis 依赖的检测层测试直接吃图片）。
2. **黄金真值对拍**：`C1Levels/level1-background.png` + `level1.json`——拉正图直接喂检测段（跳过拉正），7 条平台端点中位误差 ≤3px（交接 13.2：≤3px 或短边 0.4% 取大者）。
3. **单测**（每模块一个文件，fakeredis/合成图）：
   - rectify：合成透视图恢复角点误差；
   - detect：端点误差/漏检误检分别统计/碎线合并/相邻平行线不粘连；
   - goal：红旗检出/多旗歧义→needs_review/无旗→GOAL_NOT_FOUND；
   - semantic：LLM mock（可配置注入假客户端）+ 降级链三条路径 + Schema 校验拒绝坏响应；
   - playability：六个警告码逐一 + 可达/不可达/识别不确定三分支；
   - contracts：六份固定样例过 Schema + 跨字段校验拒绝非法样例。
4. **API/worker 测试**：镜像 `test_characters_api`/`test_character_worker` 模式——上传各违例分支/幂等（同图同参数同 jobId、不同参数不同 jobId）/force 重跑/五态轮询/退出码协议/超时重试一次。
5. **端到端**：`scripts/smoke_levels_e2e.sh` 镜像角色冒烟（健康检查→上传→轮询→验产物+Schema→幂等复验），通过输出 `SMOKE_LEVELS_E2E_PASS`。
6. 契约共享样例六份放 `testdata/levels/contracts/`（ready/needs-fix/needs-review/failed/invalid-out-of-bounds/invalid-background-size），联调时同步 Unity。

### 阈值标定约定

检测阈值常数（角度容差/最小长度/墨迹覆盖率/合并间隙等）在合成样本集 + 黄金真值上标定，每处注释写明「为什么是这个值、在什么样本上验证过、什么条件下重标定」（CLAUDE.md 代码约定），`ponytail:` 标注天花板与升级路径。换样本集必须重标定（同 annotation_repair 惯例）。

## 8. 明确不做（v1 边界，照协议第 19 节）

- 不修改/优化/补齐用户草图几何；
- 不做鉴权、限流、CORS 策略、日志脱敏、隐私删除周期（P3）；
- 不做多页拼接/曲线/移动平台/斜坡物理；
- 不做独立可玩性重分析接口（协议明确排除）；
- 不做历史结果版本化归档（P3 对象存储）。

## 9. 实施顺序（供 writing-plans 参考）

1. JobStore 参数化（角色测试全绿回归）；
2. level_contracts + 契约样例 + contracts 测试；
3. api/levels.py 上传校验+查询 + main.py 挂路由 + API 测试；
4. level_rectify + 合成样本生成器 + 测试；
5. level_detect（平台+旗帜候选）+ 测试 + 黄金真值对拍；
6. level_semantic（LLM+降级链）+ mock 测试；
7. level_parser 门面（出生点+几何校验+overlay）+ 测试；
8. playability + 测试；
9. level_worker/level_runner + docker-compose 服务 + worker 测试；
10. smoke_levels_e2e + README/CLAUDE.md 进度更新。
