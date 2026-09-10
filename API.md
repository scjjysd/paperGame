# 纸上游戏机 · 服务端接口文档（Unity 客户端用）

孩子在纸上画的火柴人 → 上传 → 服务端异步渲染 → 返回透明 RGBA 精灵表 + 切帧元数据 → Unity 直接切帧播放。

**契约唯一真源是 `app/contracts.py`**。本文档按该文件与实际代码路径编写，字段有出入时以代码为准。Unity 侧 `GameContracts.cs` 应照它逐字镜像（第 7 节给出 DTO）。

最后核对：2026-09-05，分支 `feat/2d-motion-synthesis`。

---

## 0. 快速上手

```
① POST /v1/characters        (multipart, 字段名 file)  →  202 {"jobId": "char_9c3ff81ce4ea"}
② GET  /v1/characters/{jobId}  每 1s 轮询             →  status 变为 ready（实测约 20s）
③ 按 animations.run / animations.jump 的 spriteSheetUrl 下载 PNG，按 frameWidth 切帧
```

调试时可直接在浏览器打开 `GET /v1/characters/{jobId}/view`，一页看到原图、动画、标注、精灵表。

---

## 1. 通用约定

| 项 | 值 |
|---|---|
| Base URL | `http://<host>:8000`（docker compose 的 `api` 容器） |
| 响应体 | 一律 JSON（`/view` 例外，返回 `text/html`） |
| 上传大小上限 | 10 MiB（`10 * 1024 * 1024`），超出即 400 |
| 接受的图片格式 | PNG、JPEG（按文件头前 4 字节嗅探，不看扩展名和 Content-Type） |
| 时间字段 | `updatedAt` 是 **UTC 且不带 `Z` 后缀**（如 `2026-09-05T15:19:12`），解析时务必按 UTC 处理；`renderedAt`（仅 `/detail`）带时区偏移 |
| 错误体 | `{"code": "<错误码>"}`，见第 6 节 |

