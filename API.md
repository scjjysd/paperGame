# 纸上游戏机 · 服务端接口文档（Unity 客户端用）

孩子在纸上画的火柴人 → 上传 → 服务端异步渲染 → 返回透明 RGBA 精灵表 + 切帧元数据 → Unity 直接切帧播放。

**契约唯一真源是 `app/contracts.py`**。本文档按该文件与实际代码路径编写，字段有出入时以代码为准。Unity 侧 `GameContracts.cs` 应照它逐字镜像（第 7 节给出 DTO）。

最后核对：2026-09-10。本次变更：① 服务端已配置 CORS；② 所有 `*Url` 改为**完整地址**（含 scheme+host）；③ 错误体与非终态响应新增中文 `message`；④ 新增关卡审查端点（见 2.7）。

---

## 0. 快速上手

```
① POST /v1/characters        (multipart, 字段名 file)  →  202 {"jobId": "char_9c3ff81ce4ea", "statusUrl": "http://…", "viewUrl": "http://…"}
② GET  /v1/characters/{jobId}  每 1s 轮询（也可直接用①返回的 statusUrl）→  status 变为 ready（实测约 20s）
③ 按 animations.run / animations.jump 的 spriteSheetUrl 下载 PNG（已是完整地址，直接 GET），按 frameWidth 切帧
```

调试时直接打开①返回的 `viewUrl`（即 `GET /v1/characters/{jobId}/view`），一页看到原图、动画、标注、精灵表。

---

## 1. 通用约定

| 项 | 值 |
|---|---|
| Base URL | `http://<host>:8000`（docker compose 的 `api` 容器） |
| 响应体 | 一律 JSON（`/view` 例外，返回 `text/html`） |
| URL 字段 | 所有 `*Url`（`spriteSheetUrl`/`maskUrl`/`statusUrl`/`viewUrl`…）都是**完整地址**，可直接请求，不要再拼 Base URL |
| 基址来源 | 优先环境变量 `PUBLIC_BASE_URL`；未配置时按请求的 Host 推导（反向代理下请显式配置） |
| 跨域 | 已启用 CORS，默认允许所有来源；`CORS_ALLOW_ORIGINS` 可收窄，详见第 9 节 |
| 上传大小上限 | 10 MiB（`10 * 1024 * 1024`），超出即 400 |
| 接受的图片格式 | PNG、JPEG（按文件头前 4 字节嗅探，不看扩展名和 Content-Type） |
| 时间字段 | `updatedAt` 是 **UTC 且不带 `Z` 后缀**（如 `2026-09-05T15:19:12`），解析时务必按 UTC 处理；`renderedAt`（仅 `/detail`）带时区偏移 |
| 中文说明 | 所有响应都带 `message`：非终态是状态说明，`needs_correction` 是修正指引，`failed` 与错误响应是失败原因 |
| 错误体 | `{"code": "<错误码>", "message": "<中文原因>"}`，见第 6 节 |

**404 有两种**：job 不存在返回 `{"code":"JOB_NOT_FOUND"}`；路径写错返回 `{"code":"NOT_FOUND"}`。两者都带中文 `message`，客户端按 `code` 字段判断即可，不要只看状态码。

---

## 2. 端点

### 2.1 `GET /healthz`

存活探针。恒返回 `{"status":"ok"}`。不校验 Redis 与 TorchServe。

### 2.2 `POST /v1/characters` — 上传涂鸦，入队渲染

请求：`multipart/form-data`，**字段名必须是 `file`**。可选查询参数 `force=true` 强制重跑（见下）。

```
POST /v1/characters
Content-Type: multipart/form-data; boundary=...

--...
Content-Disposition: form-data; name="file"; filename="doodle.png"
Content-Type: image/png

<PNG 字节>
```

响应 `202 Accepted`：

```json
{
  "jobId": "char_9c3ff81ce4ea",
  "statusUrl": "http://localhost:8000/v1/characters/char_9c3ff81ce4ea",
  "viewUrl": "http://localhost:8000/v1/characters/char_9c3ff81ce4ea/view"
}
```

`statusUrl` 是轮询地址，`viewUrl` 是浏览器可直接打开的审查页 —— 两者都是完整地址。

**jobId 由文件内容决定**：`"char_" + sha256(文件字节).hexdigest()[:12]`。因此同一张图无论上传多少次都得到同一个 jobId，且已存在的任务不会重复入队 —— 客户端可以放心重传（弱网重试、用户重复点击）而不会产生重复渲染。已完成的任务重传后立即可查到终态。

**强制重跑 `?force=true`**：默认幂等短路下，同一张图不会重新渲染。加 `force=true` 可绕过短路：服务端丢弃该 jobId 的旧终态结果与磁盘产物（`result.json`、`run/jump.png`、`run/jump.gif`、`anno/`），状态重置为 `queued` 并重新入队，响应仍是 `202 {"jobId": ...}`（jobId 不变）。适用于「图没变但想重渲」「旧任务失败/needs_correction 想重试」的场景。注意：对正在 `processing` 的任务 force 会额外多跑一次，属预期行为。

错误（`message` 为服务端实际返回的中文原因，可直接展示给用户）：

| 状态码 | code | message | 触发条件 |
|---|---|---|---|
| 400 | `FILE_TOO_LARGE` | 图片超过 10 MiB 上限，请压缩后重传。 | 超过 10 MiB |
| 400 | `UNSUPPORTED_FORMAT` | 暂不支持 GIF / WEBP，请上传 PNG 或 JPEG 图片。 | 文件头是 GIF 或 WEBP |
| 400 | `NOT_AN_IMAGE` | 文件头不是已知图片格式，请上传 PNG 或 JPEG 图片。 | 文件头不是已知图片格式 |
| 503 | `QUEUE_UNAVAILABLE` | 任务队列（Redis）不可用，请稍后重试。 | Redis 不可达 |
| 422 | `INVALID_REQUEST` | 请求参数校验失败，请检查字段名、类型与取值。 | 缺 `file` 字段等框架级校验失败 |

### 2.3 `GET /v1/characters/{jobId}` — 轮询状态（Unity 生产逻辑只依赖这个端点）

五种状态。**终态一旦写入不可被覆盖**（`queued`/`processing` 是非终态，其余三个是终态）。

