# 纸上游戏机（Paper Game Console）

> 孩子在纸上画一个火柴人和一张带横线、红旗的关卡，拍照上传；服务端把涂鸦变成会跑会跳的角色动画，Unity 里让这个角色跑到红旗终点。

本文件是仓库的**全局导览**：读完可以知道项目在做什么、代码在哪、怎么跑起来、当前进度到哪、下一步做什么。细节规格与验收记录见文末[文档索引](#文档索引)。

---

## 1. 项目背景与定位

| 项 | 内容 |
|---|---|
| 产品形态 | Unity WebGL 小游戏 + Python AI 服务 |
| P0 目标 | 涂鸦主角 → run/jump 精灵表；纸张关卡 → 平台/旗帜坐标；浏览器里跑到旗帜结算 |
| 核心卖点 | 「纸变数据」：不做关卡编辑器，孩子的画就是关卡本身 |
| 技术底座 | facebookresearch/AnimatedDrawings（MIT，已归档、行为冻结）自部署 |

**已锁定的 P0 语法**（改动需前后端共同确认）：

- 主角必须是正面、完整、双臂双腿的类人形涂鸦。
- 关卡纸为横版 A4、四角定位标记；黑色横线（≥4cm）是地面，红色旗帜是唯一终点。
- 起点不是第三种绘制元素：角色固定从画面左下方第一块可站立地面开始。
- 动作只有 `run`、`jump`；服务输出**透明 PNG 精灵表**，Unity 按帧播放，运行时不播 GIF/MP4。
- 唯一成功条件：角色接触旗帜。不可达则禁止开始并给出纸面修改提示。

---

## 2. 当前进度快照

截至 `feat/level-parser` 工作树（2026-09-10，关卡链路尚未完成 Docker 实机冒烟）：

| P0 任务 | 内容 | 状态 |
|---|---|---|
| 任务 1 | 契约、样本、验收基线 | ✅ 服务端侧完成（`app/contracts.py` + `testdata/characters/s01-s20.png`）；Unity 侧 `GameContracts.cs` 未建 |
| 任务 2 | AnimatedDrawings 可行性尖刺 | ✅ **验收通过**：20/20 成功，单张最大 18.1s（门槛 ≥16 成功、≤60s）。画质经两轮返工：先补标注修复（第 ⑥ 节），再把动画驱动从三维动捕投影换成按画合成二维（第 ⑦ 节）——现首帧掰动 0.00°、全图丢失中位 0.9%、形状保真 19/20 |
| 任务 3 | 服务端角色异步接口 | ✅ **完成并冒烟通过**：4 容器全栈、46 用例全绿、端到端 `SMOKE_E2E_PASS` |
| 任务 4 | 服务端关卡视觉解析 | ✅ 已落地：A4 拉正、OpenCV 平台/红旗候选、可选 LLM 候选复核、异步 Level API v1 |
| 任务 5 | 服务端可玩性校验 | ✅ 已落地：出生点/终点承载、跳跃图可达性、`ready`/`needs_fix`/`needs_review` 诊断；真实 Docker 冒烟待补 |
| 任务 6 | Unity 主角生成与缓存 | ⬜ 未开始（**仓库尚无 `client-unity/` 目录**） |
| 任务 7 | Unity 关卡拍摄与游玩闭环 | ⬜ 未开始 |
| 任务 8 | 集成验收与兜底 | 🟡 部分：角色链路端到端冒烟已做；全链路（含关卡）未做 |

一句话概括：**服务端角色动画与关卡解析/可玩性代码均已落地；关卡链路离线回归已执行，Docker 实机冒烟仍受环境阻塞，Unity 客户端尚未动工。**

---

## 3. 一分钟理解核心链路

```
character.png（涂鸦照片）
   │
   ├─① 分析   annotations.analyze()
   │          → vendor image_to_annotations → HTTP POST TorchServe:8080
   │          → mask.png / texture.png / char_cfg.yaml（16 关节骨架）
   │          失败 → 抛 NeedsCorrection(NO_HUMANOID / NO_SKELETON / MULTIPLE_SKELETONS / NO_CONTOUR)
   │
   ├─①.5 修复 annotation_repair.repair_or_reject()
   │          ① 重裁：检测框常没框住整张画（s07 有 42% 墨迹在框外）。三个候选按全图丢失率
   │             择优——按墨迹真实包围盒重裁 / 按框尺寸外扩 15% / 原框；仅当丢失率严格更低
   │             时入选，且网格连通性不得差于 vendor 原始标注，否则自动回退
   │          ② 重建 mask：把「只留最大轮廓」丢弃的部件补回，桥接到主体并强制单连通
   │          ③ 关节吸附：mask 外的关节吸附到中轴，消除 ARAP 丢 pin
   │          ④ 末端外推：手/脚控制点沿肢体方向推到墨迹末端（没有 pin 的那截会被网格乱拖）
   │          ⑤ 门禁：【吸附前】关节偏移 > 25% 对角线 → NeedsCorrection(SKELETON_MISFIT)
   │          ⑥ 降级阶梯：网格连通性不得差于 vendor 原始标注，否则逐级回退（防死锁）
   │
   ├─①.7 合成 motion_2d.synth_motion()
   │          按这张画自己的骨架现场生成纯二维 BVH（每张画一份，不用外部动捕资产）
   │          → 首帧＝原画姿态（20 份样本实测偏离 0.00°），后续帧只叠加小幅摆动
   │          → 走 35°+膝弯 16°/10 帧、跳 18°+腾空 14% 身高/7 帧，非人形自动压到 1/3 幅度
   │
   ├─② 渲染   render_scene.render_animation()
   │          → 生成 scene YAML（character_cfg + 上一步的 motion_cfg + retarget flat2d）
   │          → os.chdir(vendor) → animated_drawings.render.start()
   │          → run.gif（10 帧）/ jump.gif（7 帧），RGBA 透明
   │
   └─③ 拼表   sprite_sheet.build_sprite_sheet()
              → 逐帧 convert('RGBA') → 所有帧内容包围盒并集 → 横向拼接
              → run.png / jump.png + 元数据（frameCount/fps/frameWidth/frameHeight/footAnchor）
```

①② 是 AnimatedDrawings 现成能力（**只配置、不改 vendor 源码**），①.5 / ①.7 / ③ 是本项目自写代码。各段以文件交接，任一步可独立重跑。

> **①.7 为什么必须存在**：三维动捕的起始姿势（立正站好、手垂身侧）与孩子画的姿势（手举高、腿叉开）对不上，ARAP 在第 0 帧就要把肢体平均掰 **64°**（最狠 179°，等于把手臂对折），画被横向压扁到 50%、手臂压进躯干——这就是「跳的手没了」「跳为什么会变形」的来源。换任何固定动捕资产最好也只能降到 34°（20 张画姿态各异）。改为按画合成后首帧掰动恒为 0°，摆幅由配方直接给定。详见 [`docs/animation-spike-results.md`](docs/animation-spike-results.md) 第 ⑦ 节。

> **①.5 为什么必须存在**：vendor 侧有五道叠加缺陷——**D0** 检测框把画切了（框外墨迹根本不进 `texture.png`）；**D1** `segment()` 只保留最大连通轮廓；**D2** `_load_txtr()` 把 mask 之外的纹理像素强制 `alpha=0`；**D3** `_generate_mesh()` 建网格时又只取最长轮廓；**D4** 落在三角面之外的关节被 ARAP 当作 pin 永久丢弃。后果：与身体断开的部件（天使头环、整块头部）在成片里彻底消失；pose 模型把关节估到体外时（s20 的 `neck` 距 mask 96.9px）ARAP 用悬空骨骼驱动网格，产生严重剪切变形。完整证据链、修复数据与实施中踩到的五个坑见 [`docs/animation-spike-results.md`](docs/animation-spike-results.md) 第 ⑥ 节。

**服务化后**，这条链路被包在异步任务里：

```
Unity/浏览器                     api 容器                    redis              worker 容器
    │  POST /v1/characters  ───────▶│                         │                     │
    │  ◀───── 202 {jobId} ──────────│  LPUSH pq:characters ──▶│                     │
    │                               │                         │◀── BRPOP ───────────│
    │                               │                         │      置 processing   │
    │                               │                         │   spawn 子进程渲染 ──┼─▶ render_runner
    │  GET /v1/characters/{jobId} ─▶│  HGETALL job:{jobId} ──▶│◀─ 写终态 result.json │   （chdir/GLFW 隔离）
    │  ◀── queued|processing|ready|needs_correction|failed ───│                     │
    │  GET /artifacts/{jobId}/run.png ─▶ StaticFiles(./out/jobs)                    │
```

---

## 4. 仓库结构导览

```text
jjhks/
├── README.md                        ← 本文件
├── AGENTS.md                        AI 开发规范
├── CLAUDE.md                        Claude 引导
├── API.md                           Unity 客户端接口文档
├── level-image-to-json-api.md       关卡 API v1 协议
├── level-image-to-json-server-handoff.md  服务端交接文档
├── pyproject.toml                   Python 项目配置（依赖 + pytest）
├── conftest.py                      根级 pytest 配置
├── docs/
│   ├── animation-spike-results.md   ★ 任务 2 验收记录：20 样本逐张耗时、残留项清单
│   ├── async-service-smoke-results.md ★ 任务 3 冒烟与回归结论
│   └── superpowers/
│       ├── specs/                   设计规格（管线 / 异步服务化 / 关卡解析）
│       └── plans/                   实现计划（含 TDD 步骤与已核实的接口事实）
├── testdata/
│   ├── characters/                  ★ s01-s20.png 验收样本（已入库，见下方授权说明）
│   └── levels/                      关卡样本
│       ├── synthetic/               合成样本（CI 离线测试）
│       ├── contracts/               契约样例（6 份 JSON）
│       └── golden/                  黄金真值（Unity 侧对拍，原 C1Levels）
├── app/                             ★ 全部服务端应用代码
│   ├── main.py                      FastAPI 应用工厂：CORS + 中文日志中间件 + 路由 + /artifacts 静态挂载 + /healthz
│   ├── log.py                       日志基础设施：中文格式、out/logs 按日期拆分、接管 uvicorn 英文访问日志
│   ├── contracts.py                 ★ Pydantic 契约：五态、错误码 + 中文文案、六键动画元数据、derive_job_id
│   ├── level_contracts.py           关卡契约：LevelReady/NeedsFix/NeedsReview/Failed + 错误码/状态/阶段中文文案
│   ├── api/                         路由层
│   │   ├── characters.py            角色上传与轮询
│   │   ├── levels.py                关卡上传与轮询
│   │   ├── character_view.py        角色审查视图 /detail + /view
│   │   ├── level_view.py            关卡审查视图 /detail + /view（叠画平台/出生点/终点/告警）
│   │   ├── errors.py                中文错误体：角色扁平体 / 关卡信封体 / 框架级双键体
│   │   ├── urls.py                  站内相对路径 → 完整地址（PUBLIC_BASE_URL 优先）
│   │   └── review_html.py           两个审查页共用的样式与拼装函数
│   ├── services/
│   │   ├── annotations.py           ① vendor 包装层：异常 → NeedsCorrection
│   │   ├── annotation_repair.py     ①.5 扩框重裁 + mask 重建 + 关节吸附 + 门禁 + 网格降级阶梯
│   │   ├── motion_2d.py             ①.7 按画合成纯二维 BVH（首帧掰动 0°）
│   │   ├── render_scene.py          ② 场景 YAML 生成 + 渲染；Mesa 开关；motion 路径自愈
│   │   ├── sprite_sheet.py          ③ GIF → 透明 PNG 精灵表 + footAnchor
│   │   ├── character_pipeline.py    ★ 管线门面 render() / render_character()
│   │   ├── job_store.py             Redis Hash(TTL 24h) + List 队列 + result.json 兜底
│   │   ├── level_rectify.py         A4 纸张拉正（OpenCV 透视变换）
│   │   ├── level_detect.py          平台/红旗候选检测
│   │   ├── level_semantic.py        可选 LLM 语义复核
│   │   ├── level_parser.py          关卡解析门面
│   │   └── playability.py           可玩性校验：出生点/终点承载、跳跃图可达性
│   ├── workers/
│   │   ├── character_worker.py      BRPOP 主循环：超时 120s、重试 1 次、SIGTERM 优雅退出
│   │   ├── render_runner.py         子进程入口：渲染 → 搬运产物 → 写 result.json（退出码协议）
│   │   ├── level_worker.py          关卡队列消费
│   │   └── level_runner.py          关卡解析子进程入口
│   └── assets/motions/              ★ run.bvh/run.yaml、jump.bvh/jump.yaml + README（来源与调参结论）
├── scripts/                         验证脚本区（可 import app；反向禁止）
│   ├── setup/                       环境设置（首次必做）
│   │   ├── setup-vendor.sh          幂等克隆 AnimatedDrawings 到 vendor/
│   │   └── setup_env.sh             建宿主 .venv + pip install -e vendor
│   ├── smoke/                       冒烟测试（端到端验证）
│   │   ├── smoke_e2e.sh             ★ 全栈端到端冒烟（上传→轮询→下载验 PNG→幂等复验）
│   │   ├── smoke_levels_e2e.sh      关卡全栈冒烟
│   │   └── render_smoke.py          容器内 Mesa 渲染 go/no-go 尖刺
│   ├── tools/                       可复用工具
│   │   ├── decimate_bvh.py          BVH 抽稀
│   │   └── gen_level_samples.py     关卡合成样本生成（固定 seed）
│   ├── diag/                        诊断/验收（人工审查）
│   │   ├── diagnose_annotations.py  ★ 标注质量验收：七项指标（`--render` 含实渲）
│   │   ├── review_batch.py          批量渲染审查页（人工目视验收）
│   │   └── compare_projection.py    投影面对比动图（评估用）
│   └── archive/                     归档（一次性/历史）
│       └── download_samples.py      官方示例涂鸦下载（正式样本已入库，仅留档）
├── tests/                           测试用例（pytest，常规回归排除 test_spike_batch.py）
├── docker/
│   └── entrypoint.sh                socat 把容器内 localhost:8080 转发到 torchserve:8080
├── Dockerfile                       api 与 worker 共享镜像（python:3.9-slim + OSMesa）
├── docker-compose.yml               ★ 5 容器编排：torchserve / redis / api / worker / level-worker
├── requirements-service.txt         镜像内运行时依赖（vendor 子集，不含 torch，省 ~800MB）
├── requirements-dev.txt             宿主开发/测试依赖
├── vendor/                          AnimatedDrawings 源码（.gitignore，由 setup-vendor.sh 拉取）
└── out/                             产物与中间文件（.gitignore）：jobs/ logs/ spike/ mesa-spike/ ...
    └── logs/                        中文日志，按组件与日期拆分：api-2026-09-10.log、level-worker-…
```

标 ★ 的是最该先读的文件。

**样本授权**：`s01-s07` 来自 AnimatedDrawings 官方示例（MIT）；`s08-s20` 由 Google Quick, Draw! 公开数据集（CC BY 4.0）`angel`/`yoga` 类别的矢量笔画渲染而成。属**成人简笔画代理样本**，产品验收前仍建议用真实儿童画（家长授权）复测。

---

## 5. 服务端架构要点

### 5.1 容器拓扑

| 容器 | 镜像 | 端口 | 职责 | 关键配置 |
|---|---|---|---|---|
| `ad-torchserve` | `paper-game/ad-torchserve:local` | 8080 推理 / 8081 管理 | 涂鸦检测、分割、骨架估计 | 内存上限 16GB；python urllib 探活，`start_period: 60s`（模型加载慢） |
| `pg-redis` | `redis:7-alpine` | — | 队列 + 任务状态 | `redis-cli ping` 健康检查 |
| `pg-api` | `paper-game/server:local` | 8000 | 上传、轮询、静态伺服产物、审查页 | `uvicorn app.main:app`；卷 `./out → /data/out`；`PUBLIC_BASE_URL` / `CORS_*` / `LOG_*` |
| `pg-worker` | 同 api 镜像 | — | 消费队列、子进程渲染 | `RENDER_USE_MESA=true`；`stop_grace_period: 300s`（≥120s×2 次尝试，防 docker 默认 10s SIGKILL 打断渲染） |

api 与 worker **分开部署**：故障域隔离、可独立 restart、可 `--scale worker=N` 伸缩；共享镜像使边际成本仅一份基础运行时内存。

`entrypoint.sh` 里的 socat 转发是必需的：vendor 的 `image_to_annotations.py` 把 TorchServe 地址**硬编码为 `http://localhost:8080`**，而项目纪律是**零改动 vendor**，因此在容器内把 localhost:8080 转发到 `torchserve:8080`。

### 5.2 队列与状态机

- 队列：Redis List `pq:characters`，`LPUSH` 入队 / `BRPOP` 消费，**worker 并发 = 1**（规格锁定，不引入 Celery/RQ）。
- 状态：Redis Hash `job:{jobId}`（`status` / `result` / `updatedAt`），TTL 24h。
- 兜底：`out/jobs/{jobId}/result.json` 为终态快照；Redis 过期后 API 由快照重建响应；快照损坏视同不存在（返回 404，客户端可重传）。
- 状态机：`queued → processing → { ready | needs_correction | failed }`，**终态不可被覆盖**（`job_store.set_status` 内防御）。

### 5.3 worker 韧性设计

| 机制 | 取值 | 原因 |
|---|---|---|
| 子进程执行渲染 | `python -m app.workers.render_runner <job_dir>` | `os.chdir(VENDOR)` 是进程级状态，在 FastAPI 并发下有竞态；GLFW/OpenGL 上下文偶发初始化全挂（尖刺实测过一次 20/20 全失败，重跑即恢复）只损失当前任务 |
| 子进程超时 | 120s | 尖刺实测单任务 ≤40s 的 3 倍余量 |
| 最大尝试次数 | 2（即重试 1 次） | 仅基础设施故障重试；业务失败（`needs_correction`）不重试 |
| 退出码协议 | 0 = 业务终态已写 `result.json`；非 0 = 基础设施故障 | 让主循环能区分「渲染判定为无人形」和「渲染进程崩了」 |
| 错误码映射 | 超时 → `RENDER_TIMEOUT`；崩溃/result.json 损坏 → `RENDER_CRASHED`；动作资产缺失 → `ASSET_MISSING` | 客户端据稳定错误码分支处理 |

### 5.4 产物存储布局

```text
out/jobs/{jobId}/
├── input.png        上传原图
├── anno/            mask.png / texture.png / char_cfg.yaml（needs_correction 时供骨架确认页用）
├── run.png          run 精灵表（3107×339 RGBA，13 帧）
├── jump.png         jump 精灵表（2868×339 RGBA，12 帧）
└── result.json      终态快照
```

当前由 api 容器本地静态目录 `/artifacts` 伺服；**演示阶段计划升级为对象存储 + 签名 URL**（WebGL CORS 兼容、多实例），接口层已用 URL 抽象隔离，届时不改 API 形状。

关卡任务复用同一目录，按终态发布以下文件：

```text
out/jobs/{levelJobId}/
├── input.png          EXIF 归一化后的上传图
├── request.json       实际 playabilityProfile 与任务时间戳
├── rectified.png      拉正后的权威背景
├── overlay.png        候选、路径与复核诊断叠加图
├── level.json         ready/needs_fix 时的权威几何；needs_review 不发布
├── analysis.json      可玩性或复核原因
├── llm-audit.json     仅调用 LLM 时生成的旁路审计
└── result.json        原子发布的终态信封
```

关卡契约版本为 `schemaVersion=1.0`，当前算法版本为 `level-parser-1.0.0`。

---

## 6. API 契约速查

### 6.1 接口

```http
POST /v1/characters            multipart 字段名 "file"，仅 PNG/JPEG，≤10MB
  202 {"jobId": "char_<sha256(content) 前 12 位>", "statusUrl": "http://…", "viewUrl": "http://…"}
  400 {"code": "FILE_TOO_LARGE" | "NOT_AN_IMAGE" | "UNSUPPORTED_FORMAT", "message": "中文原因"}
  503 {"code": "QUEUE_UNAVAILABLE", "message": "中文原因"}

GET /v1/characters/{jobId}
  200 {"status": "...", "message": "中文状态/原因", ...终态附带字段, "statusUrl", "viewUrl", "updatedAt": "..."}
  404 {"code": "JOB_NOT_FOUND", "message": "中文原因"}

GET /v1/characters/{jobId}/detail   审查用：原图/GIF/标注/精灵表的全部 URL 与元数据（非契约，字段可变）
GET /v1/characters/{jobId}/view     同一份信息的单页 HTML，浏览器直开即可目视验收

POST /v1/levels                multipart：file + schemaVersion + 可选 playabilityProfile，?force=true 强制重跑
GET /v1/levels/{jobId}         queued/processing 带中文阶段名，终态返回契约信封
GET /v1/levels/{jobId}/detail  审查用：关卡图/识别几何/可玩性分析/全部产物地址（非契约）
GET /v1/levels/{jobId}/view    单页 HTML 预览：拉正图上叠画平台/出生点/终点/告警

GET /artifacts/{jobId}/{path}   StaticFiles 挂载 ./out/jobs
GET /healthz                    {"status": "ok"}
```

三条横向约定（详见 [`API.md`](API.md) 第 9 节）：

- **跨域**：已挂 `CORSMiddleware`，默认允许所有来源；`CORS_ALLOW_ORIGINS` / `CORS_ALLOW_METHODS` / `CORS_ALLOW_HEADERS` / `CORS_ALLOW_CREDENTIALS` / `CORS_MAX_AGE` 可调。来源为 `*` 时凭证自动关闭（浏览器规范所限）。
- **完整地址**：所有 `*Url` 都是含 scheme+host 的完整地址，直接请求即可；基址取 `PUBLIC_BASE_URL`，未配置时按请求 Host 推导。磁盘产物内部仍存相对路径，只在响应出口补全。
- **中文日志**：全链路中文，同时写控制台与 `out/logs/<组件>-<日期>.log`（按天拆分，跳天自动切新文件）；`LOG_LEVEL` 调级别，`LOG_KEEP_DAYS>0` 才清理旧日志。排查单个任务：`grep <jobId> out/logs/*.log`。

关卡上传与轮询示例（`playabilityProfile` 省略时使用服务端默认值）：

```bash
curl -sS -X POST http://localhost:8000/v1/levels \
  -F schemaVersion=1.0 \
  -F 'playabilityProfile={"profileVersion":"unity-c1-test-1","maxJumpRisePixels":150,"maxJumpDistancePixels":230,"characterWidthPixels":32,"characterHeightPixels":58,"landingTolerancePixels":6}' \
  -F file=@../testdata/levels/synthetic/front.png
# 202 {"jobId":"level_...","status":"queued","message":"排队中…","statusUrl":"http://localhost:8000/v1/levels/level_...","viewUrl":"http://…/view","createdAt":"..."}

curl -sS http://localhost:8000/v1/levels/level_...
# queued/processing（带中文阶段名），或 ready/needs_fix/needs_review/failed 终态信封
open http://localhost:8000/v1/levels/level_.../view      # 浏览器预览识别结果
```

关卡任务由独立 `level-worker` 消费 `pq:levels`。全栈现为 5 容器：`torchserve`、`redis`、`api`、`worker`、`level-worker`；关卡实机验收运行 `bash scripts/smoke/smoke_levels_e2e.sh`，成功标记为 `SMOKE_LEVELS_E2E_PASS`。可选 LLM 复核仅在同时配置 `LEVEL_LLM_BASE_URL`（必须 HTTPS）、`LEVEL_LLM_API_KEY`、`LEVEL_LLM_MODEL` 时启用；缺失、配置错误、请求失败或响应无效时无损降级为纯 OpenCV，LLM 和可玩性分析都不能创建、移动或延长平台坐标。

Level v1 已知边界：只支持单张横版纸、近水平直平台和单一红旗；输入限 JPEG/PNG、10 MiB、边长 800–12000、最多 4000 万像素；无鉴权/限流/对象存储；`force=true` 复用同一 jobId；真实拍摄和 Unity 任务 6–7 尚未验收。

给 Unity 的完整接口文档（字段类型、精灵表切帧、错误码、C# DTO、已知边界）见根目录 [`API.md`](API.md)。

**幂等**：`jobId` 由文件内容 sha256 派生 —— 同图必得同 ID。已存在且非终态时不重复入队；状态已过期且无快照时，同图重复提交按新任务重新入队（ID 不变）。

### 6.2 五态终态响应

下例取自真实冒烟结果（`jobId=char_9c3ff81ce4ea`，样本 `s01.png`）：

```jsonc
// 所有状态都额外带："message"（中文说明）、"statusUrl" / "viewUrl"（完整地址）
// ready：animations 必须恰好含 run 与 jump，两者帧尺寸相等
{"status":"ready","message":"渲染完成，可按 spriteSheetUrl 下载精灵表。","characterId":"char_9c3ff81ce4ea",
 "animations":{
   "run": {"spriteSheetUrl":"http://localhost:8000/artifacts/char_9c3ff81ce4ea/run.png","frameCount":10,"fps":15,
           "frameWidth":241,"frameHeight":275,"footAnchor":{"x":120,"y":275}},
   "jump":{"spriteSheetUrl":"http://localhost:8000/artifacts/char_9c3ff81ce4ea/jump.png","frameCount":7,"fps":15,
           "frameWidth":241,"frameHeight":275,"footAnchor":{"x":120,"y":275}}}}

// needs_correction：附 mask 与关节编辑数据，供 Unity「骨架点确认」页；message 可直接上屏
{"status":"needs_correction","reason":"NO_HUMANOID",
 "message":"画面里没有找到人形，请只画一个完整的火柴人后重传。",
 "maskUrl":"http://localhost:8000/artifacts/char_xxx/anno/mask.png",
 "joints":[{"name":"root","loc":[120,340],"parent":null}]}

// failed：稳定错误码 + 中文原因
{"status":"failed","code":"RENDER_TIMEOUT"|"RENDER_CRASHED"|"ASSET_MISSING"|"INTERNAL",
 "message":"单次渲染超过 120 秒，自动重试后仍失败，请重试或简化画面。"}
```

两个动作的 `frameWidth`/`frameHeight` **完全相等**（239×339）——这是 `render_character()` 取「所有动作全部帧内容包围盒的并集」作为统一帧尺寸的结果，Unity 因此能用同一套切帧参数播放 run 与 jump。反算可验证：`13 × 239 = 3107`、`12 × 239 = 2868`，与冒烟时下载到的精灵表实际像素宽逐字吻合。`footAnchor` 恒为 `(frameWidth // 2, frameHeight)`，即帧内水平居中、垂直贴底，供 Unity 落地对齐。

`joints` 正常路径固定 **16 项**；早期失败（`NO_HUMANOID`/`NO_CONTOUR` 等，此时 `char_cfg.yaml` 尚未生成）**为空数组**，客户端以 `reason` 提示重拍。`contracts.py` 的校验器显式允许 `16 或 0` 两种长度。

### 6.3 枚举全集（`app/contracts.py` 为唯一真源）

- `JobState`：`queued` `processing` `needs_correction` `ready` `failed`
- `ErrorCode`：`FILE_TOO_LARGE` `NOT_AN_IMAGE` `UNSUPPORTED_FORMAT` `JOB_NOT_FOUND` `QUEUE_UNAVAILABLE` `RENDER_TIMEOUT` `RENDER_CRASHED` `ASSET_MISSING` `INTERNAL`
- `CorrectionReason`：`NO_HUMANOID` `NO_SKELETON` `MULTIPLE_SKELETONS` `NO_CONTOUR` `SKELETON_MISFIT` `ANALYZE_FAILED`
- 关卡任务态：`queued` `processing` `ready` `needs_fix` `needs_review` `failed`；可玩性结论为 `playable` `unreachable` `uncertain`，唯一真源是 `app/level_contracts.py`。
- 中文文案表（与上述枚举一一对应，新增码必须同步补文案，已有测试卡住）：`contracts.ERROR_MESSAGES` / `REASON_MESSAGES` / `STATUS_MESSAGES`，`level_contracts.LEVEL_ERROR_MESSAGES` / `LEVEL_STATUS_MESSAGES` / `LEVEL_STAGE_MESSAGES`。

Unity 侧 `GameContracts.cs` 将来照 `contracts.py` 逐字镜像，客户端禁止使用匿名 JSON。

---

## 7. 动作资产（run / jump）

来源与调参结论完整记录在 [`app/assets/motions/README.md`](app/assets/motions/README.md)，要点：

| 资产 | CMU 源文件 | 抽稀参数 | 帧数 | 说明 |
|---|---|---|---|---|
| `run.bvh` | `009/09_01.bvh`（Subject 9 跑动） | `--every 7 --start 0 --end 86 --in-place` | 149 → **13** | 取 1 个完整跑动循环；`--in-place` 冻结根节点 X/Z 位移（跑步机式），否则角色会跑出画面 |
| `jump.bvh` | `016/16_01.bvh`（Subject 16 跳段） | `--every 5 --start 128 --end 183 --in-place` | 323 → **12** | 取起跳到落地的完整单抛物线 |

CMU BVH 的关节名需改名以适配 `fair1_ppf.yaml` retarget 配置（`LowerBack→Spine` 等逐个顺移），仅改 HIERARCHY 名字，不动通道与数据。

**三个必须记住的调参教训**：

1. `up: +y` —— CMU BVH 是 Y-up。误用 `+z` 会让高度变化被旋到水平轴，表现为「跳跃无腾空、跑动侧漂」。
2. `forward_perp_joint_vectors` 关节对必须是 **Right→Left** 顺序。反序会算出与 +X 反平行的 forward，触发 `rotate_between_vectors` 零四元数奇点，**全部关节位置变 NaN**。
3. motion YAML 里 `filepath` 写相对路径会在渲染时失效（`render_animation` 会 `chdir` 到 vendor），`render_scene._resolve_motion_cfg()` 会自愈重写为绝对路径并落 `*.resolved.yaml`（已 gitignore）。

FPS 固定 12。帧尺寸取该角色**所有动作**帧内容包围盒的并集（由 `render_character()` 保证），因此 run/jump 帧尺寸一致，Unity 可用同一套切帧参数。

---

## 8. 快速开始

### 8.1 前置条件

- Docker Desktop 分配 **≥16GB 内存**（TorchServe 模型加载需要）。
- **网络代理（本机实测必需）**：本机无法直连 docker.io，须在 Docker Desktop → Settings → Resources → Proxies 配置手动代理 `http://host.docker.internal:7890`（**不能用 `127.0.0.1`**，构建在 VM 内执行）；shell 里 `export all_proxy` 对守护进程无效。
- 首次构建镜像约 5-7 分钟。

### 8.2 起全栈（5 容器）

```bash
bash scripts/setup/setup-vendor.sh          # 幂等克隆 AnimatedDrawings 到 vendor/
docker compose up -d --build
curl http://localhost:8080/ping       # {"status": "Healthy"}
curl http://localhost:8000/healthz    # {"status":"ok"}
```

### 8.3 端到端冒烟

```bash
bash scripts/smoke/smoke_e2e.sh                       # 默认用 testdata/characters/s01.png
API_BASE=http://localhost:8000 bash scripts/smoke/smoke_e2e.sh /path/to/doodle.png
```

脚本会走完：健康检查 → 上传 → 每 5s 轮询（上限 180s）→ `ready` → 下载 run/jump 精灵表并用 PIL 断言 RGBA → 同图重复提交验幂等，最终输出 `SMOKE_E2E_PASS jobId=... elapsed=..s`。实测 20s 完成。

### 8.4 手动调一次

```bash
curl -F "file=@testdata/characters/s01.png" http://localhost:8000/v1/characters
curl http://localhost:8000/v1/characters/char_9c3ff81ce4ea
open http://localhost:8000/artifacts/char_9c3ff81ce4ea/run.png
```

### 8.5 宿主侧开发环境（不走容器，直调管线）

```bash
bash scripts/setup/setup-vendor.sh
bash scripts/setup/setup_env.sh                       # 建 .venv + pip install -e vendor + dev 依赖
source .venv/bin/activate
docker compose up -d torchserve                 # ① 分析仍依赖 TorchServe
pytest tests/ -v                                # pyproject.toml 已配置 pythonpath，可直接用 pytest
```

仓库已配置 `pyproject.toml` 与根级 `conftest.py`，`pytest` 或 `python -m pytest` 均可。

---

## 9. 测试地图

`tests/` 共 **63 个用例**：62 个服务层回归 + 1 个尖刺验收。

| 文件 | 用例数 | 覆盖内容 | 外部依赖 |
|---|---|---|---|
| `test_annotation_repair.py` | 16 | 断开部件（头环）被补回且像素/网格双连通、干净 mask 不被膨胀、非连续内存不崩、关节吸附到中轴而非边缘、越界关节不崩且计入偏移、**门禁时序（吸附前度量 + 拒绝时仍写回吸附结果）**、texture 必为 RGBA、vendor resize 复现、扩框几何与护栏、**细颈 mask 被识别为网格断开**、桥宽≥网格间距 2×、**断开时降级不硬写** | 无（合成图） |
| `test_contracts.py` | 7 | jobId 派生确定性、六键契约、`ready` 必含 run+jump、joints 16/0 双路径 | 无 |
| `test_job_store.py` | 7 | 队列进出、TTL、result 落盘、快照兜底、损坏快照返回 None、终态不可变 | fakeredis |
| `test_characters_api.py` | 12 | healthz、202+入队、幂等不重复入队、超大/非图/GIF 拒绝、404、ready 与 needs_correction 响应体、静态伺服、过期后重新入队、Redis 挂 → 503 | fakeredis + TestClient |
| `test_character_worker.py` | 6 | done→ready、崩溃重试后 `RENDER_CRASHED`、超时后重试成功不留 failed 中间态、业务终态不重试、result.json 损坏按基础设施故障重试、产物搬运 | 无（runner 注入） |
| `test_render_runner.py` | 2 | 早期失败的 result.json 形状、maskUrl 含 jobId | 无 |
| `test_render_scene_mesa_env.py` | 4 | `RENDER_USE_MESA` 各种取值、显式参数优先于环境变量 | 无 |
| `test_sprite_sheet.py` | 3 | 透明 PNG 与元数据、footAnchor 在内容底部、单帧 GIF | 无（合成 GIF） |
| `test_annotations.py` | 2 | 缺图 → NeedsCorrection、有效涂鸦产出 16 关节 | 后者需 TorchServe，否则 **skip** |
| `test_character_pipeline_contract.py` | 3 | 非法输入 → needs_correction、ready 含契约字段、跨动作帧尺寸一致 | **需 TorchServe 在线** |
| `test_spike_batch.py` | 1 | 20 样本批量验收裁决（宿主直调管线，非服务层回归，常规回归排除） | 需 TorchServe，耗时长 |

最近一次实测（宿主 macOS，TorchServe 在线）：

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/test_spike_batch.py
# 62 passed, 15 warnings in 43.52s
```

15 个警告均来自第三方（vendor `np.bool8` 弃用、urllib3/LibreSSL），非功能性。

---

## 10. 硬约束与已踩过的坑

改代码前请先看这一节，多数是付出过代价的：

| # | 约束/坑 | 后果与对策 |
|---|---|---|
| 1 | **`numpy` 必须钉 `1.24.4`** | vendor `setup.py` 钉死，连同 `Pillow==10.1.0`、`opencv-python==4.6.0.66`。升级会破坏 ARAP 形变与渲染 |
| 2 | **`render_animation` 会 `os.chdir(VENDOR)`** | 进程级状态。所有跨进程/跨容器路径**一律绝对化**；服务化后靠子进程隔离，不要在 api 进程内直接调管线 |
| 3 | **motion YAML 里的相对路径在 chdir 后解析失败** | `_resolve_motion_cfg()` 自愈重写为绝对路径，产物 `*.resolved.yaml` 已 gitignore |
| 4 | **GIF 拆帧必须逐帧 `convert('RGBA')`** | 对整个 Image 一次性 `convert` 会丢失动画帧，只剩第一帧 |
| 5 | **vendor 零改动** | `image_to_annotations` 硬编码 `localhost:8080`，用 entrypoint 里的 socat 转发解决，而不是改源码 |
| 6 | **脚本区 / 服务区硬分离** | `scripts/` 可以 import `app`；`app/` **禁止** import `scripts` |
| 7 | **OpenGL 上下文偶发初始化失败** | 尖刺首跑曾 20/20 全挂（伴随 `GLFWError: NSGL: Failed to find a suitable pixel format`），重跑恢复。已由子进程隔离 + 重试 1 次覆盖 |
| 8 | **Python 3.9 不支持 `str \| None` 运行时注解** | 统一用 `Optional[str]`（容器基础镜像 `python:3.9-slim`） |
| 9 | **`docker compose stop` 默认 10s SIGKILL** | 会打断渲染。worker 已设 `stop_grace_period: 300s`，且捕获 SIGTERM 走优雅退出 |
| 10 | **容器内无 GPU** | 走 Mesa/OSMesa 软渲染（`RENDER_USE_MESA=true`）。Mesa 尖刺已实证 GO，无需回退到「worker 宿主进程 + GLFW」方案。未来 Linux+NVIDIA 用 `docker-compose.override.yml` 加 GPU 声明 |
| 11 | **mask 必须单连通** | vendor `_generate_mesh()` 将 `find_contours` 按长度降序后**只用 `contours[0]`**。任何 mask 修补若留下多个连通域，补回的部分依旧会被丢弃 |
| 12 | **宿主 GLFW 与容器 Mesa 输出分辨率不一致** | `WINDOW_DIMENSIONS` 默认 [500,500]，但 `window_view` 取 `get_framebuffer_size()`，macOS Retina 下产出 **1000×1000**；Mesa 路径产出 500×500。对比产物时必须用**分辨率无关指标**（内容包围盒归一、占比），否则会得出错误结论 |
| 13 | **验收线曾只测「能否产出」不测「画得对不对」** | 20/20 “成功”里至少 5 份（s08/s09/s15/s17/s20）有严重视觉缺陷却均被判 `ready`。已补上质量门禁；验收时应同时看丢失率、关节偏移与丢 pin 数 |
| 14 | **vendor 的 `image.png` 存于 resize 之前，`bounding_box.yaml` 却属于 resize 之后的坐标系** | vendor 先 `imwrite(image.png)` 再 `if max(shape) > 1000: cv2.resize(...)`。用 bbox 去切 `image.png` 必须先复现这个缩放，否则**全线错位**（garlic 3024×4032 只能复现 58% 的 mask）。**手机拍摄必然 >1000px，这是真实输入路径上的必要步骤** |
| 15 | **mask 像素单连通 ≠ 三角网格连通** | 网格断开时 ARAP 刚度矩阵奇异，vendor `while np.linalg.det(...) == 0.0: += 1e-8*I` 会**死循环**（float32 下永不收敛，实测卡 >300s、983% CPU）。已用「不得差于 vendor 原始标注」的相对判据做降级阶梯 |
| 16 | **桥宽必须 ≥ 2× 网格间距**（`img_dim/39`） | 颈宽 3~8px 时桥内落不到三角面 → 网格断开。实测 300px 图上 12px（1.5×）仍断、16px（2×）才通；固定 4px 正落在死区中央 |
| 17 | **门禁必须在关节吸附之前度量** | 吸附后关节按定义落在 mask 内，偏移恒为 0——先吸附再度量会让门禁**永久失效**，坏样本静默放行为 `ready` |
| 18 | **跨不同大小的 crop 比较丢失率，必须映射回原图坐标系** | 用「各自 crop 内」的口径时，不扩框的 crop 里被裁掉的部件根本不存在，丢失率反而接近 0，**会得出完全相反的结论** |
| 19 | **纯二维骨架必须配 `flat2d.yaml`（三组投影面全 frontal）** | `motion_2d` 生成的骨架整体落在 ZY 平面（x 恒为 0），frontal 取 `(-z, y)` 才是 1:1 还原。换回 vendor 的 `fair1_ppf`（用 pca）会给下肢选中 sagittal（取 x），把只存在于 ZY 平面的动作**整体压掉**（实测大腿摆幅 76°→19°） |
| 20 | **换检测框后必须同步平移关节坐标** | `char_cfg` 的坐标相对 vendor 那个检测框；错位量＝两框左上角之差。15% 扩框只偏框宽的 15%（勉强目视可过，所以长期没暴露），按墨迹重裁能偏几百像素——实测 s07 偏 239px，ARAP 拿错位的 pin 把水平的猪拧歪压扁 31% |
| 21 | **char 侧骨骼的父子关系与 retarget 里 BVH 那侧的关节对不是一回事** | 唯一不一致的是 `neck`：BVH 侧是 `Hips→Neck`（跨多级），char 侧 `neck` 的父是 `torso`，vendor 会把那个方向施加到 `torso→neck` 上。照抄 BVH 关节对会让头被硬转两者夹角（s07 25°、s09 23°） |
| 22 | **NaN 会静默绕过断言** | NaN 参与 `max` 与比较时恒为 False。零长骨（s14 躯干仅占身高 3%）会让 vendor 归一化除零出 NaN，若测试只断言「最大偏差 < 阈值」就会通过。断言里必须显式查 `isfinite` |

---

## 11. 验收结论与残留项

### 已完成的两轮裁决

**① 动画管线尖刺（任务 2）** —— 详见 [`docs/animation-spike-results.md`](docs/animation-spike-results.md)

- 20 样本 **20 成功 / 0 失败 / 0 需修正**，单张最大 18.1s（门槛：≥16 成功、≤60s）→ 机械可产出性验收线通过，据此进入任务 3。
- 覆盖两种画风：官方彩色涂鸦（7 张）与简笔线稿（13 张）。
- 补充信号：样本筹备阶段 75 张 Quick, Draw! 候选中 6 张 `NO_HUMANOID`，检测器对简笔线稿识别率约 92%，说明 `needs_correction` 分类机制运转正常。
- **⚠️ 但该裁决不包含画面质量维度。** 2026-09-02 复查发现其中至少 **5 份**（s08 头环丢失、s09 mask 失真、s15/s17/s20 关节错位导致严重变形）不可用于演示，却均被判为 `ready`。根因（vendor 侧 D0-D4 五道叠加缺陷）、修复与重新裁决见该文档**第 ⑥ 节**。修复后：**20/20 可用**（全量实渲 40/40 成功、零空帧、无死锁；丢 pin 从 44 降到 16）。
- 已知观感局限：2D 正面纹理下 run 的双腿会部分重叠；jump 四肢幅度小，主要靠整体腾空表现跳跃。

**② 异步服务化冒烟（任务 3）** —— 详见 [`docs/async-service-smoke-results.md`](docs/async-service-smoke-results.md)

- `SMOKE_E2E_PASS`，jobId `char_9c3ff81ce4ea`，总耗时 20s；精灵表 run 3107×339 / jump 2868×339，均 RGBA。
- 全量回归 41 用例全绿（当时数字；后续审查修复项补充至 46，再加标注修复的 16 例后为 **62**，本文档已实测复核）。
- Mesa 尖刺结论 **GO**，部署形态维持全容器化 4 容器，回退分支未启用。

### 仍待处理的残留项

- **真实儿童画复测**：当前用简笔画代理样本裁决，产品验收前建议以真实儿童涂鸦（家长授权）复测，并用 `scripts/diag/diagnose_annotations.py --render` 重标定 `MAX_JOINT_OFFSET` 与 `PAD_RATIO`。
- **s08/s10/s11/s15 仍各有 2 个 pin 被丢**（s08 原为 22），未归零；归零需改 vendor 的网格密度（已评估为低优先级）。
- **s09 双腿交叉一团无法修**（双膝间距 10px、左右腿长 18.7% vs 8.6%），经人工确认可接受，故**未加退化门禁**（加了会误杀）。
- **无头/CI 渲染**：`use_mesa=True` 路线已在容器内验证；纯 CI 环境未验证。
- **偶发 GLFW 故障的长期观察**：已加进程隔离与重试，需在更大样本量下确认不再复现。
- 管线层的次要隐患（`_resolve_motion_cfg` 同名文件边缘情况、`frame_size` 小于内容无防御、门面入参未 `resolve()`）记录在尖刺结果文档第 ⑤ 节，可延后。

---

## 12. 文档索引

| 文件 | 读它的时机 |
|---|---|
| [`API.md`](API.md) | **要给 Unity 接客户端**：全部端点、字段类型、精灵表切帧与脚底锚点、错误码、C# DTO、已知边界（CORS / 无鉴权）|
| [`docs/plans/2026-09-01-paper-game-p0.md`](docs/plans/2026-09-01-paper-game-p0.md) | 想知道整体目标、8 个任务、10 日排期、前后端职责边界、三段 MVP 验收链路 |
| [`docs/superpowers/specs/2026-09-01-doodle-animation-pipeline-design.md`](docs/superpowers/specs/2026-09-01-doodle-animation-pipeline-design.md) | 想知道动画管线为什么选 AnimatedDrawings 自部署、输出契约、失败处理 |
| [`docs/superpowers/plans/2026-09-01-doodle-animation-pipeline.md`](docs/superpowers/plans/2026-09-01-doodle-animation-pipeline.md) | 想按 TDD 步骤重走一遍管线实现；含**已对 vendor 源码核实的接口事实**（勿再猜测） |
| [`docs/superpowers/specs/2026-09-02-character-async-service-design.md`](docs/superpowers/specs/2026-09-02-character-async-service-design.md) | 想知道服务化的 compose 拓扑、队列/状态存储、worker 韧性、测试策略、裁定理由 |
| [`docs/superpowers/plans/2026-09-02-character-async-service.md`](docs/superpowers/plans/2026-09-02-character-async-service.md) | 想看服务化 8 个任务的逐步实现与全局约束清单 |
| [`docs/animation-spike-results.md`](docs/animation-spike-results.md) | 想知道尖刺验收数据与残留项清单 |
| [`docs/async-service-smoke-results.md`](docs/async-service-smoke-results.md) | 想知道冒烟与回归结论、下一步 |
| [`app/assets/motions/README.md`](app/assets/motions/README.md) | 想换动作资产、调 motion YAML、或搞清 BVH 来源与抽稀参数 |

---

## 13. 下一步

### 已立项待办（2026-09-04 评估后确定）

| 编号 | 待办 | 触发条件 | 为什么值得做 |
|---|---|---|---|
| **F6** | **真实拍摄照片的分割鲁棒性**：把 vendor `segment()` 的 `adaptiveThreshold` 换成对光照/纸纹鲁棒的方案（候选 `rembg`/U²-Net 或 SAM） | 拿到真实手机拍摄样本后 | **优先级最高**。当前 20 个验收样本全是干净白底 PNG，而产品真实输入是手机拍的纸张（纸纹/阴影/光照不均）——**这条路径至今一次未测**。已观察到前微：garlic（彩色非白底）上 `_ink` 大量误判背景，扩框被迫回退。换掉它同时解决 D1（不再只留最大块） |
| **F4** | **把形变从服务端搬到 Unity 2D Animation**：服务端只出「切好的部件图 + 骨架 JSON」，蒙皮交给 Unity Skinning Editor | 精灵表契约可重议时 | 效果上限最高。ARAP 是 2021 年的研究代码，Unity 2D Animation 是产品级工具链；能彻底摆脱「正面画 + 挤压」这一整类问题，资产体积从 13+12 帧精灵表变成一张图 + 骨架，且美术能手工修权重。代价：会动 P0 已锁定的输出契约，Unity 侧工作量前移 |

同期评估过但**已定不做**的：改 vendor 源码（补丁/Fork/猴补丁）——原本最大的卖点是修 ARAP 丢 pin，已被零改动的「关节中轴吸附」替代；换投影面（pca/frontal/sagittal 按部位组合）——人工目视逐个比对后确认现状 `fair1_ppf.yaml` 最优。

### 紧接的两件事（来自冒烟结论）

1. **对接 P0 任务 6 Unity 客户端联调**：把 `POST /v1/characters` → 轮询 `GET /v1/characters/{jobId}` → 下载 `/artifacts/{jobId}/run.png|jump.png` 接入 Unity，验证五态契约与精灵表元数据在客户端的消费（切帧、跑跳状态机、脚底锚点对齐）。
2. **演示阶段对象存储升级**：把产物从 api 容器本地静态目录迁到 OSS/S3，`spriteSheetUrl` 指向 CDN，支撑多实例与持久化。

### 尚未动工的 P0 任务

- 任务 4-5：服务端关卡视觉解析与可玩性校验代码已落地；仍需补真实 Docker 全栈冒烟、真实拍摄集标定，以及 C1Levels 黄金端点精度收敛。
- 任务 6-7：Unity 侧全部（`client-unity/` 目录尚不存在）。
- 任务 8：全链路集成验收（15 张真实关卡纸 ≥13 张正确识别；3 名儿童完整流程；全程 ≤3 分钟）。

### 明确不在 P0 范围

迷宫、怪物、金币、自由物体语义识别、关卡编辑器、账号体系、作品分享、多人玩法；非人形角色的自动骨架推断（只可走人工标注路线）。
