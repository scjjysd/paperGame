# Level API v1：手绘关卡图片转 JSON 协议

> 状态：Draft
>
> 协议版本：`1.0`
>
> 适用范围：PaperGame Unity WebGL 客户端与关卡解析服务
>
> 配套交接文档：[level-image-to-json-server-handoff.md](./level-image-to-json-server-handoff.md)

本文是独立于角色图片处理协议的新协议，不修改或替代现有 `API.md` 中的 `/v1/characters` 契约。

## 1. 协议目标

本协议定义如何将用户上传的手绘关卡照片异步转换为：

- 拉正后的背景图片；
- 与背景严格对齐的平台碰撞线；
- 玩家出生点；
- 终点区域；
- 识别与可玩性分析结果。

关卡 JSON 表达的是图片中已经存在的几何，不负责自动优化关卡。

## 2. 基础约定

### 2.1 服务地址

示例：

```text
https://api.example.com
```

所有生产请求必须使用 HTTPS。本文中的相对 URL 均相对于服务地址。

### 2.2 内容类型

- 上传：`multipart/form-data`
- 查询和错误响应：`application/json; charset=utf-8`
- 图片产物：`image/png` 或原始上传对应的安全图片格式

### 2.3 时间格式

时间使用 UTC ISO 8601：

```text
2026-09-07T08:30:15Z
```

### 2.4 坐标系

Level API v1 只允许以下坐标系：

| 属性 | 固定值 |
|---|---|
| 空间基准 | 拉正后的背景图片 |
| 原点 | 左上角 |
| X 轴 | 向右 |
| Y 轴 | 向下 |
| 单位 | 像素 |
| 数值 | v1 输出整数 |
| 边界 | `0 <= x < width`，`0 <= y < height` |

禁止把屏幕坐标、CSS 像素、Unity 世界坐标或宽度为 32 的逻辑坐标写入 v1 JSON。

### 2.5 文件限制

v1 必须支持：

- JPEG；
- PNG；
- 最大文件大小 10 MiB；
- 单边尺寸 800～12,000 px；
- 解码后最大 40,000,000 像素。

服务器必须按文件魔数判断格式。HEIC/HEIF 不属于 v1 必选能力；客户端应在上传前转换为 JPEG 或 PNG。

## 3. 状态机

```text
queued ──► processing ──► ready
                    ├──► needs_fix
                    ├──► needs_review
                    └──► failed
```

终态定义：

| 状态 | 含义 | 可直接开始游戏 |
|---|---|---:|
| `ready` | 识别可靠，且按提交的能力参数可达 | 是 |
| `needs_fix` | 识别可靠，但草图不可玩 | 否 |
| `needs_review` | 识别存在歧义，不能发布权威结果 | 否 |
| `failed` | 系统、依赖或不可恢复的处理错误 | 否 |

## 4. 创建解析任务

### `POST /v1/levels`

上传照片并创建异步解析任务。

### 4.1 表单字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `file` | binary | 是 | 无 | JPEG 或 PNG 图片 |
| `force` | boolean | 否 | `false` | 忽略既有完成结果并重新运行 |
| `schemaVersion` | string | 否 | `1.0` | 客户端期望的关卡协议版本 |
| `playabilityProfile` | JSON string | 否 | 服务默认值 | 仅用于可玩性分析，不得影响几何识别 |

`playabilityProfile` 示例：

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

字段含义：

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `profileVersion` | string | 1～64 字符 | 能力配置版本，便于复现分析 |
| `maxJumpRisePixels` | integer | `> 0` | 角色脚底可达到的最大垂直上升高度 |
| `maxJumpDistancePixels` | integer | `> 0` | 一次跳跃允许的最大水平位移 |
| `characterWidthPixels` | integer | `> 0` | 角色碰撞体宽度 |
| `characterHeightPixels` | integer | `> 0` | 角色碰撞体高度 |
| `landingTolerancePixels` | integer | `>= 0` | 着陆边界允许的误差 |

### 4.2 请求示例