**① 非终态** — `queued` 排队中，`processing` 正在渲染。无进度百分比。

```json
{
  "status": "processing",
  "message": "渲染中，正在生成 run / jump 精灵表。",
  "statusUrl": "http://localhost:8000/v1/characters/char_9c3ff81ce4ea",
  "viewUrl": "http://localhost:8000/v1/characters/char_9c3ff81ce4ea/view",
  "updatedAt": "2026-09-05T15:19:12"
}
```

**② `ready`** — 渲染成功，可以下载精灵表了。`animations` 恒含且仅含 `run` 与 `jump` 两个键（契约校验器强制）。

```json
{
  "status": "ready",
  "message": "渲染完成，可按 spriteSheetUrl 下载精灵表。",
  "statusUrl": "http://localhost:8000/v1/characters/char_9c3ff81ce4ea",
  "viewUrl": "http://localhost:8000/v1/characters/char_9c3ff81ce4ea/view",
  "characterId": "char_9c3ff81ce4ea",
  "animations": {
    "run":  {"spriteSheetUrl": "http://localhost:8000/artifacts/char_9c3ff81ce4ea/run.png",
             "frameCount": 10, "fps": 15, "frameWidth": 241, "frameHeight": 275,
             "footAnchor": {"x": 120, "y": 275}},
    "jump": {"spriteSheetUrl": "http://localhost:8000/artifacts/char_9c3ff81ce4ea/jump.png",
             "frameCount": 7,  "fps": 15, "frameWidth": 241, "frameHeight": 275,
             "footAnchor": {"x": 120, "y": 275}}
  }
}
```

`AnimationMeta` 六个字段全部必填，含义见第 3 节。`spriteSheetUrl` 已是完整地址，**直接 GET 即可，不要再拼 Base URL**（服务端落盘的 `result.json` 里仍是相对路径，只在响应出口补全，因此历史任务换部署地址也不会失效）。

**③ `needs_correction`** — 认不出人形，或标注质量不过门禁，需要用户修图/改关节后重传。

```json
{
  "status": "needs_correction",
  "reason": "SKELETON_MISFIT",
  "message": "关节位置偏离墨迹过远，请对照 /view 页面的关节叠加图重画。",
  "maskUrl": "http://localhost:8000/artifacts/char_5a60409ca6d2/anno/mask.png",
  "joints": [{"name": "root", "loc": [289, 422], "parent": null}, "... 共 16 项"]
}
```

`reason` 六种取值（`message` 就是服务端返回的中文文案，取自 `contracts.REASON_MESSAGES`）：

| reason | message（中文原因） |
|---|---|
| `NO_HUMANOID` | 画面里没有找到人形，请只画一个完整的火柴人后重传。 |
| `NO_SKELETON` | 找到了人形但推不出骨架，请确保头、躯干与四肢线条清晰相连。 |
| `MULTIPLE_SKELETONS` | 画面里检出多个人形，请只保留一个主角后重传。 |
| `NO_CONTOUR` | 提不出闭合轮廓，请检查线条是否断裂或过于潦草。 |
| `SKELETON_MISFIT` | 关节位置偏离墨迹过远，请对照 /view 页面的关节叠加图重画。 |
| `ANALYZE_FAILED` | 标注服务调用失败，请稍后重试；持续失败请检查 TorchServe 是否健康。 |

`joints` 长度是 **16 或 0**：早期失败（如 `NO_HUMANOID`）时标注文件尚未生成，返回空数组，此时客户端只能提示重拍，没有可编辑的关节。关节表见第 4 节。

⚠️ **`maskUrl` 是无条件写死的，早期失败时该文件并不存在**（服务端已知行为）。客户端若要展示 mask，请改用 `/detail` 端点的 `annotation.maskUrl` —— 它按磁盘实际存在与否给出 `null`。

**④ `failed`** — 基础设施故障，重传可能成功。

```json
{
  "status": "failed",
  "code": "RENDER_CRASHED",
  "message": "渲染子进程异常退出或结果文件损坏，自动重试后仍失败。"
}
```

实际会出现的 `code`：`RENDER_TIMEOUT`（单次渲染超 120s）、`RENDER_CRASHED`（渲染子进程崩溃，或结果文件损坏；已自动重试 1 次仍失败）、`ASSET_MISSING`（渲染依赖的资产缺失）。中文原因取自 `contracts.ERROR_MESSAGES`。

**⑤ 未知 jobId** — `404` + `{"code":"JOB_NOT_FOUND","message":"任务不存在或已过期，请重新上传原图。"}`。

任务状态在 Redis 中保留 **24 小时**；过期后由磁盘快照 `out/jobs/{jobId}/result.json` 重建，所以终态通常仍能查到。快照损坏时视同不存在（404），客户端重传即可（幂等，同图同 jobId）。

### 2.4 `GET /artifacts/{jobId}/<相对路径>` — 静态产物

所有 `*Url` 字段都指向这里。当前 job 目录布局：

```
/artifacts/{jobId}/input.png          上传的原图（原始字节，未经处理）
/artifacts/{jobId}/run.png            run 精灵表（RGBA，横向拼接）
/artifacts/{jobId}/jump.png           jump 精灵表
/artifacts/{jobId}/run.gif            run 渲染原件（未裁切，审查用；历史任务可能没有）
/artifacts/{jobId}/jump.gif           jump 渲染原件
/artifacts/{jobId}/anno/mask.png      角色区域遮罩（L 灰度，白 = 保留）
/artifacts/{jobId}/anno/texture.png   贴图
/artifacts/{jobId}/anno/char_cfg.yaml 16 关节标注
```

目录本身不可列出（无索引页），只能按具体文件名取。产物不存在时返回 `404` + `{"code":"NOT_FOUND","message":"请求的路径不存在，请检查接口地址与 jobId。"}`（JSON，不再是框架默认的英文 `{"detail":"Not Found"}`）。

### 2.5 `GET /v1/characters/{jobId}/detail` — 全部产物汇总（审查用，非契约）

一次拿到原图、动图、标注、精灵表的全部 URL 与元数据。**这是给人和调试工具用的端点，字段可能随开发调整；Unity 生产逻辑请只依赖 2.3。**

