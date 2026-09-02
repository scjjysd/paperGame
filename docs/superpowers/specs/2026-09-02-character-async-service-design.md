# 角色异步服务化（P0 任务 3）设计

> 对应 `2026-09-01-paper-game-p0.md` 任务 3 与服务端工作包 S2。
> 前置：动画管线尖刺已验收通过（20/20 成功，单张最大 18.1s，见 `docs/animation-spike-results.md`）。
> 范围：把单机函数调用的 `CharacterPipeline.render_character()` 服务化为异步 HTTP 接口；不含对象存储、不含 Unity 客户端、不含关卡接口（P0 任务 4-7）。

## 1. 结论

FastAPI + Redis 队列 + 独立 worker 容器，子进程执行渲染，全部组件集成 docker-compose 管理；验证脚本与 HTTP 服务代码硬分离。存储用本地卷 + FastAPI 静态伺服，接口层以 URL 抽象隔离，演示阶段升级对象存储时不改 API 形状。

## 2. compose 拓扑

```
server/docker-compose.yml（4 容器统一编排）
├── torchserve   既有容器不动（涂鸦分析，8080/8081，16GB 上限）
├── redis        redis:7-alpine（队列 + 任务状态）
├── api          paper-game/server:local，command=uvicorn，8000 端口
└── worker       同一镜像，command=python -m app.workers.character_worker
共享卷：./out → /data/out
```

- api 与 worker **分开部署**（已裁定）：故障域隔离、可独立 restart、可 `--scale worker=N` 伸缩；镜像经 compose 锚点共享，边际成本仅一份基础运行时内存。
- worker 镜像：`python:3.9-slim` + Mesa/OSMesa 软渲染库 + vendor 源码 + 管线依赖（numpy==1.24.4 钉版继承）。
- **GPU 扩展位**：约定 `docker-compose.override.yml` 承载未来 Linux+NVIDIA 的 `--gpus` 配置；Mac 开发阶段容器无 GPU，Mesa 软渲染（尖刺已证 CPU 全程可跑）。
- **Mesa 渲染尖刺前置**：实现计划第一个任务先以最小容器验证 `USE_MESA=True` 能渲染出 GIF；失败则 worker 回退宿主进程（compose 只管 api+redis），其余设计不变。

## 3. 脚本与 HTTP 服务分离（硬边界）

| 分区 | 路径 | 职责 | 依赖方向 |
|---|---|---|---|
| 快速验证脚本 | `server/scripts/`（smoke_e2e.sh 等） | 开发期验证、CLI 直调管线 | 脚本 → app 允许 |
| HTTP 服务 | `server/app/main.py`、`app/api/characters.py`、`app/workers/character_worker.py`、`app/contracts.py` | 真实对外服务 | app → scripts 禁止 |

## 4. 队列与状态存储

- 队列：Redis List `pq:characters`，`LPUSH` 入队 / `BRPOP` 消费（worker 并发 = 1）；不引入 Celery/RQ（YAGNI）。
- 状态：Redis Hash `job:{jobId}`（status、error、result、updatedAt），TTL 24h；`out/jobs/{jobId}/result.json` 为终态快照，Redis 过期后由 API 兜底重建。
- 状态机：`queued → processing → {ready | needs_correction | failed}`；终态不可变更。

## 5. API 契约

```
POST /v1/characters        multipart "file"，PNG/JPEG，≤10MB
  → 202 {"jobId": "char_<sha256前12位>"}
  → 400 {"code": "FILE_TOO_LARGE" | "NOT_AN_IMAGE" | "UNSUPPORTED_FORMAT"}
  → 503 {"code": "QUEUE_UNAVAILABLE"}（入队前 Redis 探测失败）
  幂等：jobId 由文件内容 sha256 派生；以 `job:{jobId}` 现存状态判重，非终态重复提交返回同一 jobId 不重复入队；状态已过期（TTL 到期且无 result.json）时同图重复提交按新任务重新入队

GET /v1/characters/{jobId}
  → 200 {"status": "...", ...终态附带字段, "updatedAt": "..."}
  → 404 {"code": "JOB_NOT_FOUND"}（含 TTL 过期且无 result.json）

GET /artifacts/{jobId}/{path}   StaticFiles 挂载 ./out/jobs，伺服产物
```