```bash
curl -X POST "https://api.example.com/v1/levels" \
  -F "file=@paper-level.jpg" \
  -F "schemaVersion=1.0" \
  -F 'playabilityProfile={"profileVersion":"unity-c1-test-1","maxJumpRisePixels":150,"maxJumpDistancePixels":230,"characterWidthPixels":32,"characterHeightPixels":58,"landingTolerancePixels":6}'
```

### 4.3 成功响应

HTTP `202 Accepted`

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "queued",
  "statusUrl": "/v1/levels/level_4e91a63bcf21",
  "createdAt": "2026-09-07T08:30:15Z"
}
```

对于相同文件、相同解析参数和相同算法主版本，服务端可以返回已有任务。此时仍返回 `202`，查询接口会给出任务当前或最终状态。

## 5. 查询任务

### `GET /v1/levels/{jobId}`

客户端建议每 1 秒轮询一次。连续 30 秒未完成后可采用 2～5 秒退避间隔。客户端应允许用户取消本地等待，但取消等待不等于删除服务端任务。

### 5.1 排队响应

HTTP `200 OK`

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "queued",
  "progress": {
    "stage": "waiting",
    "percent": 0
  },
  "createdAt": "2026-09-07T08:30:15Z",
  "updatedAt": "2026-09-07T08:30:15Z"
}
```

### 5.2 处理中响应

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "processing",
  "progress": {
    "stage": "detecting_platforms",
    "percent": 55
  },
  "createdAt": "2026-09-07T08:30:15Z",
  "updatedAt": "2026-09-07T08:30:18Z"
}
```

`progress.stage` 为展示与排查信息，客户端不得依赖完整枚举。当前推荐值包括：

- `waiting`
- `validating_upload`
- `rectifying_paper`
- `detecting_platforms`
- `detecting_goal`
- `semantic_review`
- `validating_geometry`
- `analyzing_playability`
- `publishing_artifacts`

## 6. `ready` 响应

HTTP `200 OK`

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "ready",
  "schemaVersion": "1.0",
  "algorithmVersion": "level-parser-1.0.0",
  "createdAt": "2026-09-07T08:30:15Z",
  "updatedAt": "2026-09-07T08:30:22Z",
  "result": {
    "level": {
      "schemaVersion": "1.0",
      "coordinateSystem": {
        "origin": "top_left",
        "xAxis": "right",
        "yAxis": "down",
        "unit": "pixel"
      },
      "canvas": {
        "width": 1245,
        "height": 810
      },
      "background": {
        "imageUrl": "/artifacts/level_4e91a63bcf21/rectified.png",
        "contentType": "image/png",
        "width": 1245,
        "height": 810,
        "sha256": "8f1c2f6d0e18d5a95e2b9846a7d375af4d62f34cbb06bf30df117d1d47694f75"
      },
      "playerStart": {
        "x": 110,
        "y": 681,
        "source": "inferred",
        "confidence": 0.94
      },
      "platforms": [
        {
          "id": "platform_001",
          "start": { "x": 83, "y": 681 },
          "end": { "x": 298, "y": 681 },
          "confidence": 0.98
        },
        {
          "id": "platform_002",
          "start": { "x": 315, "y": 682 },
          "end": { "x": 472, "y": 682 },
          "confidence": 0.97
        }
      ],
      "goalRegion": {
        "x": 1100,
        "y": 142,
        "width": 96,
        "height": 112,
        "confidence": 0.93
      }
    },
    "analysis": {
      "playability": "playable",
      "profile": {
        "profileVersion": "unity-c1-test-1",
        "maxJumpRisePixels": 150,
        "maxJumpDistancePixels": 230,
        "characterWidthPixels": 32,
        "characterHeightPixels": 58,
        "landingTolerancePixels": 6
      },
      "startPlatformId": "platform_001",
      "goalPlatformId": "platform_007",
      "path": [
        "platform_001",
        "platform_002",
        "platform_004",
        "platform_007"
      ],
      "warnings": []
    },
    "artifacts": {
      "inputUrl": "/artifacts/level_4e91a63bcf21/input.jpg",
      "rectifiedImageUrl": "/artifacts/level_4e91a63bcf21/rectified.png",
      "overlayImageUrl": "/artifacts/level_4e91a63bcf21/overlay.png",
      "levelJsonUrl": "/artifacts/level_4e91a63bcf21/level.json",
      "analysisJsonUrl": "/artifacts/level_4e91a63bcf21/analysis.json"
    }
  }
}
```