与 2.3 的三个实质区别：
1. **任何状态都能调**（含 `queued`），缺失的产物一律为 `null`，不会给出坏链接；
2. `ready` 状态也返回 `annotation.joints`（2.3 只在 `needs_correction` 时返回）；
3. 所有 URL 都按**磁盘实际存在**判定，不像 2.3 的 `maskUrl` 会指向不存在的文件。

```json
{
  "characterId": "char_9c3ff81ce4ea",
  "status": "ready",
  "reason": null,
  "code": null,
  "message": null,
  "renderedAt": "2026-09-05T23:19:56+08:00",
  "viewUrl": "http://localhost:8000/v1/characters/char_9c3ff81ce4ea/view",
  "detailUrl": "http://localhost:8000/v1/characters/char_9c3ff81ce4ea/detail",
  "inputUrl": "http://localhost:8000/artifacts/char_9c3ff81ce4ea/input.png",
  "annotation": {
    "maskUrl": "http://localhost:8000/artifacts/char_9c3ff81ce4ea/anno/mask.png",
    "textureUrl": "http://localhost:8000/artifacts/char_9c3ff81ce4ea/anno/texture.png",
    "charCfgUrl": "http://localhost:8000/artifacts/char_9c3ff81ce4ea/anno/char_cfg.yaml",
    "width": 517,
    "height": 595,
    "joints": [{"name": "root", "loc": [289, 422], "parent": null}, "... 共 16 项"]
  },
  "animations": {
    "run": {
      "spriteSheetUrl": "http://localhost:8000/artifacts/char_9c3ff81ce4ea/run.png",
      "frameCount": 10, "fps": 15, "frameWidth": 241, "frameHeight": 275,
      "footAnchor": {"x": 120, "y": 275},
      "gifUrl": "http://localhost:8000/artifacts/char_9c3ff81ce4ea/run.gif"
    },
    "jump": { "... 同上" }
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | string | 五态之一，与 2.3 一致 |
| `reason` / `code` | string \| null | 分别对应 `needs_correction` / `failed`，其余状态为 `null` |
| `message` | string \| null | `reason` / `code` 的中文原因；`ready` 等无错时为 `null` |
| `renderedAt` | string \| null | 产物落盘时间，带时区偏移；尚无产物时为 `null` |
| `inputUrl` | string \| null | 上传的原图（完整地址） |
| `annotation.width` / `.height` | int \| null | 标注画布尺寸，**`joints[].loc` 的坐标系就是它**；无标注时为 `null` |
| `annotation.joints` | array | 16 项或空数组，见第 4 节 |
| `animations` | object | 只在 `ready` 时非空；`{}` 表示无动画产物 |
| `animations.*.gifUrl` | string \| null | 未裁切的渲染原件。**历史任务为 `null`**（GIF 留存是后加的能力，早于该改动完成的任务没有此文件） |

未知 jobId 同样返回 `404` + `{"code":"JOB_NOT_FOUND","message":"…"}`。

### 2.6 `GET /v1/characters/{jobId}/view` — 单页审查 HTML

返回 `text/html`，浏览器直接打开即可目视验收。内容与 2.5 同源，一页含：

- 原图；
- **run / jump 的精灵表实时播放** —— 用 CSS `steps()` 按 `frameCount`/`fps` 驱动，切帧参数与 Unity 完全同一套，所见即真机效果；
- 未裁切的 GIF 原件（有则显示，供对照裁切前后）；
- texture 上叠红色 mask + 青色关节点：**红区之外的黑笔画会在成片里彻底消失**，一眼看出哪些笔画丢了、关节吸附到了哪里；
- Unity 精灵表原图（点击看原尺寸）+ 帧数 / 帧尺寸 / fps / 脚底锚点。

产物缺失时显示占位块，不会产生坏图链接。页面顶部徽章直接写出中文原因（如 `needs_correction · NO_HUMANOID（画面里没有找到人形…）`）。未知 jobId 返回 `404` + `{"code":"JOB_NOT_FOUND"}`（JSON，不是 HTML）。

### 2.7 `POST /v1/levels` — 上传关卡图，入队解析

请求：`multipart/form-data`，**字段名必须是 `file`**。可选查询参数 `force=true` 强制重跑（见下）。

```
POST /v1/levels
Content-Type: multipart/form-data; boundary=...

--...
Content-Disposition: form-data; name="file"; filename="paper-level.jpg"
Content-Type: image/jpeg

<图片字节>
```

可选表单字段：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `file` | binary | 是 | — | JPEG 或 PNG 图片 |
| `schemaVersion` | string | 否 | `1.0` | 客户端期望的关卡协议版本，当前只接受 `1.0` |
| `playabilityProfile` | JSON string | 否 | 服务默认值 | 角色能力参数（仅影响可玩性分析，不影响几何识别） |

`playabilityProfile` 示例与字段含义：

```json
{
  "profileVersion": "unity-c1-test-1",
  "maxJumpRisePixels": 150,
  "maxJumpDistancePixels": 230,
  "characterWidthPixels": 32,
  "characterHeightPixels": 58,
  "landingTolerancePixels": 6
}
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `profileVersion` | string | 1～64 字符 | 能力配置版本，便于复现分析 |
| `maxJumpRisePixels` | integer | `> 0` | 角色脚底可达到的最大垂直上升高度（像素） |
| `maxJumpDistancePixels` | integer | `> 0` | 一次跳跃允许的最大水平位移（像素） |
| `characterWidthPixels` | integer | `> 0` | 角色碰撞体宽度（像素） |
| `characterHeightPixels` | integer | `> 0` | 角色碰撞体高度（像素） |
| `landingTolerancePixels` | integer | `>= 0` | 着陆边界允许的误差（像素） |

