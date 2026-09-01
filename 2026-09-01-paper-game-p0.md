# 纸上游戏机 P0 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 孩子画一个类人形主角和一张只含横线地面、红色旗帜终点的纸上关卡；系统将主角生成跑、跳动画，并在 Unity 中让它跑到终点。

**架构：** Unity 客户端负责引导拍摄、展示处理进度、编辑与运行关卡；Python 服务负责图像校正、角色动画任务与关卡解析。服务输出固定的 `CharacterDefinition`、PNG 精灵表和 `LevelDefinition`；Unity 不读取原始照片，也不承担视觉识别。

**技术栈：** Unity 2022 LTS、C#、FastAPI、Python 3.8、OpenCV、Redis 队列、Docker/TorchServe、facebookresearch/AnimatedDrawings、对象存储。

---

## 已锁定的 P0 语法与验收线

- 主角必须是正面、完整、双臂双腿的类人形涂鸦；拍摄后给孩子一个“骨架点确认”页。
- 关卡纸固定为横版 A4，四角有定位标记。黑色横线且长度不小于 4 cm 为地面；红色旗帜为唯一终点；每张纸仅一个旗帜。
- 起点不是第三种绘制元素：角色固定从画面左下方、第一块可站立地面开始。
- 目标动作只有 `run`、`jump`；服务以透明 PNG 精灵表输出，Unity 按帧播放，不在运行时播放 GIF/MP4。
- 关卡只有一个成功条件：角色接触旗帜。若无法从起点到旗帜，禁止开始并给出可理解的纸面修改提示。
- 动画验证线：20 张样本中至少 16 张可以在 60 秒内产出可用跑、跳动画；否则演示版采用“引导画人 + 人工骨架确认”，并保留预置角色作为兜底。

## 目标文件结构

```text
client-unity/
  Assets/Scripts/Contracts/GameContracts.cs       # 与服务共享的数据模型
  Assets/Scripts/Network/PaperGameApi.cs          # REST、轮询、下载与超时
  Assets/Scripts/Character/CharacterCaptureFlow.cs# 主角拍摄、裁剪、确认、提交
  Assets/Scripts/Character/CharacterSpritePlayer.cs# 精灵表切帧、跑跳状态
  Assets/Scripts/Level/LevelCaptureFlow.cs        # 关卡拍摄、提交、错误定位
  Assets/Scripts/Level/LevelBuilder.cs            # LevelDefinition 转 Unity 碰撞体/旗帜
  Assets/Scripts/Play/PlayerController.cs         # 只含跑、跳、落地、到旗帜
  Assets/Scripts/Play/LevelValidator.cs           # 客户端启动前防御性校验
  Assets/Prefabs/Player.prefab
  Assets/Prefabs/Ground.prefab
  Assets/Prefabs/GoalFlag.prefab

server/
  app/main.py                                    # FastAPI 应用与路由注册
  app/contracts.py                               # Pydantic 请求、响应、错误码模型
  app/api/characters.py                          # 角色创建、任务状态、结果下载签名
  app/api/levels.py                              # 关卡解析接口
  app/services/character_pipeline.py             # AnimatedDrawings 调度与精灵表转换
  app/services/level_parser.py                   # 透视矫正、横线/旗帜解析
  app/services/playability.py                    # 跳跃图建模与 BFS 可达性
  app/workers/character_worker.py                # 异步角色任务执行器
  app/assets/motions/run.bvh
  app/assets/motions/jump.bvh
  tests/test_level_parser.py
  tests/test_playability.py
  tests/test_character_pipeline_contract.py
  docker-compose.yml
```

## 接口契约（前后端先行）

`POST /v1/characters` 接收主角照片，返回 `{ jobId }`；`GET /v1/characters/{jobId}` 返回 `queued | processing | needs_correction | ready | failed`。`ready` 的结果为：