## 7. 权威 `level` 对象规范

### 7.1 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `schemaVersion` | string | 是 | 固定为 `1.0` |
| `coordinateSystem` | object | 是 | 必须等于 v1 固定坐标系 |
| `canvas` | object | 是 | 拉正背景的真实像素尺寸 |
| `background` | object | 是 | 拉正背景及完整性信息 |
| `playerStart` | object | 是 | 角色脚底参考点 |
| `platforms` | array | 是 | 至少一条不可见碰撞线 |
| `goalRegion` | object | 是 | 终点触发区域 |

### 7.2 平台

平台由两个端点组成：

```json
{
  "id": "platform_001",
  "start": { "x": 83, "y": 681 },
  "end": { "x": 298, "y": 681 },
  "confidence": 0.98
}
```

约束：

- `id` 在当前关卡内唯一且稳定；
- `start.x <= end.x`；
- 两端点都必须位于画布内；
- 平台长度不得为零；
- 允许 `start.y != end.y`，以忠实保留轻微倾斜；
- `confidence` 范围为 `0.0～1.0`；
- JSON 不包含可见线宽；
- Unity 生成的碰撞体不可见，其物理厚度属于客户端运行参数，不能用于改变平台中心线。

### 7.3 玩家出生点

`playerStart` 表示角色脚底中心在图片中的像素位置。`source` 当前允许：

- `detected`：草图中存在明确出生标记；
- `inferred`：服务端根据起始平台推断；
- `reviewed`：人工复核后确定。

### 7.4 终点区域

`goalRegion` 为左上角加宽高形式的轴对齐矩形：

```json
{
  "x": 1100,
  "y": 142,
  "width": 96,
  "height": 112,
  "confidence": 0.93
}
```

必须满足：

- `width > 0`；
- `height > 0`；
- 整个矩形位于画布范围内；
- 区域覆盖图片中真实终点旗帜；
- 不允许为了更容易触发而扩展到无关平台。

### 7.5 背景

`background.width` 和 `background.height` 必须同时等于 `canvas` 尺寸以及下载图片的真实尺寸。任一不一致都属于协议错误，客户端不得尝试自行补偿。

`imageUrl` 可以是相对 URL 或绝对 HTTPS URL。相对 URL 按 API 服务地址解析。生产环境可返回有时效的签名 URL，此时响应应额外提供 `expiresAt`。

## 8. `needs_fix` 响应

当几何识别可靠，但按提交的角色能力参数无法从出生点到达终点时返回。