**注意区分两种 404**：job 不存在返回 `{"code":"JOB_NOT_FOUND"}`；路径写错则是 FastAPI 默认的 `{"detail":"Not Found"}`。客户端请按 `code` 字段判断，不要只看状态码。

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
{"jobId": "char_9c3ff81ce4ea"}
```

**jobId 由文件内容决定**：`"char_" + sha256(文件字节).hexdigest()[:12]`。因此同一张图无论上传多少次都得到同一个 jobId，且已存在的任务不会重复入队 —— 客户端可以放心重传（弱网重试、用户重复点击）而不会产生重复渲染。已完成的任务重传后立即可查到终态。

**强制重跑 `?force=true`**：默认幂等短路下，同一张图不会重新渲染。加 `force=true` 可绕过短路：服务端丢弃该 jobId 的旧终态结果与磁盘产物（`result.json`、`run/jump.png`、`run/jump.gif`、`anno/`），状态重置为 `queued` 并重新入队，响应仍是 `202 {"jobId": ...}`（jobId 不变）。适用于「图没变但想重渲」「旧任务失败/needs_correction 想重试」的场景。注意：对正在 `processing` 的任务 force 会额外多跑一次，属预期行为。

错误：

| 状态码 | code | 触发条件 |
|---|---|---|
| 400 | `FILE_TOO_LARGE` | 超过 10 MiB |
| 400 | `UNSUPPORTED_FORMAT` | 文件头是 GIF 或 WEBP |
| 400 | `NOT_AN_IMAGE` | 文件头既不是 PNG/JPEG 也不是 GIF/WEBP |
| 503 | `QUEUE_UNAVAILABLE` | Redis 不可达 |

### 2.3 `GET /v1/characters/{jobId}` — 轮询状态（Unity 生产逻辑只依赖这个端点）

五种状态。**终态一旦写入不可被覆盖**（`queued`/`processing` 是非终态，其余三个是终态）。

**① 非终态** — `queued` 排队中，`processing` 正在渲染。无进度百分比。

```json
{"status": "processing", "updatedAt": "2026-09-05T15:19:12"}
```

**② `ready`** — 渲染成功，可以下载精灵表了。`animations` 恒含且仅含 `run` 与 `jump` 两个键（契约校验器强制）。

```json
{
  "status": "ready",
  "characterId": "char_9c3ff81ce4ea",
  "animations": {
    "run":  {"spriteSheetUrl": "/artifacts/char_9c3ff81ce4ea/run.png",
             "frameCount": 10, "fps": 15, "frameWidth": 241, "frameHeight": 275,
             "footAnchor": {"x": 120, "y": 275}},
    "jump": {"spriteSheetUrl": "/artifacts/char_9c3ff81ce4ea/jump.png",
             "frameCount": 7,  "fps": 15, "frameWidth": 241, "frameHeight": 275,
             "footAnchor": {"x": 120, "y": 275}}
  }
}
```

`AnimationMeta` 六个字段全部必填，含义见第 3 节。`spriteSheetUrl` 是站内绝对路径，需自行拼上 Base URL。

**③ `needs_correction`** — 认不出人形，或标注质量不过门禁，需要用户修图/改关节后重传。

```json
{
  "status": "needs_correction",
  "reason": "SKELETON_MISFIT",
  "maskUrl": "/artifacts/char_5a60409ca6d2/anno/mask.png",
  "joints": [{"name": "root", "loc": [289, 422], "parent": null}, "... 共 16 项"]
}
```

`reason` 六种取值：

| reason | 含义 |
|---|---|
| `NO_HUMANOID` | 图里找不到人形 |
| `NO_SKELETON` | 找到人形但推不出骨架 |
| `MULTIPLE_SKELETONS` | 检出多个人形，无法确定主角 |
| `NO_CONTOUR` | 提不出单连通轮廓 |
| `SKELETON_MISFIT` | 关节偏离墨迹过远，超出修复门禁 |
| `ANALYZE_FAILED` | 标注服务（TorchServe）本身失败 |

`joints` 长度是 **16 或 0**：早期失败（如 `NO_HUMANOID`）时标注文件尚未生成，返回空数组，此时客户端只能提示重拍，没有可编辑的关节。关节表见第 4 节。

⚠️ **`maskUrl` 是无条件写死的，早期失败时该文件并不存在**（服务端已知行为）。客户端若要展示 mask，请改用 `/detail` 端点的 `annotation.maskUrl` —— 它按磁盘实际存在与否给出 `null`。

**④ `failed`** — 基础设施故障，重传可能成功。

```json
{"status": "failed", "code": "RENDER_CRASHED"}
```

实际会出现的 `code`：`RENDER_TIMEOUT`（单次渲染超 120s）、`RENDER_CRASHED`（渲染子进程崩溃，或结果文件损坏；已自动重试 1 次仍失败）、`ASSET_MISSING`（渲染依赖的资产缺失）。

**⑤ 未知 jobId** — `404` + `{"code":"JOB_NOT_FOUND"}`。

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

目录本身不可列出（无索引页），只能按具体文件名取。

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
  "renderedAt": "2026-09-05T23:19:56+08:00",
  "viewUrl": "/v1/characters/char_9c3ff81ce4ea/view",
  "inputUrl": "/artifacts/char_9c3ff81ce4ea/input.png",
  "annotation": {
    "maskUrl": "/artifacts/char_9c3ff81ce4ea/anno/mask.png",
    "textureUrl": "/artifacts/char_9c3ff81ce4ea/anno/texture.png",
    "charCfgUrl": "/artifacts/char_9c3ff81ce4ea/anno/char_cfg.yaml",
    "width": 517,
    "height": 595,
    "joints": [{"name": "root", "loc": [289, 422], "parent": null}, "... 共 16 项"]
  },
  "animations": {
    "run": {
      "spriteSheetUrl": "/artifacts/char_9c3ff81ce4ea/run.png",
      "frameCount": 10, "fps": 15, "frameWidth": 241, "frameHeight": 275,
      "footAnchor": {"x": 120, "y": 275},
      "gifUrl": "/artifacts/char_9c3ff81ce4ea/run.gif"
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
| `renderedAt` | string \| null | 产物落盘时间，带时区偏移；尚无产物时为 `null` |
| `inputUrl` | string \| null | 上传的原图 |
| `annotation.width` / `.height` | int \| null | 标注画布尺寸，**`joints[].loc` 的坐标系就是它**；无标注时为 `null` |
| `annotation.joints` | array | 16 项或空数组，见第 4 节 |
| `animations` | object | 只在 `ready` 时非空；`{}` 表示无动画产物 |
| `animations.*.gifUrl` | string \| null | 未裁切的渲染原件。**历史任务为 `null`**（GIF 留存是后加的能力，早于该改动完成的任务没有此文件） |

未知 jobId 同样返回 `404` + `{"code":"JOB_NOT_FOUND"}`。

### 2.6 `GET /v1/characters/{jobId}/view` — 单页审查 HTML

返回 `text/html`，浏览器直接打开即可目视验收。内容与 2.5 同源，一页含：

- 原图；
- **run / jump 的精灵表实时播放** —— 用 CSS `steps()` 按 `frameCount`/`fps` 驱动，切帧参数与 Unity 完全同一套，所见即真机效果；
- 未裁切的 GIF 原件（有则显示，供对照裁切前后）；
- texture 上叠红色 mask + 青色关节点：**红区之外的黑笔画会在成片里彻底消失**，一眼看出哪些笔画丢了、关节吸附到了哪里；
- Unity 精灵表原图（点击看原尺寸）+ 帧数 / 帧尺寸 / fps / 脚底锚点。

产物缺失时显示占位块，不会产生坏图链接。未知 jobId 返回 `404` + `{"code":"JOB_NOT_FOUND"}`（JSON，不是 HTML）。

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

| code | 出现位置 | 含义 |
|---|---|---|
| `FILE_TOO_LARGE` | POST 400 | 超过 10 MiB |
| `NOT_AN_IMAGE` | POST 400 | 文件头不是已知图片格式 |
| `UNSUPPORTED_FORMAT` | POST 400 | 是 GIF / WEBP，当前只收 PNG / JPEG |
| `JOB_NOT_FOUND` | GET 404 | jobId 不存在，或磁盘快照损坏 |
| `QUEUE_UNAVAILABLE` | POST 503 | Redis 不可达 |
| `RENDER_TIMEOUT` | `failed.code` | 单次渲染超 120 s |
| `RENDER_CRASHED` | `failed.code` | 渲染子进程崩溃或结果损坏，重试后仍失败 |
| `ASSET_MISSING` | `failed.code` | 渲染依赖资产缺失 |
| `INTERNAL` | 契约保留 | 当前代码路径不产出，客户端仍应兜底处理 |

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

public class JobAccepted { public string jobId; }
public class ErrorBody   { public string code; }

// 五态共用一个类，按 status 分派：无关字段为 null，一次反序列化即可
public class CharacterStatus {
    public string status;
    public string updatedAt;                             // 非终态。UTC，无 Z 后缀
    public string characterId;                           // ready
    public Dictionary<string, AnimationMeta> animations;  // ready。含且仅含 "run" / "jump"
    public string reason;                                 // needs_correction
    public string maskUrl;                                // needs_correction。文件可能不存在
    public List<Joint> joints;                            // needs_correction。16 项或空
    public string code;                                   // failed
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
        Debug.LogError($"上传失败: {err?.code}");   // FILE_TOO_LARGE / NOT_AN_IMAGE / ...
        yield break;
    }
    string jobId = JsonConvert.DeserializeObject<JobAccepted>(post.downloadHandler.text).jobId;

    // ② 轮询到终态。实测约 20s；最坏留 5 分钟以上
    CharacterStatus st = null;
    for (float t = 0; t < 300f; t += 1f) {
        using var get = UnityWebRequest.Get($"{baseUrl}/v1/characters/{jobId}");
        yield return get.SendWebRequest();
        st = JsonConvert.DeserializeObject<CharacterStatus>(get.downloadHandler.text);
        if (st != null && GameContracts.IsTerminal(st.status)) break;
        yield return new WaitForSeconds(1f);
    }
    if (st == null || st.status != GameContracts.Ready) {
        Debug.LogWarning($"未成功: {st?.status} {st?.reason}{st?.code}");   // 引导重拍或重传
        yield break;
    }

    // ③ 下载精灵表并切帧。run 与 jump 的帧尺寸必然相同，可共用切帧参数
    foreach (var kv in st.animations) {
        var meta = kv.Value;
        using var tex = UnityWebRequestTexture.GetTexture(baseUrl + meta.spriteSheetUrl);
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

按影响排序，前两条会直接阻塞客户端接入：

1. **服务端未配置 CORS**。Unity WebGL 跑在浏览器里，若页面与 API 不同源，所有请求都会被浏览器拦下。两种解法：把 WebGL 构建部署到与 API 同源的路径下；或给 FastAPI 加 `CORSMiddleware` 并显式允许客户端来源。接入前需先确认部署形态。
2. **全部端点无鉴权**。`/artifacts/**`（含 `anno/` 全部中间产物）、`/detail`、`/view` 都是公开可读的，任何知道 jobId 的人都能取到图。当前只适用于内网与本地开发；公网部署前必须加访问控制。
3. **没有列表端点**。拿不到"所有角色"清单，只能按已知 jobId 查询。jobId 需客户端自行持久化。
4. **关节不可回传**。`needs_correction` 时服务端只给出关节位置供展示，没有 PATCH 端点；修正靠引导用户改图后重新上传。注意幂等按文件内容计算，图没变则 jobId 不变、不会重渲染。
5. **`gifUrl` 对历史任务为 `null`**。GIF 留存是后加的能力，在该改动之前完成的任务目录里没有这个文件。仅影响审查视图，不影响 Unity 取精灵表。
6. **不要下载 `anno/image.png`**。它是标注服务存的原图副本，实测可达 19 MB。需要原图请用 `inputUrl`（`input.png`）。
7. **关卡解析尚未实现**。本文档只覆盖角色链路；关卡（`/v1/levels`）与可玩性校验还没动工，接口未定。