```json
{
  "characterId": "char_123",
  "animations": {
    "run": { "spriteSheetUrl": "...", "frameCount": 12, "fps": 12 },
    "jump": { "spriteSheetUrl": "...", "frameCount": 10, "fps": 12 }
  }
}
```

`POST /v1/levels` 接收关卡照片，返回：

```json
{
  "status": "playable",
  "level": {
    "canvas": { "width": 1920, "height": 1080 },
    "platforms": [{ "x": 120, "y": 820, "width": 520, "height": 28 }],
    "goal": { "x": 1540, "y": 580 }
  },
  "warnings": []
}
```

不可玩时 `status` 为 `needs_fix`，`warnings` 使用稳定错误码：`NO_GROUND`、`NO_FLAG`、`MULTIPLE_FLAGS`、`GOAL_UNREACHABLE`、`PHOTO_INVALID`，并带归一化坐标区域供客户端高亮。

### 任务 1：建立契约、样本和验收基线

**文件：**
- 创建：`server/app/contracts.py`
- 创建：`client-unity/Assets/Scripts/Contracts/GameContracts.cs`
- 创建：`testdata/characters/`
- 创建：`testdata/levels/`

- [ ] **步骤 1：收集 20 张已获授权的儿童类人形涂鸦与 15 张关卡纸样本，按成功、异常、模糊、缺旗帜分类。**
- [ ] **步骤 2：为上述 JSON 接口编写 Pydantic 模型和 C# 可序列化镜像类型；禁止客户端使用匿名 JSON。**
- [ ] **步骤 3：用契约测试断言：`ready` 必含 run/jump 两个动画；`needs_fix` 必含至少一个错误码和区域。**
- [ ] **步骤 4：运行 `pytest tests/test_contracts.py -v`，预期全部通过。**
- [ ] **步骤 5：Commit：`git add server/app/contracts.py client-unity/Assets/Scripts/Contracts testdata && git commit -m "feat: define paper game contracts"`。**

### 任务 2：先完成 AnimatedDrawings 可行性尖刺

**文件：**
- 创建：`server/app/services/character_pipeline.py`
- 创建：`server/app/assets/motions/run.bvh`
- 创建：`server/app/assets/motions/jump.bvh`
- 创建：`server/tests/test_character_pipeline_contract.py`
- 创建：`docs/animation-spike-results.md`

- [ ] **步骤 1：为 `CharacterPipeline.render(input_path, motion)` 写失败测试：返回透明 PNG 精灵表、帧数和 fps；缺少人体或骨架异常返回 `needs_correction`，而不是 500。**
- [ ] **步骤 2：运行 `pytest server/tests/test_character_pipeline_contract.py -v`，预期失败，因为管线尚未实现。**
- [ ] **步骤 3：在 Docker 中安装并启动 AnimatedDrawings 所需的 TorchServe；分别以 `run.bvh`、`jump.bvh` 生成动画，将输出视频/GIF 拆成等尺寸、带 alpha 的 PNG 精灵表。**
- [ ] **步骤 4：实现骨架/蒙版结果的持久化；预测无效时保存可编辑标注并返回 `needs_correction`。**
- [ ] **步骤 5：运行 20 张样本，记录每张的耗时、是否成功、失败分类；只有满足 16/20 与 60 秒两项门槛才进入自动生成路线。**
- [ ] **步骤 6：运行 `pytest server/tests/test_character_pipeline_contract.py -v`，预期通过。**
- [ ] **步骤 7：Commit：`git add server/app/services/character_pipeline.py server/app/assets server/tests docs/animation-spike-results.md && git commit -m "feat: validate doodle animation pipeline"`。**

### 任务 3：服务端角色异步接口

**文件：**
- 创建：`server/app/api/characters.py`
- 创建：`server/app/workers/character_worker.py`
- 修改：`server/app/main.py`
- 测试：`server/tests/test_character_api.py`