响应 `202 Accepted`：

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "queued",
  "message": "排队中，等待关卡 worker 领取。",
  "statusUrl": "http://localhost:8000/v1/levels/level_4e91a63bcf21",
  "viewUrl": "http://localhost:8000/v1/levels/level_4e91a63bcf21/view",
  "createdAt": "2026-09-07T08:30:15"
}
```

`statusUrl` 是轮询地址，`viewUrl` 是浏览器可直接打开的审查页 —— 两者都是完整地址。

**jobId 由文件内容 + 能力参数 + 算法主版本共同决定**：`"level_" + sha256(文件字节 + 规范化 profile JSON + 算法主版本).hexdigest()[:12]`。因此同一张图 + 同一套参数无论上传多少次都得到同一个 jobId，已存在的任务不会重复入队。

**强制重跑 `?force=true`**：对已处于终态的任务，force 会清理旧终态与磁盘产物（`level.json`、`analysis.json`、`rectified.png` 等），状态重置为 `queued` 并重新入队。对正在 `processing` 的任务 force 会被拒绝（`409 JOB_IN_PROGRESS`），需等当前任务进入终态后再试。

**图片限制**：JPEG 或 PNG（按文件头嗅探），单边 800～12,000 像素，总像素不超过 4000 万，不接受多帧动图，文件不超过 10 MiB。上传后服务端会自动做 EXIF 转置与模式归一化（统一输出 PNG），因此 `input.png` 可能与原始上传字节不同。

错误（`message` 为服务端实际返回的中文原因，可直接展示给用户；关卡链路错误体为信封格式 `{"error": {"code", "message", "retryable", "requestId"}}`）：

| 状态码 | code | message | 触发条件 |
|---|---|---|---|
| 400 | `UNSUPPORTED_SCHEMA_VERSION` | 服务端不支持该 schemaVersion，当前只接受 1.0。 | `schemaVersion` 不是 `1.0` |
| 400 | `INVALID_PLAYABILITY_PROFILE` | 角色能力参数缺失或越界，请对照文档校验 playabilityProfile。 | `playabilityProfile` 不是合法 JSON 或字段越界 |
| 413 | `FILE_TOO_LARGE` | 图片不能超过 10 MiB，请压缩后重传。 | 超过 10 MiB |
| 415 | `UNSUPPORTED_IMAGE_FORMAT` | 仅支持 JPEG 或 PNG，请转换格式后重传。 | 文件头不是 JPEG 或 PNG |
| 422 | `IMAGE_DECODE_FAILED` | 图片无法安全解码（具体原因见 `details.reason`）。 | 动图 / 边长不在 800~12000 / 超 4000 万像素 / 文件损坏 |
| 409 | `JOB_IN_PROGRESS` | 任务正在处理中，不能强制重跑，请等当前任务进入终态。 | `force=true` 但任务尚未终态 |
| 503 | `QUEUE_UNAVAILABLE` | 任务队列（Redis）不可用，请稍后重试。 | Redis 不可达 |

### 2.8 `GET /v1/levels/{jobId}` — 轮询关卡状态（Unity 生产逻辑只依赖这个端点）

六种状态。`queued`/`processing` 是非终态，`ready`/`needs_fix`/`needs_review`/`failed` 是终态。

**① 非终态** — `queued` 排队中，`processing` 正在解析。带 `progress` 指示当前阶段。

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "processing",
  "message": "解析中，正在拉正纸张、识别平台与终点。",
  "statusUrl": "http://localhost:8000/v1/levels/level_4e91a63bcf21",
  "viewUrl": "http://localhost:8000/v1/levels/level_4e91a63bcf21/view",
  "progress": {
    "stage": "detecting_platforms",
    "stageLabel": "识别平台",
    "percent": 55
  },
  "createdAt": "2026-09-07T08:30:15",
  "updatedAt": "2026-09-07T08:30:18"
}
```

`progress.stage` 是当前解析阶段（英文枚举值，不得依赖完整枚举），`progress.stageLabel` 是服务端给出的中文阶段名。当前阶段取值：

| stage | stageLabel |
|---|---|
| `waiting` | 等待中 |
| `validating_upload` | 校验上传图 |
| `rectifying_paper` | 拉正纸张 |
| `detecting_platforms` | 识别平台 |
| `detecting_goal` | 识别终点 |
| `semantic_review` | 语义复核 |
| `validating_geometry` | 校验几何 |
| `analyzing_playability` | 分析可玩性 |
| `publishing_artifacts` | 发布产物 |

**② `ready`** — 解析完成，关卡可玩。响应体含完整的 `result`（`level` + `analysis` + `artifacts`），字段与 `level-image-to-json-api.md` 第 6 节一致。

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "ready",
  "message": "解析完成，关卡可玩。",
  "statusUrl": "http://localhost:8000/v1/levels/level_4e91a63bcf21",
  "viewUrl": "http://localhost:8000/v1/levels/level_4e91a63bcf21/view",
  "schemaVersion": "1.0",
  "algorithmVersion": "level-parser-1.0.0",
  "createdAt": "2026-09-07T08:30:15",
  "updatedAt": "2026-09-07T08:30:22",
  "result": {
    "level": { "…见 level-image-to-json-api.md 第 7 节" },
    "analysis": { "…见 level-image-to-json-api.md 第 17 节" },
    "artifacts": {
      "inputUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/input.png",
      "rectifiedImageUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/rectified.png",
      "overlayImageUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/overlay.png",
      "levelJsonUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/level.json",
      "analysisJsonUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/analysis.json"
    }
  }
}
```

`result` 三个子对象全部必填：`level`（权威关卡几何，含 `schemaVersion` / `coordinateSystem` / `canvas` / `background` / `playerStart` / `platforms` / `goalRegion`）、`analysis`（可玩性分析，含 `playability` / `profile` / `path` / `warnings`）、`artifacts`（产物地址，`*Url` 已是完整地址）。

**③ `needs_fix`** — 识别可靠，但按提交的能力参数草图不可玩（跳不过去或终点悬空）。响应体结构与 `ready` 相同（含完整 `result`），区别在于 `analysis.playability` 为 `"unreachable"` 且 `analysis.warnings` 非空。

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "needs_fix",
  "message": "解析完成，但存在可玩性问题（跳不过去或终点悬空）。",
  "result": {
    "level": { "…同上" },
    "analysis": {
      "playability": "unreachable",
      "warnings": [
        {
          "code": "JUMP_GAP_TOO_HIGH",
          "message": "platform_003 到 platform_004 的高度差超过当前跳跃能力。",
          "relatedPlatformIds": ["platform_003", "platform_004"],
          "requiredValuePixels": 184,
          "availableValuePixels": 150
        }
      ]
    },
    "artifacts": { "…同上" }
  }
}
```