终态响应体：

```jsonc
// ready：六键契约与尖刺产物一致，帧尺寸跨动作相等
{"status":"ready","characterId":"char_abc123",
 "animations":{
   "run": {"spriteSheetUrl":"/artifacts/char_abc123/run.png","frameCount":13,"fps":12,"frameWidth":481,"frameHeight":655,"footAnchor":{"x":240,"y":655}},
   "jump":{"spriteSheetUrl":"/artifacts/char_abc123/jump.png","frameCount":12,"fps":12,"frameWidth":481,"frameHeight":655,"footAnchor":{"x":240,"y":655}}}}

// needs_correction：附 mask 与关节编辑数据（骨架确认页用；joints 正常路径固定 16 项，早期失败如 NO_HUMANOID/NO_CONTOUR 无可编辑标注时为空数组，客户端以 reason 提示重拍）
{"status":"needs_correction","reason":"NO_HUMANOID",
 "maskUrl":"/artifacts/char_abc123/anno/mask.png",
 "joints":[{"name":"root","loc":[120,340],"parent":null}, {"name":"hip","loc":[120,340],"parent":"root"}]}

// failed：稳定错误码
{"status":"failed","code":"RENDER_TIMEOUT"|"RENDER_CRASHED"|"ASSET_MISSING"|"INTERNAL"}
```

存储布局 `./out/jobs/{jobId}/`：`input.png`、`anno/`（mask/texture/char_cfg）、`run.png`、`jump.png`、`result.json`。

契约类型：`server/app/contracts.py` 以 Pydantic 定义全部请求/响应/错误码模型（P0 任务 1 共享契约文件；Unity 侧 `GameContracts.cs` 将来照此镜像）。

## 6. worker 韧性与错误处理

- **执行模型**：主循环 `BRPOP` → 置 `processing` → spawn 子进程执行 `render_character()` → 按退出码/`result.json` 写终态。子进程为硬要求：`os.chdir(VENDOR)` 只发生在子进程内；GLFW 上下文偶发全挂（尖刺实测）只损失当前任务。
- **超时与重试**：子进程超时 120 秒（实测 ≤40s 的 3 倍余量）→ kill → 重试 1 次 → `RENDER_TIMEOUT`；非零退出且非业务异常 → 重试 1 次 → `RENDER_CRASHED`。业务失败（NeedsCorrection）不重试。
- **异常映射**（子进程内捕获）：`NeedsCorrection(reason)` → needs_correction；`FileNotFoundError`（动作资产缺失）→ `ASSET_MISSING`；超时 → `RENDER_TIMEOUT`；崩溃/未知 → `RENDER_CRASHED`/`INTERNAL`。
- **Mesa 开关**：环境变量 `RENDER_USE_MESA`，镜像内默认 `true`（容器 worker）；宿主脚本默认 `false`。
- **路径纪律**：子进程调用管线时所有路径绝对化；容器内统一以 `/data/out`、`/app/server/app/assets` 为根（chdir 后相对路径失效为已记录教训）。

## 7. 测试策略

- 单元测试（无容器依赖，Redis 用 fakeredis）：上传校验、幂等 jobId、状态机流转、异常→错误码映射。
- 契约测试：`ready` 必含 run/jump 双动画六键且帧尺寸相等；`needs_correction` 必含 reason+maskUrl+joints。
- 集成冒烟（脚本分区 `scripts/smoke_e2e.sh`）：compose 全栈拉起 → curl 上传 garlic.png → 轮询至 ready → 下载精灵表验证 PNG → PASS。
- 回归线：既有 8 个管线测试保持全绿，服务层测试独立新增。

## 8. 不在本设计范围

- 对象存储与签名 URL（演示阶段升级，接口 URL 抽象已预留）。
- needs_correction 后的"修正回写"接口（骨架编辑提交属 P0 后续迭代）。
- 关卡接口（P0 任务 4-5）、Unity 客户端（任务 6-7）。