- [ ] **步骤 1：编写 API 测试：上传有效 PNG 返回 202 与 `jobId`；轮询最终返回 `ready`；非图片、超过 10 MB、处理超时均返回明确错误。**
- [ ] **步骤 2：运行 `pytest server/tests/test_character_api.py -v`，预期失败。**
- [ ] **步骤 3：实现上传校验、对象存储路径、Redis 队列、幂等任务创建；worker 调用任务 2 的管线。**
- [ ] **步骤 4：为 `needs_correction` 返回蒙版和关节点编辑数据；为 `ready` 返回 run/jump 精灵表下载地址。**
- [ ] **步骤 5：运行 `pytest server/tests/test_character_api.py -v`，预期通过。**
- [ ] **步骤 6：Commit：`git add server/app/api/characters.py server/app/workers/character_worker.py server/app/main.py server/tests && git commit -m "feat: expose doodle animation jobs"`。**

### 任务 4：服务端关卡视觉解析

**文件：**
- 创建：`server/app/services/level_parser.py`
- 创建：`server/app/api/levels.py`
- 修改：`server/app/main.py`
- 测试：`server/tests/test_level_parser.py`

- [ ] **步骤 1：为三种样本写失败测试：有效横线+单红旗返回平台和旗帜；只有横线返回 `NO_FLAG`；倾斜拍摄仍在透视矫正后返回正确相对坐标。**
- [ ] **步骤 2：运行 `pytest server/tests/test_level_parser.py -v`，预期失败。**
- [ ] **步骤 3：实现四角定位标记检测和 A4 透视校正，统一映射到 1920×1080 画布。**
- [ ] **步骤 4：以灰度二值化、HoughLinesP 检出黑色近水平线，按间距合并为平台；以 HSV 红色阈值、三角形轮廓和竖杆空间关系识别旗帜。**
- [ ] **步骤 5：对零、多个或置信度不足的旗帜返回相应错误码与待修正区域。**
- [ ] **步骤 6：运行 `pytest server/tests/test_level_parser.py -v`，预期通过。**
- [ ] **步骤 7：Commit：`git add server/app/services/level_parser.py server/app/api/levels.py server/tests && git commit -m "feat: parse paper ground and goal"`。**

### 任务 5：服务端可玩性校验

**文件：**
- 创建：`server/app/services/playability.py`
- 修改：`server/app/api/levels.py`
- 测试：`server/tests/test_playability.py`

- [ ] **步骤 1：写失败测试：起始平台到旗帜路径可达为 playable；水平间隙超过 `maxJumpX` 为 `GOAL_UNREACHABLE`；上升高度超过 `maxJumpY` 为不可达。**
- [ ] **步骤 2：运行 `pytest server/tests/test_playability.py -v`，预期失败。**
- [ ] **步骤 3：以平台为节点、基于 Unity 已锁定 `maxJumpX/maxJumpY` 建有向边；从起始平台 BFS 到旗帜所依附的平台。**
- [ ] **步骤 4：不可达时返回最靠近目标的可达平台区域和“把两条线画近一点”提示。**
- [ ] **步骤 5：运行 `pytest server/tests/test_playability.py -v`，预期通过。**
- [ ] **步骤 6：Commit：`git add server/app/services/playability.py server/app/api/levels.py server/tests && git commit -m "feat: validate paper level reachability"`。**

### 任务 6：Unity 主角生成与素材缓存流程

**文件：**
- 创建：`client-unity/Assets/Scripts/Network/PaperGameApi.cs`
- 创建：`client-unity/Assets/Scripts/Character/CharacterCaptureFlow.cs`
- 创建：`client-unity/Assets/Scripts/Character/CharacterSpritePlayer.cs`
- 创建：`client-unity/Assets/Prefabs/Player.prefab`
- 测试：`client-unity/Assets/Tests/EditMode/CharacterSpritePlayerTests.cs`