可玩性告警码（`analysis.warnings[].code`）：

| code | 说明 |
|---|---|
| `START_NOT_SUPPORTED` | 出生点没有可靠承载平台 |
| `GOAL_NOT_SUPPORTED` | 终点不在可到达平台附近 |
| `JUMP_GAP_TOO_HIGH` | 垂直高度超过角色能力 |
| `JUMP_GAP_TOO_WIDE` | 水平间距超过角色能力 |
| `LANDING_AREA_TOO_SHORT` | 平台可着陆长度不足 |
| `NO_PATH_TO_GOAL` | 可达图中不存在完整路径 |

**④ `needs_review`** — 识别存在歧义（纸张/平台/终点/方向不确定），不能发布权威结果。不含 `result.level`，改给 `review` 与 `artifacts`。

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "needs_review",
  "message": "识别证据不足，需要人工复核或重拍。",
  "schemaVersion": "1.0",
  "algorithmVersion": "level-parser-1.0.0",
  "review": {
    "reason": "AMBIGUOUS_GOAL",
    "message": "检测到两个置信度接近的终点旗帜候选，请重拍或人工确认。",
    "candidates": [
      {"id": "goal_candidate_01", "type": "goal",
       "region": {"x": 1080, "y": 130, "width": 88, "height": 120}, "confidence": 0.62}
    ],
    "suggestions": ["保证纸张边缘完整出现在画面中。", "只保留一个终点旗帜。"]
  },
  "artifacts": {
    "rectifiedImageUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/rectified.png",
    "overlayImageUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/overlay.png"
  }
}
```

`review.reason` 推荐取值：`PAPER_NOT_FOUND`、`PAPER_AMBIGUOUS`、`PAPER_OCCLUDED`、`ORIENTATION_AMBIGUOUS`、`NO_PLATFORM_DETECTED`、`PLATFORM_GEOMETRY_AMBIGUOUS`、`GOAL_NOT_FOUND`、`AMBIGUOUS_GOAL`、`START_PLATFORM_NOT_FOUND`、`LOW_CONFIDENCE`。

**⑤ `failed`** — 技术故障（解析进程异常或依赖不可用），可重试。

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "failed",
  "message": "解析失败，属于技术故障，可重试。",
  "error": {
    "code": "PROCESSING_CRASHED",
    "message": "关卡解析进程异常退出或结果不符合契约，已自动重试一次。",
    "retryable": true,
    "requestId": "req_01J7BE8WZC6Y2K5Q74H86PRB01"
  },
  "createdAt": "2026-09-07T08:30:15",
  "updatedAt": "2026-09-07T08:30:22"
}
```

实际会出现的 `error.code`：`PROCESSING_TIMEOUT`（单次解析超 90 秒，已自动重试 1 次）、`PROCESSING_CRASHED`（解析进程崩溃，已自动重试 1 次）。

**⑥ 未知 jobId** — `404` + `{"error":{"code":"JOB_NOT_FOUND","message":"任务不存在或已过期，请重新上传关卡图。","retryable":false,"requestId":"req_…"}}`。

关卡链路的错误体一律是信封格式 `{"error": {"code", "message", "retryable", "requestId"}}`，与角色链路的扁平体 `{"code", "message"}` 不同，客户端按各自契约解析即可。

### 2.9 `GET /v1/levels/{jobId}/detail` — 关卡全部产物汇总（审查用，非契约）

一次拿到关卡图、识别几何、可玩性分析与全部产物的完整地址。**这是给人和调试工具用的端点，字段可能随开发调整；Unity 生产逻辑请只依赖 2.8。**

与 2.8 的三个实质区别：
1. **任何状态都能调**（含 `queued`/`processing`，此时 `progress.stageLabel` 给中文阶段名）；
2. 产物按**磁盘实际存在**判定，缺失一律 `null`，不会给出坏链接；
3. Redis 过期后仍能从磁盘的 `level.json` / `analysis.json` 重建预览。

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "ready",
  "statusMessage": "解析完成，关卡可玩。",
  "message": "解析完成，关卡可玩。",
  "createdAt": "2026-09-07T08:30:15",
  "updatedAt": "2026-09-07T08:30:22",
  "parsedAt": "2026-09-07T16:30:22+08:00",
  "schemaVersion": "1.0",
  "algorithmVersion": "level-parser-1.0.0",
  "statusUrl": "http://localhost:8000/v1/levels/level_4e91a63bcf21",
  "detailUrl": "http://localhost:8000/v1/levels/level_4e91a63bcf21/detail",
  "viewUrl": "http://localhost:8000/v1/levels/level_4e91a63bcf21/view",
  "progress": null,
  "canvas": {"width": 1245, "height": 810},
  "artifacts": {
    "inputUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/input.png",
    "rectifiedImageUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/rectified.png",
    "overlayImageUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/overlay.png",
    "paperMaskUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/paper-mask.png",
    "inkMaskUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/ink-mask.png",
    "levelJsonUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/level.json",
    "analysisJsonUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/analysis.json",
    "transformJsonUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/transform.json",
    "llmAuditUrl": null,
    "resultJsonUrl": "http://localhost:8000/artifacts/level_4e91a63bcf21/result.json"
  },
  "level": {"…权威 level 对象，与 level-image-to-json-api.md 第 7 节一致"},
  "analysis": {"…可玩性分析"},
  "review": null,
  "error": null,
  "platforms": [
    {"id": "platform_001", "start": {"x": 83, "y": 681}, "end": {"x": 298, "y": 681},
     "length": 215, "confidence": 0.98, "onPath": true}
  ],
  "playability": "playable",
  "path": ["platform_001", "platform_002"],
  "warnings": [],
  "profile": {"profileVersion": "unity-c1-test-1", "maxJumpRisePixels": 150},
  "reviewReason": null
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `statusMessage` | string | 状态的中文说明 |
| `message` | string | 综合中文原因（失败原因 > 复核原因 > 可玩性告警 > 状态说明） |
| `parsedAt` | string \| null | 产物落盘时间，带时区偏移；尚无产物时为 `null` |
| `canvas` | object | 画布尺寸（优先取契约，缺失时读拉正图实际尺寸） |
| `artifacts` | object | 全部产物完整地址，按磁盘实际存在判定，缺失为 `null` |
| `level` | object \| null | 权威关卡几何（`ready` / `needs_fix` 时非空） |
| `analysis` | object \| null | 可玩性分析（含 `playability` / `warnings` / `profile`） |
| `review` | object \| null | 复核信息（仅 `needs_review` 时非空） |
| `error` | object \| null | 错误信息（仅 `failed` 时非空） |
| `platforms` | array | 平台清单（含 `length` 与 `onPath`，方便审查页直接展示） |
| `reviewReason` | string \| null | 复核原因（`needs_review` 时非空） |