HTTP `200 OK`

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "needs_fix",
  "schemaVersion": "1.0",
  "algorithmVersion": "level-parser-1.0.0",
  "result": {
    "level": {
      "schemaVersion": "1.0",
      "coordinateSystem": {
        "origin": "top_left",
        "xAxis": "right",
        "yAxis": "down",
        "unit": "pixel"
      },
      "canvas": { "width": 1245, "height": 810 },
      "background": {
        "imageUrl": "/artifacts/level_4e91a63bcf21/rectified.png",
        "contentType": "image/png",
        "width": 1245,
        "height": 810,
        "sha256": "8f1c2f6d0e18d5a95e2b9846a7d375af4d62f34cbb06bf30df117d1d47694f75"
      },
      "playerStart": {
        "x": 110,
        "y": 681,
        "source": "inferred",
        "confidence": 0.94
      },
      "platforms": [
        {
          "id": "platform_001",
          "start": { "x": 83, "y": 681 },
          "end": { "x": 298, "y": 681 },
          "confidence": 0.98
        },
        {
          "id": "platform_002",
          "start": { "x": 520, "y": 480 },
          "end": { "x": 690, "y": 480 },
          "confidence": 0.96
        }
      ],
      "goalRegion": {
        "x": 1100,
        "y": 142,
        "width": 96,
        "height": 112,
        "confidence": 0.93
      }
    },
    "analysis": {
      "playability": "unreachable",
      "profile": {
        "profileVersion": "unity-c1-test-1",
        "maxJumpRisePixels": 150,
        "maxJumpDistancePixels": 230,
        "characterWidthPixels": 32,
        "characterHeightPixels": 58,
        "landingTolerancePixels": 6
      },
      "path": [],
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
    "artifacts": {
      "rectifiedImageUrl": "/artifacts/level_4e91a63bcf21/rectified.png",
      "overlayImageUrl": "/artifacts/level_4e91a63bcf21/overlay.png",
      "levelJsonUrl": "/artifacts/level_4e91a63bcf21/level.json",
      "analysisJsonUrl": "/artifacts/level_4e91a63bcf21/analysis.json"
    }
  }
}
```

真实响应必须返回完整、忠实的识别平台，不得为了缩短跳跃距离而修改示例中的几何。

## 9. `needs_review` 响应

当纸张、平台、旗帜或方向存在识别歧义时返回。非权威候选只能放入 `review`，不得放入 `result.level`。

HTTP `200 OK`

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "needs_review",
  "schemaVersion": "1.0",
  "algorithmVersion": "level-parser-1.0.0",
  "review": {
    "reason": "AMBIGUOUS_GOAL",
    "message": "检测到两个置信度接近的终点旗帜候选，请重拍或人工确认。",
    "candidates": [
      {
        "id": "goal_candidate_01",
        "type": "goal",
        "region": { "x": 1080, "y": 130, "width": 88, "height": 120 },
        "confidence": 0.62
      },
      {
        "id": "goal_candidate_02",
        "type": "goal",
        "region": { "x": 740, "y": 190, "width": 73, "height": 105 },
        "confidence": 0.59
      }
    ],
    "suggestions": [
      "保证纸张边缘完整出现在画面中。",
      "只保留一个终点旗帜，并避免阴影遮挡。"
    ]
  },
  "artifacts": {
    "rectifiedImageUrl": "/artifacts/level_4e91a63bcf21/rectified.png",
    "overlayImageUrl": "/artifacts/level_4e91a63bcf21/overlay.png"
  }
}
```

推荐的 `review.reason`：

- `PAPER_NOT_FOUND`
- `PAPER_AMBIGUOUS`
- `PAPER_OCCLUDED`
- `ORIENTATION_AMBIGUOUS`
- `NO_PLATFORM_DETECTED`
- `PLATFORM_GEOMETRY_AMBIGUOUS`
- `GOAL_NOT_FOUND`
- `AMBIGUOUS_GOAL`
- `START_PLATFORM_NOT_FOUND`
- `LOW_CONFIDENCE`

## 10. `failed` 响应

`failed` 用于技术失败，不用于表达“用户草图不可玩”。

HTTP `200 OK`

```json
{
  "jobId": "level_4e91a63bcf21",
  "status": "failed",
  "error": {
    "code": "PROCESSING_DEPENDENCY_UNAVAILABLE",
    "message": "图像处理依赖暂时不可用。",
    "retryable": true,
    "requestId": "req_01J7BE8WZC6Y2K5Q74H86PRB01"
  },
  "createdAt": "2026-09-07T08:30:15Z",
  "updatedAt": "2026-09-07T08:30:22Z"
}
```

## 11. HTTP 错误

无法创建或查询任务时使用非 2xx HTTP 状态码。

统一格式：

```json
{
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "图片不能超过 10 MiB。",
    "retryable": false,
    "requestId": "req_01J7BE8WZC6Y2K5Q74H86PRB01",
    "details": {
      "limitBytes": 10485760
    }
  }
}
```