- [ ] **步骤 1：编写 EditMode 测试：run 状态按服务 fps 循环帧；jump 状态播放结束回到 run；缺失精灵表时显示预置角色且不可进入正式演示。**
- [ ] **步骤 2：在 Unity Test Runner 运行 `CharacterSpritePlayerTests`，预期失败。**
- [ ] **步骤 3：实现相机/相册选择、裁剪框、上传、指数退避轮询、`needs_correction` 的重拍入口，以及本地精灵表缓存。**
- [ ] **步骤 4：将 PNG 精灵表切为 Sprite，绑定 Animator/状态机；跑动和跳跃不依赖网络。**
- [ ] **步骤 5：在 Unity Test Runner 运行 `CharacterSpritePlayerTests`，预期通过。**
- [ ] **步骤 6：Commit：`git add client-unity/Assets/Scripts/Network client-unity/Assets/Scripts/Character client-unity/Assets/Prefabs/Player.prefab client-unity/Assets/Tests && git commit -m "feat: add doodle character flow"`。**

### 任务 7：Unity 关卡拍摄、构建与游玩闭环

**文件：**
- 创建：`client-unity/Assets/Scripts/Level/LevelCaptureFlow.cs`
- 创建：`client-unity/Assets/Scripts/Level/LevelBuilder.cs`
- 创建：`client-unity/Assets/Scripts/Level/LevelValidator.cs`
- 创建：`client-unity/Assets/Scripts/Play/PlayerController.cs`
- 创建：`client-unity/Assets/Prefabs/Ground.prefab`
- 创建：`client-unity/Assets/Prefabs/GoalFlag.prefab`
- 测试：`client-unity/Assets/Tests/EditMode/LevelBuilderTests.cs`
- 测试：`client-unity/Assets/Tests/PlayMode/GoalCompletionTests.cs`

- [ ] **步骤 1：编写 EditMode 测试：一个平台定义生成一个含 BoxCollider2D 的地面；旗帜定义生成唯一触发器；非法坐标被拒绝。**
- [ ] **步骤 2：编写 PlayMode 测试：角色落在地面后可跑跳；碰到旗帜只触发一次完成页。**
- [ ] **步骤 3：在 Unity Test Runner 运行两组测试，预期失败。**
- [ ] **步骤 4：实现拍摄引导（定位标记需完整入镜）、上传解析、错误区域叠加与重新拍摄；只有服务端返回 playable 才允许“开始玩”。**
- [ ] **步骤 5：实现 `LevelDefinition` 到地面和旗帜预制体的映射，统一使用服务端 1920×1080 坐标。**
- [ ] **步骤 6：实现常量速度跑动、单次跳跃、落地检测、镜头跟随和旗帜结算；跳跃参数与服务端可玩性配置使用同一份 JSON。**
- [ ] **步骤 7：运行 Unity Test Runner 的 `LevelBuilderTests` 与 `GoalCompletionTests`，预期通过。**
- [ ] **步骤 8：Commit：`git add client-unity/Assets/Scripts/Level client-unity/Assets/Scripts/Play client-unity/Assets/Prefabs client-unity/Assets/Tests && git commit -m "feat: play parsed paper levels"`。**

### 任务 8：集成验收、性能和可演示兜底

**文件：**
- 创建：`docs/p0-acceptance-checklist.md`
- 修改：`docker-compose.yml`
- 测试：`server/tests/test_e2e_demo.py`

- [ ] **步骤 1：写端到端测试：上传主角、轮询 ready、上传有效关卡、收到 playable、下载精灵表与关卡 JSON。**
- [ ] **步骤 2：用真实手机拍摄的 15 张关卡纸验收：至少 13 张正确识别地面与旗帜；失败页面必须能告诉孩子缺什么或画在哪里。**
- [ ] **步骤 3：用 3 名儿童完成完整流程，记录从拍照到开始玩的时长、重拍原因、是否自行理解“横线/红旗”语法。**
- [ ] **步骤 4：若角色管线未达标，启用引导画人页面和预置角色演示开关；若关卡识别不稳，启用实时拍摄取景框与纸张定位标记校验，不增加第三个绘制元素。**
- [ ] **步骤 5：运行 `pytest server/tests/test_e2e_demo.py -v` 与 Unity 全量 Test Runner，预期全部通过。**
- [ ] **步骤 6：Commit：`git add docs docker-compose.yml server/tests && git commit -m "test: verify paper game p0 demo"`。**