关卡产物目录布局：

```
/artifacts/{jobId}/input.png          上传原图（已归一化为 PNG）
/artifacts/{jobId}/rectified.png      透视拉正图（契约坐标系即此图）
/artifacts/{jobId}/overlay.png        服务端生成的识别叠加图
/artifacts/{jobId}/paper-mask.png     纸张区域遮罩
/artifacts/{jobId}/ink-mask.png       墨迹遮罩（平台证据来源）
/artifacts/{jobId}/level.json         关卡几何 JSON（契约产物）
/artifacts/{jobId}/analysis.json      可玩性分析 JSON
/artifacts/{jobId}/transform.json     透视变换矩阵
/artifacts/{jobId}/llm-audit.json     LLM 语义复核审计（有则存在）
/artifacts/{jobId}/result.json        终态快照
```

未知 jobId 返回 `404` + `{"error":{"code":"JOB_NOT_FOUND","message":"…"}}`（关卡链路的信封体）。

### 2.10 `GET /v1/levels/{jobId}/view` — 关卡单页审查 HTML

返回 `text/html`，浏览器直接打开即可目视验收。内容与 2.9 同源，一页含：

- 上传原图、透视拉正图、服务端叠加图、纸张/墨迹遮罩；
- **浏览器端重绘的识别叠加图**：绿线 = 可达路径上的平台，橙线 = 未纳入路径，红框 = 终点区域与复核候选，紫点 = 出生点（不依赖服务端的 `overlay.png`，缺它也能预览）；
- 平台清单表（起点/终点/长度/置信度/是否在可达路径上）；
- 可玩性分析：判定、出生/终点平台、路径、角色能力参数、逐条中文告警（需要多少像素 vs 实际多少像素）；
- `needs_review` 时列出复核原因、候选区域与重拍建议；`failed` 时列出错误码与中文原因；
- 产物完整地址清单（含 `level.json` / `analysis.json` / `transform.json` / `llm-audit.json`）。

产物缺失时显示占位块，不会产生坏图链接。页面顶部徽章直接写出中文原因。未知 jobId 返回 `404` + `{"error":{"code":"JOB_NOT_FOUND"}}`（JSON，不是 HTML）。

---

## 3. 精灵表怎么切（Unity 关键）

精灵表是**单行横向拼接的 RGBA PNG**：总宽 = `frameWidth × frameCount`，总高 = `frameHeight`，逐帧内容水平居中、底部对齐。

第 `i` 帧（0-based）的源矩形：

```
x = i * frameWidth,  y = 0,  width = frameWidth,  height = frameHeight
```

**run 与 jump 的 `frameWidth` / `frameHeight` 必然相同** —— 服务端取两个动作全部帧内容包围盒的并集作为统一帧尺寸，正是为了让 Unity 能用同一套切帧参数，只换纹理。可以放心把它当作角色级常量缓存。

`footAnchor` 是脚底锚点，恒为 `{x: frameWidth / 2, y: frameHeight}`（帧内水平中点 + 底边），坐标系是**自左上向右下的像素坐标**。把它对齐地面即可让角色站稳，跨帧不抖。

单帧时长 = `1 / fps`。当前 `fps` 固定为 **15**。

```csharp
// 精灵表 PNG 已是 RGBA；导入设置：Alpha Is Transparency = true, Wrap Mode = Clamp
Sprite[] Slice(Texture2D sheet, AnimationMeta meta) {
    // footAnchor 是自顶向下的像素坐标，Unity pivot 是自底向上的归一化坐标
    var pivot = new Vector2(meta.footAnchor.x / (float)meta.frameWidth,
                            1f - meta.footAnchor.y / (float)meta.frameHeight);  // == (0.5f, 0f)
    var frames = new Sprite[meta.frameCount];
    for (int i = 0; i < meta.frameCount; i++) {
        var rect = new Rect(i * meta.frameWidth, 0, meta.frameWidth, meta.frameHeight);
        frames[i] = Sprite.Create(sheet, rect, pivot, pixelsPerUnit: 100f);
    }
    return frames;   // 逐帧间隔 1f / meta.fps
}
```

---

## 4. 关节表（`needs_correction` 的编辑目标）

固定 16 个关节，名称与顺序如下（`parent` 为 `null` 者是根）。`loc` 是 `[x, y]` 像素坐标，原点左上、y 向下，坐标系是 `annotation.width × annotation.height`（也就是 `mask.png` / `texture.png` 的尺寸）。

| # | name | parent | | # | name | parent |
|---|---|---|---|---|---|---|
| 0 | `root` | `null` | | 8 | `left_elbow` | `left_shoulder` |
| 1 | `hip` | `root` | | 9 | `left_hand` | `left_elbow` |
| 2 | `torso` | `hip` | | 10 | `right_hip` | `root` |
| 3 | `neck` | `torso` | | 11 | `right_knee` | `right_hip` |
| 4 | `right_shoulder` | `torso` | | 12 | `right_foot` | `right_knee` |
| 5 | `right_elbow` | `right_shoulder` | | 13 | `left_hip` | `root` |
| 6 | `right_hand` | `right_elbow` | | 14 | `left_knee` | `left_hip` |
| 7 | `left_shoulder` | `torso` | | 15 | `left_foot` | `left_knee` |

服务端当前**不接受关节回传**（无 PATCH 端点）：客户端引导用户修图后重新 `POST` 即可 —— 但注意幂等是按**文件内容**算的，图必须真的变了才会产生新 jobId 并重新渲染。

---

## 5. 状态机与轮询

```
POST ──202──> queued ──> processing ──┬──> ready              （终态）
                                       ├──> needs_correction  （终态）
                                       └──> failed            （终态）
```

