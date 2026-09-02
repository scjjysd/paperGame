# 纸上游戏机（Paper Game Console）

> 孩子在纸上画一个火柴人和一张带横线、红旗的关卡，拍照上传；服务端把涂鸦变成会跑会跳的角色动画，Unity 里让这个角色跑到红旗终点。

本文件是仓库的**全局导览**：读完可以知道项目在做什么、代码在哪、怎么跑起来、当前进度到哪、下一步做什么。细节规格与验收记录见文末[文档索引](#文档索引)。

---

## 1. 项目背景与定位

| 项 | 内容 |
|---|---|
| 参赛背景 | 叫叫 1024 程序员节 · JiaoJiao Hackathon（AI Startup Round）主投场 |
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

截至 `main` 分支最新提交（`21d284a`）：

| P0 任务 | 内容 | 状态 |
|---|---|---|
| 任务 1 | 契约、样本、验收基线 | ✅ 服务端侧完成（`app/contracts.py` + `testdata/characters/s01-s20.png`）；Unity 侧 `GameContracts.cs` 未建 |
| 任务 2 | AnimatedDrawings 可行性尖刺 | ✅ **验收通过**：20/20 成功，单张最大 18.1s（门槛 ≥16 成功、≤60s） |
| 任务 3 | 服务端角色异步接口 | ✅ **完成并冒烟通过**：4 容器全栈、46 用例全绿、端到端 `SMOKE_E2E_PASS` |
| 任务 4 | 服务端关卡视觉解析 | ⬜ 未开始（无 `level_parser.py` / `api/levels.py`） |
| 任务 5 | 服务端可玩性校验 | ⬜ 未开始（无 `playability.py`） |
| 任务 6 | Unity 主角生成与缓存 | ⬜ 未开始（**仓库尚无 `client-unity/` 目录**） |
| 任务 7 | Unity 关卡拍摄与游玩闭环 | ⬜ 未开始 |
| 任务 8 | 集成验收与兜底 | 🟡 部分：角色链路端到端冒烟已做；全链路（含关卡）未做 |

一句话概括：**服务端角色动画这一条链路已经打通并可部署，关卡解析与 Unity 客户端尚未动工。**

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
   ├─② 渲染   render_scene.render_animation()
   │          → 生成 scene YAML（character_cfg + motion_cfg + retarget fair1_ppf）
   │          → os.chdir(vendor) → animated_drawings.render.start()
   │          → run.gif（13 帧）/ jump.gif（12 帧），RGBA 透明
   │
   └─③ 拼表   sprite_sheet.build_sprite_sheet()
              → 逐帧 convert('RGBA') → 所有帧内容包围盒并集 → 横向拼接
              → run.png / jump.png + 元数据（frameCount/fps/frameWidth/frameHeight/footAnchor）
```

①② 是 AnimatedDrawings 现成能力（**只配置、不改 vendor 源码**），③ 是本项目唯一新写的图像后处理代码。三段以文件交接，任一步可独立重跑。

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
├── 2026-09-01-paper-game-p0.md      ★ P0 总计划：8 个任务 + 10 日排期 + 三段 MVP 验收链路
├── docs/
│   ├── animation-spike-results.md   ★ 任务 2 验收记录：20 样本逐张耗时、残留项清单
│   ├── async-service-smoke-results.md ★ 任务 3 冒烟与回归结论
│   └── superpowers/
│       ├── specs/                   两份设计规格（管线 / 异步服务化）
│       └── plans/                   两份实现计划（含 TDD 步骤与已核实的接口事实）
├── testdata/characters/             ★ s01-s20.png 验收样本（已入库，见下方授权说明）
└── server/                          ★ 全部服务端代码
    ├── app/
    │   ├── main.py                  FastAPI 应用工厂：路由 + /artifacts 静态挂载 + /healthz
    │   ├── contracts.py             ★ Pydantic 契约：五态、错误码、六键动画元数据、derive_job_id
    │   ├── api/characters.py        POST 上传入队（幂等）/ GET 轮询
    │   ├── services/
    │   │   ├── annotations.py       ① vendor 包装层：异常 → NeedsCorrection
    │   │   ├── render_scene.py      ② 场景 YAML 生成 + 渲染；Mesa 开关；motion 路径自愈
    │   │   ├── sprite_sheet.py      ③ GIF → 透明 PNG 精灵表 + footAnchor
    │   │   ├── character_pipeline.py ★ 管线门面 render() / render_character()
    │   │   └── job_store.py         Redis Hash(TTL 24h) + List 队列 + result.json 兜底
    │   ├── workers/
    │   │   ├── character_worker.py  BRPOP 主循环：超时 120s、重试 1 次、SIGTERM 优雅退出
    │   │   └── render_runner.py     子进程入口：渲染 → 搬运产物 → 写 result.json（退出码协议）
    │   └── assets/motions/          ★ run.bvh/run.yaml、jump.bvh/jump.yaml + README（来源与调参结论）
    ├── scripts/                     验证脚本区（可 import app；反向禁止）
    │   ├── setup-vendor.sh          幂等克隆 AnimatedDrawings 到 vendor/
    │   ├── setup_env.sh             建宿主 .venv + pip install -e vendor
    │   ├── render_smoke.py          容器内 Mesa 渲染 go/no-go 尖刺
    │   ├── smoke_e2e.sh           ★ 全栈端到端冒烟（上传→轮询→下载验 PNG→幂等复验）
    │   ├── decimate_bvh.py          BVH 抽稀（可复用）
    │   └── download_samples.py      官方示例涂鸦下载（正式样本已入库，仅留档）
    ├── tests/                       47 个用例（46 回归 + 1 尖刺验收）
    ├── docker-compose.yml         ★ 4 容器编排：torchserve / redis / api / worker
    ├── Dockerfile                   api 与 worker 共享镜像（python:3.9-slim + OSMesa）
    ├── docker/entrypoint.sh         socat 把容器内 localhost:8080 转发到 torchserve:8080
    ├── requirements-service.txt     镜像内运行时依赖（vendor 子集，不含 torch，省 ~800MB）
    ├── requirements-dev.txt         宿主开发/测试依赖
    ├── vendor/                      AnimatedDrawings 源码（.gitignore，由 setup-vendor.sh 拉取）
    └── out/                         产物与中间文件（.gitignore）：jobs/ spike/ mesa-spike/ ...
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
| `pg-api` | `paper-game/server:local` | 8000 | 上传、轮询、静态伺服产物 | `uvicorn app.main:app`；卷 `./out → /data/out` |
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
server/out/jobs/{jobId}/
├── input.png        上传原图
├── anno/            mask.png / texture.png / char_cfg.yaml（needs_correction 时供骨架确认页用）
├── run.png          run 精灵表（3107×339 RGBA，13 帧）
├── jump.png         jump 精灵表（2868×339 RGBA，12 帧）
└── result.json      终态快照
```

当前由 api 容器本地静态目录 `/artifacts` 伺服；**演示阶段计划升级为对象存储 + 签名 URL**（WebGL CORS 兼容、多实例），接口层已用 URL 抽象隔离，届时不改 API 形状。

---

## 6. API 契约速查

### 6.1 接口

```http
POST /v1/characters            multipart 字段名 "file"，仅 PNG/JPEG，≤10MB
  202 {"jobId": "char_<sha256(content) 前 12 位>"}
  400 {"code": "FILE_TOO_LARGE" | "NOT_AN_IMAGE" | "UNSUPPORTED_FORMAT"}
  503 {"code": "QUEUE_UNAVAILABLE"}

GET /v1/characters/{jobId}
  200 {"status": "...", ...终态附带字段, "updatedAt": "..."}
  404 {"code": "JOB_NOT_FOUND"}

GET /artifacts/{jobId}/{path}   StaticFiles 挂载 ./out/jobs
GET /healthz                    {"status": "ok"}
```

**幂等**：`jobId` 由文件内容 sha256 派生 —— 同图必得同 ID。已存在且非终态时不重复入队；状态已过期且无快照时，同图重复提交按新任务重新入队（ID 不变）。

### 6.2 五态终态响应

下例取自真实冒烟结果（`jobId=char_9c3ff81ce4ea`，样本 `s01.png`）：

```jsonc
// ready：animations 必须恰好含 run 与 jump，两者帧尺寸相等
{"status":"ready","characterId":"char_9c3ff81ce4ea",
 "animations":{
   "run": {"spriteSheetUrl":"/artifacts/char_9c3ff81ce4ea/run.png","frameCount":13,"fps":12,
           "frameWidth":239,"frameHeight":339,"footAnchor":{"x":119,"y":339}},
   "jump":{"spriteSheetUrl":"/artifacts/char_9c3ff81ce4ea/jump.png","frameCount":12,"fps":12,
           "frameWidth":239,"frameHeight":339,"footAnchor":{"x":119,"y":339}}}}

// needs_correction：附 mask 与关节编辑数据，供 Unity「骨架点确认」页
{"status":"needs_correction","reason":"NO_HUMANOID",
 "maskUrl":"/artifacts/char_xxx/anno/mask.png",
 "joints":[{"name":"root","loc":[120,340],"parent":null}]}

// failed：稳定错误码
{"status":"failed","code":"RENDER_TIMEOUT"|"RENDER_CRASHED"|"ASSET_MISSING"|"INTERNAL"}
```

两个动作的 `frameWidth`/`frameHeight` **完全相等**（239×339）——这是 `render_character()` 取「所有动作全部帧内容包围盒的并集」作为统一帧尺寸的结果，Unity 因此能用同一套切帧参数播放 run 与 jump。反算可验证：`13 × 239 = 3107`、`12 × 239 = 2868`，与冒烟时下载到的精灵表实际像素宽逐字吻合。`footAnchor` 恒为 `(frameWidth // 2, frameHeight)`，即帧内水平居中、垂直贴底，供 Unity 落地对齐。

`joints` 正常路径固定 **16 项**；早期失败（`NO_HUMANOID`/`NO_CONTOUR` 等，此时 `char_cfg.yaml` 尚未生成）**为空数组**，客户端以 `reason` 提示重拍。`contracts.py` 的校验器显式允许 `16 或 0` 两种长度。

### 6.3 枚举全集（`server/app/contracts.py` 为唯一真源）

- `JobState`：`queued` `processing` `needs_correction` `ready` `failed`
- `ErrorCode`：`FILE_TOO_LARGE` `NOT_AN_IMAGE` `UNSUPPORTED_FORMAT` `JOB_NOT_FOUND` `QUEUE_UNAVAILABLE` `RENDER_TIMEOUT` `RENDER_CRASHED` `ASSET_MISSING` `INTERNAL`
- `CorrectionReason`：`NO_HUMANOID` `NO_SKELETON` `MULTIPLE_SKELETONS` `NO_CONTOUR` `ANALYZE_FAILED`
- 关卡侧（**尚未实现**，契约已在 P0 计划中锁定）：`playable` / `needs_fix`，错误码 `NO_GROUND` `NO_FLAG` `MULTIPLE_FLAGS` `GOAL_UNREACHABLE` `PHOTO_INVALID`

Unity 侧 `GameContracts.cs` 将来照 `contracts.py` 逐字镜像，客户端禁止使用匿名 JSON。

---

## 7. 动作资产（run / jump）

来源与调参结论完整记录在 [`server/app/assets/motions/README.md`](server/app/assets/motions/README.md)，要点：

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

### 8.2 起全栈（4 容器）

```bash
cd server
bash scripts/setup-vendor.sh          # 幂等克隆 AnimatedDrawings 到 vendor/
docker compose up -d --build
curl http://localhost:8080/ping       # {"status": "Healthy"}
curl http://localhost:8000/healthz    # {"status":"ok"}
```

### 8.3 端到端冒烟

```bash
cd server
bash scripts/smoke_e2e.sh                       # 默认用 ../testdata/characters/s01.png
API_BASE=http://localhost:8000 bash scripts/smoke_e2e.sh /path/to/doodle.png
```

脚本会走完：健康检查 → 上传 → 每 5s 轮询（上限 180s）→ `ready` → 下载 run/jump 精灵表并用 PIL 断言 RGBA → 同图重复提交验幂等，最终输出 `SMOKE_E2E_PASS jobId=... elapsed=..s`。实测 20s 完成。

### 8.4 手动调一次

```bash
curl -F "file=@../testdata/characters/s01.png" http://localhost:8000/v1/characters
curl http://localhost:8000/v1/characters/char_9c3ff81ce4ea
open http://localhost:8000/artifacts/char_9c3ff81ce4ea/run.png
```

### 8.5 宿主侧开发环境（不走容器，直调管线）

```bash
cd server
bash scripts/setup-vendor.sh
bash scripts/setup_env.sh                       # 建 .venv + pip install -e vendor + dev 依赖
source .venv/bin/activate
docker compose up -d torchserve                 # ① 分析仍依赖 TorchServe
python -m pytest tests/ -v --ignore=tests/test_spike_batch.py
```

注意必须用 `python -m pytest`（把 cwd 加进 `sys.path`），仓库当前无 `conftest.py`。

---

## 9. 测试地图

`server/tests/` 共 **47 个用例**：46 个服务层回归 + 1 个尖刺验收。

| 文件 | 用例数 | 覆盖内容 | 外部依赖 |
|---|---|---|---|
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
# 46 passed, 15 warnings in 26.72s
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
| 6 | **脚本区 / 服务区硬分离** | `server/scripts/` 可以 import `app`；`server/app/` **禁止** import `scripts` |
| 7 | **OpenGL 上下文偶发初始化失败** | 尖刺首跑曾 20/20 全挂（伴随 `GLFWError: NSGL: Failed to find a suitable pixel format`），重跑恢复。已由子进程隔离 + 重试 1 次覆盖 |
| 8 | **Python 3.9 不支持 `str \| None` 运行时注解** | 统一用 `Optional[str]`（容器基础镜像 `python:3.9-slim`） |
| 9 | **`docker compose stop` 默认 10s SIGKILL** | 会打断渲染。worker 已设 `stop_grace_period: 300s`，且捕获 SIGTERM 走优雅退出 |
| 10 | **容器内无 GPU** | 走 Mesa/OSMesa 软渲染（`RENDER_USE_MESA=true`）。Mesa 尖刺已实证 GO，无需回退到「worker 宿主进程 + GLFW」方案。未来 Linux+NVIDIA 用 `docker-compose.override.yml` 加 GPU 声明 |

---

## 11. 验收结论与残留项

### 已完成的两轮裁决

**① 动画管线尖刺（任务 2）** —— 详见 [`docs/animation-spike-results.md`](docs/animation-spike-results.md)

- 20 样本 **20 成功 / 0 失败 / 0 需修正**，单张最大 18.1s（门槛：≥16 成功、≤60s）→ **验收线通过**，据此进入任务 3。
- 覆盖两种画风：官方彩色涂鸦（7 张）与简笔线稿（13 张）。
- 补充信号：样本筹备阶段 75 张 Quick, Draw! 候选中 6 张 `NO_HUMANOID`，检测器对简笔线稿识别率约 92%，说明 `needs_correction` 分类机制运转正常。
- 已知观感局限：2D 正面纹理下 run 的双腿会部分重叠；jump 四肢幅度小，主要靠整体腾空表现跳跃。

**② 异步服务化冒烟（任务 3）** —— 详见 [`docs/async-service-smoke-results.md`](docs/async-service-smoke-results.md)

- `SMOKE_E2E_PASS`，jobId `char_9c3ff81ce4ea`，总耗时 20s；精灵表 run 3107×339 / jump 2868×339，均 RGBA。
- 全量回归 41 用例全绿（当时数字；后续审查修复项补充至 **46**，本文档已实测复核）。
- Mesa 尖刺结论 **GO**，部署形态维持全容器化 4 容器，回退分支未启用。

### 仍待处理的残留项

- **真实儿童画复测**：当前用简笔画代理样本裁决，产品验收前建议以真实儿童涂鸦（家长授权）复测。
- **无头/CI 渲染**：`use_mesa=True` 路线已在容器内验证；纯 CI 环境未验证。
- **偶发 GLFW 故障的长期观察**：已加进程隔离与重试，需在更大样本量下确认不再复现。
- 管线层的次要隐患（`_resolve_motion_cfg` 同名文件边缘情况、`frame_size` 小于内容无防御、门面入参未 `resolve()`）记录在尖刺结果文档第 ⑤ 节，可延后。

---

## 12. 文档索引

| 文件 | 读它的时机 |
|---|---|
| [`2026-09-01-paper-game-p0.md`](2026-09-01-paper-game-p0.md) | 想知道整体目标、8 个任务、10 日排期、前后端职责边界、三段 MVP 验收链路 |
| [`docs/superpowers/specs/2026-09-01-doodle-animation-pipeline-design.md`](docs/superpowers/specs/2026-09-01-doodle-animation-pipeline-design.md) | 想知道动画管线为什么选 AnimatedDrawings 自部署、输出契约、失败处理 |
| [`docs/superpowers/plans/2026-09-01-doodle-animation-pipeline.md`](docs/superpowers/plans/2026-09-01-doodle-animation-pipeline.md) | 想按 TDD 步骤重走一遍管线实现；含**已对 vendor 源码核实的接口事实**（勿再猜测） |
| [`docs/superpowers/specs/2026-09-02-character-async-service-design.md`](docs/superpowers/specs/2026-09-02-character-async-service-design.md) | 想知道服务化的 compose 拓扑、队列/状态存储、worker 韧性、测试策略、裁定理由 |
| [`docs/superpowers/plans/2026-09-02-character-async-service.md`](docs/superpowers/plans/2026-09-02-character-async-service.md) | 想看服务化 8 个任务的逐步实现与全局约束清单 |
| [`docs/animation-spike-results.md`](docs/animation-spike-results.md) | 想知道尖刺验收数据与残留项清单 |
| [`docs/async-service-smoke-results.md`](docs/async-service-smoke-results.md) | 想知道冒烟与回归结论、下一步 |
| [`server/app/assets/motions/README.md`](server/app/assets/motions/README.md) | 想换动作资产、调 motion YAML、或搞清 BVH 来源与抽稀参数 |

---

## 13. 下一步

**紧接的两件事**（来自冒烟结论）：

1. **对接 P0 任务 6 Unity 客户端联调**：把 `POST /v1/characters` → 轮询 `GET /v1/characters/{jobId}` → 下载 `/artifacts/{jobId}/run.png|jump.png` 接入 Unity，验证五态契约与精灵表元数据在客户端的消费（切帧、跑跳状态机、脚底锚点对齐）。
2. **演示阶段对象存储升级**：把产物从 api 容器本地静态目录迁到 OSS/S3，`spriteSheetUrl` 指向 CDN，支撑多实例与持久化。

**尚未动工的 P0 任务**：

- 任务 4：关卡视觉解析（四角定位标记检测 → A4 透视校正 → 1920×1080 画布；HoughLinesP 检黑色近水平线合并为平台；HSV 红色阈值 + 三角形轮廓 + 竖杆空间关系识别旗帜）。
- 任务 5：可玩性校验（平台为节点、按 Unity 锁定的 `maxJumpX/maxJumpY` 建有向边，从起始平台 BFS 到旗帜所在平台）。
- 任务 6-7：Unity 侧全部（`client-unity/` 目录尚不存在）。
- 任务 8：全链路集成验收（15 张真实关卡纸 ≥13 张正确识别；3 名儿童完整流程；全程 ≤3 分钟）。

**明确不在 P0 范围**：迷宫、怪物、金币、自由物体语义识别、关卡编辑器、账号体系、作品分享、多人玩法；非人形角色的自动骨架推断（只可走人工标注路线）。