## 10 个工作日并行排期

| 工作日 | 服务端 | Unity 前端 |
|---|---|---|
| D1 | 定契约、部署 AnimatedDrawings 尖刺 | 灰盒跑跳、地面/旗帜预制体、按 mock JSON 构建关卡 |
| D2 | 跑/跳动作导出与 20 样本验证 | 主角拍摄与上传 UI，mock 精灵表播放 |
| D3 | 角色异步任务 API | 角色处理、重拍与缓存流程 |
| D4 | A4 校正、横线与红旗解析 | 关卡拍摄、错误区域叠加 UI |
| D5 | 可玩性图与 BFS 校验 | 接入真实关卡 API、开始页限制 |
| D6 | 联调、异常码与性能日志 | 真实角色与关卡端到端联调 |
| D7 | 样本回归、识别参数收敛 | 跑跳手感、镜头、到旗帜结算 |
| D8 | 部署演示环境与可观测性 | 首次使用引导与离线素材兜底 |
| D9 | 3 组用户试用、修复 P0 阻断项 | 3 组用户试用、修复 P0 阻断项 |
| D10 | 回归、演示数据冻结 | 回归、录制路演 Demo |

## MVP 链路拆解：每一端的交付物与交接点

本节优先级高于上面的代码级任务。任何一端都不应等待另一端全部完成；每个阶段都必须交付可被另一端直接消费的文件、接口或可演示页面。

### 共同先锁定（D0，1 小时）

**共同决定：**

1. 纸张语法：横版 A4、四角定位标记、黑色横线是地面、红色旗帜是终点、每张仅一个旗帜。
2. 游戏物理常量：画布 `1920×1080`、角色脚底锚点、角色宽高、最大水平跳距 `maxJumpX`、最大上跳高度 `maxJumpY`。
3. 三份样本：一张合格主角、一张可玩关卡、一张不可玩关卡。
4. 契约版本 `v1`：角色结果与关卡结果 JSON 字段固定，改动需双方确认。

**共同交付：** `paper-template.pdf`、`contracts-v1.json`、三张样本图。没有这些，不进入开发。

### 服务端工作包

| 编号 | 服务端只负责什么 | 输入 | 必须交付的输出 | 客户端何时可以接手 |
|---|---|---|---|---|
| S1 | **角色动画尖刺**：把一张人形涂鸦变成两套动画资源 | `character.jpg` | `run.png`、`jump.png`、帧数、FPS、脚底 pivot；或明确失败原因 | 一拿到两张 PNG，就可脱离网络开发角色播放 |
| S2 | **角色异步服务**：上传、排队、轮询、资源地址 | 主角照片 | `POST /characters` 与 `GET /characters/{jobId}` | 客户端可将 S1 的本地 mock 换为真实服务 |
| S3 | **关卡解析尖刺**：照片变成地面坐标和旗帜坐标 | 纸张照片 | `LevelDefinition.json`，含 `platforms[]`、`goal`、置信度 | 一拿到 JSON，客户端就能生成完整关卡 |
| S4 | **可玩性校验**：判定角色能否跳到旗帜 | S3 的关卡 JSON + 物理常量 | `playable` 或 `GOAL_UNREACHABLE`，并给出需要修改的坐标区域 | 客户端可禁止进入坏关卡并展示提示 |
| S5 | **WebGL 可用的部署**：跨域、签名 URL、超时和日志 | 浏览器请求 | 演示环境 API 地址与 3 份验证结果 | 可在真实浏览器端到端联调 |

**服务端不负责：** Unity 碰撞、跑跳手感、镜头、UI、精灵表在 Unity 中的切分、孩子如何理解报错文案。