| HTTP | `code` | 说明 |
|---:|---|---|
| 400 | `INVALID_REQUEST` | 表单字段或 JSON 无效 |
| 400 | `INVALID_PLAYABILITY_PROFILE` | 角色能力参数缺失或越界 |
| 400 | `UNSUPPORTED_SCHEMA_VERSION` | 服务端不支持请求版本 |
| 401 | `UNAUTHORIZED` | 未认证 |
| 403 | `FORBIDDEN` | 无权访问该任务 |
| 404 | `JOB_NOT_FOUND` | 任务不存在或已过期 |
| 413 | `FILE_TOO_LARGE` | 文件超过限制 |
| 415 | `UNSUPPORTED_IMAGE_FORMAT` | 不是受支持的 JPEG 或 PNG |
| 422 | `IMAGE_DECODE_FAILED` | 图片无法安全解码 |
| 429 | `RATE_LIMITED` | 请求过于频繁 |
| 503 | `QUEUE_UNAVAILABLE` | 任务队列暂不可用 |
| 500 | `INTERNAL_ERROR` | 未分类服务错误 |

HTTP `429` 和可重试的 `503` 应返回 `Retry-After`。

## 12. 调试接口与产物

### `GET /v1/levels/{jobId}/detail`

返回服务端排查信息。生产环境必须鉴权，不保证面向普通客户端长期兼容。

建议字段：

- 各处理阶段耗时；
- OpenCV 参数快照；
- 透视矩阵；
- 候选数量与过滤原因；
- LLM 模型、提示词版本和结构化分类结果；
- 置信度明细；
- Worker 尝试次数；
- 产物校验和。

### `GET /v1/levels/{jobId}/view`

返回供内部人员查看的调试页面，至少能切换：

- 原图；
- 拉正图；
- 墨迹遮罩；
- 检测叠加图；
- 平台编号；
- 出生点和终点区域；
- 可玩路径和失败跳跃。

### `/artifacts/{jobId}/{name}`

产物下载接口。生产环境建议使用鉴权下载或短期签名 URL。

## 13. 幂等规则

幂等键建议由以下内容共同计算：

```text
SHA-256(
  original_file_bytes
  + canonical_request_options
  + algorithm_major_version
)
```

默认 `jobId` 格式：

```text
level_<hash-prefix>
```

要求：

- 参数 JSON 必须先按固定键序和数值格式规范化；
- `playabilityProfile` 参与哈希，因为它影响最终状态和分析；
- `force=true` 创建新的运行实例，但不能丢失输入哈希；
- 同一任务发布结果时必须采用原子写入；
- 历史结果不得因算法升级被静默覆盖。

## 14. 兼容性与版本策略

### 14.1 `schemaVersion`

- `1.x` 的新增字段必须是可选字段；
- 删除字段、改变坐标语义或改变必填性需要升级主版本；
- 客户端遇到不支持的主版本必须停止加载并提示升级；
- 客户端应忽略同一主版本中的未知可选字段。

### 14.2 `algorithmVersion`

算法版本不影响协议解析，但用于复现结果和回归。任何会明显改变纸张拉正、平台端点或状态判定的更新都必须升级算法版本。

## 15. Unity 客户端加载规范

Unity 客户端必须按以下顺序加载：

1. 校验 `schemaVersion`；
2. 下载背景图片并校验解码尺寸；
3. 验证背景尺寸与 `canvas` 完全一致；
4. 可选校验 SHA-256；
5. 创建背景 Sprite；
6. 使用统一的像素到世界坐标函数转换所有几何；
7. 创建不可见平台碰撞体；
8. 放置角色和终点触发器；
9. 完成后才允许进入游戏。

若任何一步失败，不得显示“可玩关卡”。客户端不能通过单独缩放 JSON 坐标来弥补图片尺寸错误。

像素点 `(x, y)` 转为以背景中心为原点的 Unity 局部坐标时，推荐使用：

```text
localX = (x - canvasWidth / 2) / pixelsPerUnit
localY = (canvasHeight / 2 - y) / pixelsPerUnit
```

背景和所有碰撞体必须位于同一父节点下并共享缩放。屏幕适配只缩放这一父节点，不能分别适配背景与碰撞体。

## 16. OpenCV 和 LLM 输出约束

### 16.1 OpenCV 中间结果

OpenCV 应输出未发布的内部结构，至少包含：