- 终态不可被覆盖，客户端轮到终态即可停止；
- 单次渲染超时 **120 s**，基础设施故障自动重试 1 次（最多 2 次尝试），业务失败（`needs_correction`）不重试；
- 实测端到端约 **20 s**（单机、无排队）。建议轮询间隔 1 s，客户端超时按最坏情况留到 5 分钟以上；
- 非终态响应不含进度百分比，UI 请用不定态进度指示。

---

## 6. 错误码总表

错误体一律是 `{"code": "<码>", "message": "<中文原因>"}`；`message` 取自 `contracts.ERROR_MESSAGES`，可直接展示给用户。

| code | 出现位置 | message（中文原因） |
|---|---|---|
| `FILE_TOO_LARGE` | POST 400 | 图片超过 10 MiB 上限，请压缩后重传。 |
| `NOT_AN_IMAGE` | POST 400 | 文件头不是已知图片格式，请上传 PNG 或 JPEG 图片。 |
| `UNSUPPORTED_FORMAT` | POST 400 | 暂不支持 GIF / WEBP，请上传 PNG 或 JPEG 图片。 |
| `JOB_NOT_FOUND` | GET 404 | 任务不存在或已过期，请重新上传原图。 |
| `QUEUE_UNAVAILABLE` | POST/GET 503 | 任务队列（Redis）不可用，请稍后重试。 |
| `RENDER_TIMEOUT` | `failed.code` | 单次渲染超过 120 秒，自动重试后仍失败，请重试或简化画面。 |
| `RENDER_CRASHED` | `failed.code` | 渲染子进程异常退出或结果文件损坏，自动重试后仍失败。 |
| `ASSET_MISSING` | `failed.code` | 渲染依赖的资产缺失，请检查服务端部署是否完整。 |
| `INTERNAL` | 未捕获异常 500 | 服务端内部错误，请查看 out/logs 下的当天日志。 |

框架级错误（不属于业务契约，但同样带中文 `message`）：

| code | 状态码 | 触发条件 |
|---|---|---|
| `NOT_FOUND` | 404 | 路径写错、方法不存在、产物文件不在磁盘上 |
| `METHOD_NOT_ALLOWED` | 405 | 路径对但方法不对（如 `DELETE /v1/characters`） |
| `INVALID_REQUEST` | 400 / 422 | 缺必填字段、类型不对等参数校验失败（`details.errors` 给字段级细节） |
| `HTTP_<状态码>` | 其余 | 其他框架抛出的 HTTP 异常，按状态码给中文说明 |

框架级错误体同时带信封键，两条链路的客户端都能直接读：

```json
{
  "code": "NOT_FOUND",
  "message": "请求的路径不存在，请检查接口地址与 jobId。",
  "error": {"code": "NOT_FOUND", "message": "同上", "retryable": false, "requestId": "req_…"}
}
```

---

## 7. C# 契约镜像

**用 Newtonsoft.Json**（`com.unity.nuget.newtonsoft-json`）。Unity 自带的 `JsonUtility` 不支持 `Dictionary`，`animations` 会解析成空 —— 这是最容易踩的坑。

```csharp
// GameContracts.cs —— 逐字镜像 app/contracts.py，改契约需前后端共同确认
using System.Collections.Generic;

public static class GameContracts {
    public const long MaxUploadBytes = 10L * 1024 * 1024;
    public const string Queued = "queued", Processing = "processing", Ready = "ready",
                        NeedsCorrection = "needs_correction", Failed = "failed";

    public static bool IsTerminal(string status) =>
        status == Ready || status == NeedsCorrection || status == Failed;
}

public class FootAnchor { public int x, y; }

public class AnimationMeta {
    public string spriteSheetUrl;
    public int frameCount, fps, frameWidth, frameHeight;
    public FootAnchor footAnchor;
}

public class Joint {
    public string name;
    public int[] loc;       // [x, y]，坐标系见第 4 节
    public string parent;   // null = 根节点
}

public class JobAccepted {
    public string jobId;
    public string statusUrl;   // 完整地址，轮询直接用它
    public string viewUrl;     // 完整地址，浏览器可直开的审查页
}

public class ErrorBody {
    public string code;
    public string message;     // 中文原因，可直接上屏
}

// 五态共用一个类，按 status 分派：无关字段为 null，一次反序列化即可
public class CharacterStatus {
    public string status;
    public string message;                                // 任意状态都有：中文状态/失败原因
    public string statusUrl, viewUrl;                      // 完整地址
    public string updatedAt;                             // 非终态。UTC，无 Z 后缀
    public string characterId;                           // ready
    public Dictionary<string, AnimationMeta> animations;  // ready。含且仅含 "run" / "jump"
    public string reason;                                 // needs_correction
    public string maskUrl;                                // needs_correction。文件可能不存在
    public List<Joint> joints;                            // needs_correction。16 项或空
    public string code;                                   // failed
}

// *Url 已是完整地址；兼容旧服务端的站内相对路径，不要直接 baseUrl + url
public static class Urls {
    public static string Resolve(string baseUrl, string url) =>
        string.IsNullOrEmpty(url) ? url
        : (url.StartsWith("http://") || url.StartsWith("https://")) ? url : baseUrl.TrimEnd('/') + url;
}
```

端到端流程（协程，省略错误分支的 UI 处理）：