### 客户端（Unity WebGL）工作包

| 编号 | 客户端只负责什么 | 可先使用的假数据 | 必须交付的输出 | 依赖何时解除 |
|---|---|---|---|---|
| C1 | **游戏灰盒**：角色能跑、跳、落地，碰旗帜结束 | 手工制作的 `run.png`、`jump.png` 与 `LevelDefinition.json` | 浏览器中可操作的“跑到旗帜”场景 | 不依赖服务端，D1 必须完成 |
| C2 | **资源播放器**：下载 PNG、按 `frameCount/fps` 切帧、跑跳状态机 | S1 的本地文件 | 输入任何符合契约的角色资源都能播放 | 等 S1 输出后切真资源 |
| C3 | **关卡构建器**：JSON 转 BoxCollider2D 地面和旗帜触发器 | 手工 JSON | 输入任何符合契约的关卡 JSON 都能运行 | 等 S3 输出后切真关卡 |
| C4 | **浏览器桥接**：手机选图/拍照、上传、轮询、显示处理中 | mock API | WebGL Template + `.jslib`，可将图片字节交给 Unity | 等 S2、S5 后接真 API |
| C5 | **修正体验**：把服务端错误码转成儿童能懂的提示与重拍入口 | `NO_FLAG` 等 mock 响应 | 纸面预览高亮 + “请画一个红旗”这类提示 | 等 S4 后接真校验 |
| C6 | **演示体验**：首次引导、加载、胜利页、预置角色兜底 | 全部真实资源 | 一条 3 分钟内完成的可录屏流程 | 依赖 S1-S5 完成 |

**客户端不负责：** 人物抠图、骨架推理、关卡视觉识别、判定纸张上哪一笔是横线或旗帜。

### 三段 MVP 验证链路（按顺序验收）

#### MVP-1：游戏是否成立（只做客户端，D1）

```text
手工精灵表 + 手工关卡 JSON
→ Unity WebGL 跑跳
→ 碰旗帜胜利
```

**通过标准：** PC 浏览器打开后 10 秒内进入游戏；角色能跨过一段间隙、落在地面、碰旗帜出现胜利页。此阶段不拍照、不联网、不调用 AI。

#### MVP-2：两个“纸变数据”的技术风险是否可解（服务端尖刺，D1-D2）

```text
主角照片 → run/jump PNG 精灵表
关卡照片 → LevelDefinition JSON
```

**通过标准：**

- 20 张合规人形涂鸦中至少 16 张产出可用 run/jump，单张不超过 60 秒；
- 15 张合规关卡纸中至少 13 张识别到正确的横线数量范围和唯一旗帜；
- 结果可用本地文件交给 C2/C3，无须先搭完整 API。

**失败时的唯一降级：** 主角使用预置角色、关卡使用一张固定示例纸；保留上传 UI，不扩展玩法范围来掩盖风险。

#### MVP-3：真实一条龙（联合联调，D3-D5）

```text
手机拍主角 → 服务端生成动画 → WebGL 播放
→ 手机拍关卡 → 服务端解析/校验 → Unity 构建
→ 跑到旗帜
```

**通过标准：** 一名未参与开发的成年人可按引导完成一轮；全过程不超过 3 分钟；失败可明确归因到“重拍主角”“补红旗”或“把两条地面画近”。

### 每日 15 分钟联调站会只回答四个问题

1. 今天服务端交给客户端的具体 URL、JSON 或资源文件是什么？
2. 客户端今天用的是真实数据还是 mock 数据？
3. 哪个验收数字尚未达到：角色成功率、关卡识别率、全链路时长？
4. 若今天失败，启用的既定降级是什么？

## 不在 P0 范围

- 迷宫、怪物、金币、自由物体语义识别、关卡编辑器、账号体系、作品分享、多人玩法。
- 非人形角色的自动骨架推断；这类素材只可作为后续研究或人工标注路线。