```json
{
  "canvas": { "width": 1245, "height": 810 },
  "platformCandidates": [
    {
      "candidateId": "line_001",
      "start": { "x": 83, "y": 681 },
      "end": { "x": 298, "y": 681 },
      "angleDegrees": 0.0,
      "inkCoverage": 0.96,
      "geometryConfidence": 0.98
    }
  ],
  "goalCandidates": [],
  "transformArtifact": "transform.json"
}
```

### 16.2 LLM 输入原则

- 输入拉正图，不输入尚未校正透视的原图作为坐标依据；
- 在图片上绘制候选编号；
- 让模型在既有候选 ID 中选择，不允许自由输出最终端点；
- 使用严格 JSON Schema；
- `temperature` 使用低值；
- 保存模型名称、提示词版本和原始结构化响应用于审计。

### 16.3 LLM 推荐提示词模板

```text
你是手绘横版平台关卡的候选分类器，不是关卡设计师。

输入是一张已经完成透视拉正的纸张图片。图片上标出了 OpenCV 产生的候选编号。
你的任务仅限于：
1. 判断每个 line candidate 是平台墨迹、纸张边缘、阴影、文字或其他内容；
2. 判断每个 goal candidate 是否为终点旗帜；
3. 指出是否存在多张纸、严重遮挡、方向不明确或无法判断的情况。

禁止行为：
- 不得创建候选列表中不存在的平台；
- 不得输出、移动、延长或修正任何平台坐标；
- 不得为了让关卡可玩而修改判断；
- 不确定时必须返回 needs_review，不得猜测。

只返回符合提供的 JSON Schema 的 JSON，不要输出解释性文字。
```

推荐的结构化返回：

```json
{
  "lineClassifications": [
    {
      "candidateId": "line_001",
      "label": "platform",
      "confidence": 0.97
    }
  ],
  "goalClassifications": [
    {
      "candidateId": "goal_001",
      "label": "goal_flag",
      "confidence": 0.93
    }
  ],
  "sceneIssues": [],
  "decision": "accepted"
}
```

`decision` 只允许：

- `accepted`
- `needs_review`

服务端必须再次按 Schema 校验 LLM 响应。校验失败时不得尝试从自然语言中正则提取坐标。

## 17. 可玩性警告码

| `code` | 说明 |
|---|---|
| `START_NOT_SUPPORTED` | 出生点没有可靠承载平台 |
| `GOAL_NOT_SUPPORTED` | 终点不在可到达平台附近 |
| `JUMP_GAP_TOO_HIGH` | 垂直高度超过能力 |
| `JUMP_GAP_TOO_WIDE` | 水平间距超过能力 |
| `LANDING_AREA_TOO_SHORT` | 平台可着陆长度不足 |
| `NO_PATH_TO_GOAL` | 可达图中不存在完整路径 |

警告应尽量附带相关平台 ID、所需值和当前能力值，便于客户端告诉用户应该修改草图的哪一部分。

## 18. 契约测试要求

服务端和 Unity 至少共享以下固定样例：

- `ready.json`
- `needs-fix.json`
- `needs-review.json`
- `failed.json`
- `invalid-out-of-bounds.json`
- `invalid-background-size.json`

服务端测试：

- 响应符合 JSON Schema；
- 坐标不越界；
- 图片真实尺寸等于 JSON 画布；
- 相同输入产生稳定结果；
- 低置信度不会进入 `ready`；
- 不可玩不会进入 `failed`。

Unity 测试：

- 能解析所有终态；
- 拒绝不支持的协议主版本；
- 拒绝图片与画布尺寸不一致；
- Y 轴只翻转一次；
- 背景缩放后碰撞体仍与墨迹重合；
- `needs_fix` 和 `needs_review` 不进入正式游戏。

## 19. v1 明确不包含的能力

- 自动修改或优化用户草图；
- 自动补平台、移动平台或压缩关卡；
- 将照片转换为重新绘制的美术背景；
- 多页纸拼接；
- 曲线、移动平台、斜坡物理和复杂多边形碰撞；
- 用户在线编辑候选坐标并提交复核；
- 分享链接、关卡社区和长期发布；
- 独立的可玩性重新分析接口。

