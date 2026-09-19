# 角色管线诊断与超时加固设计

日期：2026-09-19

## 1. 背景与目标

`char_a0bb4255e1d2` 的一次真实任务总耗时 43.779 秒，其中 `repair` 为 12.445 秒，
`render_animations` 为 28.758 秒。现有日志只能定位到一级阶段，无法判断修复阶段具体慢在
候选分割、网格连通性检查还是骨架处理。同时，TorchServe 请求没有独立超时；ARAP 会静默
丢弃落在三角网格外的 pin；渲染帧数与 GIF 实际编码帧数可能不一致；Uvicorn access log
持续输出健康检查请求。

本次目标：

- 细分标注修复耗时，给后续优化提供证据。
- 明确记录 ARAP 丢弃的关节名称和坐标，不在证据不足时改变骨架。
- 为图像分析阶段增加可配置总超时，并沿现有基础设施重试链路处理。
- 同时记录动作源帧数、渲染帧数和 GIF 实际帧数，不改变 API 返回契约。
- 关闭重复的 Uvicorn access log，保留项目自己的请求日志。

## 2. 范围与约束

- 不重建或重启当前 Docker 服务；部署验证由后续单独执行。
- 不调整 TorchServe worker 数。
- 不改变 `force=true` 行为。
- 不修改 `vendor/AnimatedDrawings` 源码。
- 不改 ARAP 数学、网格密度、动作配方、动作帧数或精灵表契约。
- 不改变角色 worker 单任务串行消费与子进程隔离。
- 保持 Python 3.9 兼容和现有 DDD 调用方向。
- 新日志只用于诊断，不进入 API 响应或持久化业务契约。

## 3. 标注修复细分计时

在 `app/services/annotation_repair.py` 增加模块内计时上下文，使用
`time.perf_counter()`，日志统一为：

```text
标注修复计时：anno=<目录> stage=<阶段> elapsed=<秒>s
```

覆盖以下阶段：

- `baseline`：读取磁盘候选、计算原始关节偏移。
- `clean_check`：墨迹缺失量和像素连通性快速检查。
- `candidate.plain`、`candidate.ink`、`candidate.pad`：仅在实际执行对应候选时记录。
- `mesh.baseline`：计算原始网格不可达顶点数。
- `mesh.<crop>`：候选排序后实际执行的网格检查；惰性求值未执行的候选不造日志。
- `skeleton`：关节中轴吸附和肢体末端外推。
- `write`：texture、mask、配置写回及旧 overlay 清理。

顶层 `CharacterPipeline` 的 `repair` 总计时继续保留。细分日志必须反映真实执行路径，不能为
埋点重复调用 `segment()`、Delaunay 或 `mesh_unreachable()`。

## 4. ARAP 丢 pin 诊断

项目已有 `_LoggedAnimatedDrawing` 运行时子类。它在 vendor 构造完成后读取：

- `self.char_cfg.skeleton` 中的关节名称和归一化前像素坐标；
- `self.arap.pin_mask` 中每个 pin 是否进入网格；
- `self.arap.pin_num` 的有效数量。

若存在被丢弃 pin，输出一条 WARNING：动作批次、丢弃数量、总数量，以及每个关节的
`name` 和原始 `loc`。若 vendor 对象结构不满足预期，诊断代码只输出异常说明，不得让原本能
完成的渲染失败。

本次不做 mesh-aware 关节移动。mask 中轴并不等价于最终三角网格内部，自动移动可能改变
动画外观；先通过真实样本确认具体关节和频率，再单独设计修复规则。

## 5. 图像分析总超时

`app/services/annotations.py` 在项目适配层包住一次完整的
`image_to_annotations()` 调用，使用 Unix `SIGALRM`/`setitimer` 实现总超时，不修改 vendor
的两个 `requests.post()`。默认值由 `ANALYZE_TIMEOUT_SECONDS=30` 提供，允许正浮点数；
非数字、零或负数视为部署配置错误，在任务执行时抛出明确 `ValueError`。

超时异常必须在 `NeedsCorrection` 分类前单独捕获并原样抛出。这样 `render_runner` 返回非零，
由 `character_worker` 按现有基础设施故障策略最多重试一次；不能把依赖超时错误发布为
`needs_correction`，因为用户修改图片无法解决服务不可用。

计时器必须在 `finally` 中恢复调用前的 signal handler 和剩余 timer，避免污染同一子进程中
后续逻辑。在非主线程或不支持 `SIGALRM` 的平台上，显式跳过内部计时器，继续依赖外层
120 秒子进程超时；记录 WARNING 说明降级原因。

## 6. 帧数可观测性

在批量动作渲染完成后记录每个动作的：

- motion/BVH 声明帧数；
- controller 实际渲染帧数；
- 写盘后 GIF 实际可读取帧数。

三者不一致时输出 WARNING，一致时输出 INFO。日志必须说明 GIF 编码器可能合并连续重复帧。
`build_sprite_sheet()` 和 API 继续以 GIF 实际帧数为准，避免声明一个客户端无法读取的帧。
本次不强行保留重复帧，也不修改动作闭环配方。

## 7. 请求日志降噪

应用中间件已经将 `/healthz`、`/artifacts/`、`/webgl/` 降到 DEBUG。Compose 中 API 的
Uvicorn 启动参数增加 `--no-access-log`，关闭重复的框架 access log；普通业务请求仍由
`app.main.log_request` 记录进入、状态码和耗时。

不修改 Nginx 上传缓冲。实测 101687 字节上传即使落临时文件，API 仍在 10.8 毫秒内返回，
不构成当前性能瓶颈。

## 8. 测试策略

- 修复计时：覆盖干净快速路径与需要候选/mesh 检查的路径，断言阶段存在且未增加算法调用。
- pin 诊断：构造 `pin_mask` 含 false 的 fake ARAP，断言日志带关节名和坐标；诊断字段缺失时
  渲染构造仍成功。
- 分析超时：先以短超时驱动阻塞 fake 失败，断言异常不会变成 `NeedsCorrection`；再验证正常
  完成、非法配置、计时器恢复和不支持 signal 的降级路径。
- 帧数：fake controller 写出被 GIF 合并的重复帧，断言差异 WARNING；一致时断言 INFO。
- 日志降噪：解析 Compose，断言 API 命令包含 `--no-access-log`；应用请求日志测试继续覆盖业务
  路径，保证没有把全部访问日志一起关闭。
- 完成后运行上述定向测试和全量 `.venv/bin/pytest -q`。

## 9. 验收标准

- 真实任务日志可分别看到 repair 子阶段耗时，而非只有 12.445 秒总值。
- ARAP 丢 pin 时能直接定位关节，不再只有匿名归一化坐标。
- TorchServe 卡住时约 30 秒退出本次 analyze，并进入现有 worker 重试，而不是等待 120 秒或
  发布用户修图终态。
- 帧数不一致有结构化日志解释，API 行为保持不变。
- Docker 日志不再被 Uvicorn `/healthz` access log 淹没，业务请求日志仍保留。