```csharp
using System.Collections;
using Newtonsoft.Json;
using UnityEngine;
using UnityEngine.Networking;

public IEnumerator UploadAndBuild(string baseUrl, byte[] png) {
    // ① 上传。字段名必须是 file
    var form = new WWWForm();
    form.AddBinaryData("file", png, "doodle.png", "image/png");
    using var post = UnityWebRequest.Post($"{baseUrl}/v1/characters", form);
    yield return post.SendWebRequest();
    if (post.result != UnityWebRequest.Result.Success) {
        var err = JsonConvert.DeserializeObject<ErrorBody>(post.downloadHandler.text);
        Debug.LogError($"上传失败: {err?.code} {err?.message}");   // message 是中文原因，可直接上屏
        yield break;
    }
    var accepted = JsonConvert.DeserializeObject<JobAccepted>(post.downloadHandler.text);
    string jobId = accepted.jobId;

    // ② 轮询到终态。实测约 20s；最坏留 5 分钟以上
    CharacterStatus st = null;
    for (float t = 0; t < 300f; t += 1f) {
        using var get = UnityWebRequest.Get(Urls.Resolve(baseUrl, accepted.statusUrl));
        yield return get.SendWebRequest();
        st = JsonConvert.DeserializeObject<CharacterStatus>(get.downloadHandler.text);
        if (st != null && GameContracts.IsTerminal(st.status)) break;
        yield return new WaitForSeconds(1f);
    }
    if (st == null || st.status != GameContracts.Ready) {
        Debug.LogWarning($"未成功: {st?.status} {st?.message}");   // 中文原因，引导重拍或重传
        yield break;
    }

    // ③ 下载精灵表并切帧。run 与 jump 的帧尺寸必然相同，可共用切帧参数
    foreach (var kv in st.animations) {
        var meta = kv.Value;
        using var tex = UnityWebRequestTexture.GetTexture(Urls.Resolve(baseUrl, meta.spriteSheetUrl));
        yield return tex.SendWebRequest();
        var sheet = DownloadHandlerTexture.GetContent(tex);
        sheet.wrapMode = TextureWrapMode.Clamp;
        Sprite[] frames = Slice(sheet, meta);        // 见第 3 节
        // kv.Key == "run" / "jump"，逐帧间隔 1f / meta.fps
    }
}
```

---

## 8. 已知边界

按影响排序，第一条会直接阻塞公网部署：

1. **全部端点无鉴权**。`/artifacts/**`（含 `anno/` 全部中间产物）、`/detail`、`/view` 都是公开可读的，任何知道 jobId 的人都能取到图；CORS 又默认放开所有来源。当前只适用于内网与本地开发，公网部署前必须加访问控制并把 `CORS_ALLOW_ORIGINS` 收窄。
2. **没有列表端点**。拿不到"所有角色"清单，只能按已知 jobId 查询。jobId 需客户端自行持久化。
3. **关节不可回传**。`needs_correction` 时服务端只给出关节位置供展示，没有 PATCH 端点；修正靠引导用户改图后重新上传。注意幂等按文件内容计算，图没变则 jobId 不变、不会重渲染。
4. **`gifUrl` 对历史任务为 `null`**。GIF 留存是后加的能力，在该改动之前完成的任务目录里没有这个文件。仅影响审查视图，不影响 Unity 取精灵表。
5. **不要下载 `anno/image.png`**。它是标注服务存的原图副本，实测可达 19 MB。需要原图请用 `inputUrl`（`input.png`）。
6. **产物文件内部的 URL 仍是相对路径**。只有接口响应会补全成完整地址；直接下载的 `level.json` 里 `background.imageUrl` 依旧是 `/artifacts/…`（为了让产物跳机器可用）。客户端请用接口响应里的地址，或对文件内容自行拼 Base URL。
7. **关卡链路另有契约文档**。本文以角色链路为主；关卡（`/v1/levels`）的上传/轮询/终态契约见 `level-image-to-json-api.md`，审查端点见 2.7。

---

## 9. 跨域、完整地址与服务端日志（运维/联调）

### 9.1 跨域

已挂 `CORSMiddleware`，全部走环境变量（compose 里已接好，`.env` 有注释模板）：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CORS_ALLOW_ORIGINS` | `*` | 逗号分隔的来源白名单；`*` 表示全放开 |
| `CORS_ALLOW_ORIGIN_REGEX` | 空 | 来源正则，适合多子域 |
| `CORS_ALLOW_METHODS` | `*` | 逗号分隔，如 `GET,POST` |
| `CORS_ALLOW_HEADERS` | `*` | 逗号分隔，如 `Content-Type,X-Trace-Id` |
| `CORS_EXPOSE_HEADERS` | `Content-Length,Content-Type` | 允许前端 JS 读到的响应头 |
| `CORS_ALLOW_CREDENTIALS` | `false` | 要带 Cookie/Authorization 就置 `true`，**但必须同时把来源配成具体值** |
| `CORS_MAX_AGE` | `600` | 预检结果缓存秒数 |

两个坑：① 来源为 `*` 时浏览器不允许携带凭证，服务端会自动把 `allow_credentials` 关掉并在日志里告警；② CORS 中间件在栈最外层，预检 `OPTIONS` 不会进业务逻辑。

### 9.2 完整地址

响应里的站内路径在**出口处**补全成完整地址，基址优先取 `PUBLIC_BASE_URL`，未配置时按请求的 Host 推导。反向代理/端口映射（如 NAS 上把 8000 映射到别的端口）下必须显式配置，否则拿到的是内部地址：

```bash
PUBLIC_BASE_URL=http://192.168.1.20:8000 docker compose up -d api
```

磁盘上的 `result.json` / `level.json` 始终存相对路径：它们跟部署地址无关，换机器、换端口后历史任务依旧可用。

### 9.3 日志

全链路中文日志，同时输出到控制台（`docker logs`）与文件：

```
out/logs/api-2026-09-10.log              接口进程（含每个请求的进出与耗时）
out/logs/character-worker-2026-09-10.log  角色渲染 worker
out/logs/render-runner-2026-09-10.log     角色渲染子进程
out/logs/level-worker-2026-09-10.log      关卡解析 worker
out/logs/level-runner-2026-09-10.log      关卡解析子进程
```

容器内写 `/data/out/logs`，已由 compose 的 `./out:/data/out` 挂到宿主机 `out/logs`。按本地日期分文件，跳天自动切新文件（不需重启）；一个进程一个文件，避免多进程争抢同一句柄。

格式：`2026-09-10 19:41:35 [INFO] app.api.levels: 关卡任务 level_fd33 已创建并入队…`。`LOG_LEVEL` 调级别（默认 INFO；`/healthz` 与 `/artifacts/**` 降到 DEBUG，不刷屏）；`LOG_KEEP_DAYS` 大于 0 才会清理过期日志，默认 0 = 永久保留。

排查一个关卡任务的典型路径：`grep level_<jobId> out/logs/*.log` —— 会依次看到入队、逐阶段进度（中文阶段名）、候选检测数量、语义复核来源与降级原因、几何门禁结果、可玩性告警、终态。