这些能力需要单独版本和产品设计，不应通过扩展 v1 字段的含义偷偷加入。

## 附录 A：权威关卡对象 JSON Schema

以下 Schema 约束 `result.level` 和 `level.json`。API 响应信封的状态分支由接口契约测试分别校验。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://api.example.com/schemas/level-1.0.schema.json",
  "title": "PaperGame Level 1.0",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schemaVersion",
    "coordinateSystem",
    "canvas",
    "background",
    "playerStart",
    "platforms",
    "goalRegion"
  ],
  "properties": {
    "schemaVersion": {
      "const": "1.0"
    },
    "coordinateSystem": {
      "type": "object",
      "additionalProperties": false,
      "required": ["origin", "xAxis", "yAxis", "unit"],
      "properties": {
        "origin": { "const": "top_left" },
        "xAxis": { "const": "right" },
        "yAxis": { "const": "down" },
        "unit": { "const": "pixel" }
      }
    },
    "canvas": {
      "$ref": "#/$defs/canvas"
    },
    "background": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "imageUrl",
        "contentType",
        "width",
        "height",
        "sha256"
      ],
      "properties": {
        "imageUrl": { "type": "string", "minLength": 1 },
        "contentType": {
          "enum": ["image/png", "image/jpeg"]
        },
        "width": { "type": "integer", "minimum": 1 },
        "height": { "type": "integer", "minimum": 1 },
        "sha256": {
          "type": "string",
          "pattern": "^[a-f0-9]{64}$"
        },
        "expiresAt": {
          "type": "string",
          "format": "date-time"
        }
      }
    },
    "playerStart": {
      "type": "object",
      "additionalProperties": false,
      "required": ["x", "y", "source", "confidence"],
      "properties": {
        "x": { "type": "integer", "minimum": 0 },
        "y": { "type": "integer", "minimum": 0 },
        "source": {
          "enum": ["detected", "inferred", "reviewed"]
        },
        "confidence": { "$ref": "#/$defs/confidence" }
      }
    },
    "platforms": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "start", "end", "confidence"],
        "properties": {
          "id": {
            "type": "string",
            "pattern": "^platform_[A-Za-z0-9_-]+$"
          },
          "start": { "$ref": "#/$defs/point" },
          "end": { "$ref": "#/$defs/point" },
          "confidence": { "$ref": "#/$defs/confidence" }
        }
      }
    },
    "goalRegion": {
      "allOf": [
        { "$ref": "#/$defs/region" },
        {
          "type": "object",
          "required": ["confidence"],
          "properties": {
            "confidence": { "$ref": "#/$defs/confidence" }
          }
        }
      ]
    }
  },
  "$defs": {
    "canvas": {
      "type": "object",
      "additionalProperties": false,
      "required": ["width", "height"],
      "properties": {
        "width": { "type": "integer", "minimum": 1 },
        "height": { "type": "integer", "minimum": 1 }
      }
    },
    "point": {
      "type": "object",
      "additionalProperties": false,
      "required": ["x", "y"],
      "properties": {
        "x": { "type": "integer", "minimum": 0 },
        "y": { "type": "integer", "minimum": 0 }
      }
    },
    "region": {
      "type": "object",
      "required": ["x", "y", "width", "height"],
      "properties": {
        "x": { "type": "integer", "minimum": 0 },
        "y": { "type": "integer", "minimum": 0 },
        "width": { "type": "integer", "minimum": 1 },
        "height": { "type": "integer", "minimum": 1 }
      }
    },
    "confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1
    }
  }
}
```

JSON Schema 无法独立表达以下跨字段约束，服务端与客户端都必须额外校验：

- 所有点和矩形完整位于 `canvas` 内；
- `background.width == canvas.width`；
- `background.height == canvas.height`；
- 每个平台满足 `start.x <= end.x` 且两个端点不相同；
- 平台 `id` 在数组内唯一；
- 下载图片的真实像素尺寸与 `background` 声明一致。
