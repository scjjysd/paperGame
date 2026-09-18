# 角色任务渲染性能优化设计

日期：2026-09-18

## 1. 背景与目标

`testdata/myson/man1.jpg` 的角色任务曾耗时约 117 秒；复测时两次渲染子进程均以 `-9` 退出，Docker 事件明确记录 OOM。诊断确认有两个独立但叠加的问题：

1. TorchServe 在未限制 worker 数时，按 Docker 可见的 12 个 CPU 为检测模型和姿态模型各启动 12 个 worker。24 个模型进程空闲即占约 11.7--11.9 GiB。
2. `man1.jpg` 的修复后轮廓生成 5,217 个网格顶点和 10,944 条边。AnimatedDrawings 为单个动作创建的 ARAP 稠密矩阵峰值超过 2 GiB；当前 `run`、`jump` 又分别完整创建一次 Scene、网格和 ARAP。

本次优化目标：

- 允许部署者通过 `.env` 控制每个 TorchServe 模型的 worker 数，未配置时保持 TorchServe 原生默认。
- 同一角色的多个动作共享静态角色、网格、ARAP 和 OpenGL 上下文。
- `run`、`jump` 保持顺序渲染，禁止动作级并发造成内存峰值叠加。
- 增加阶段耗时与网格规模日志，让慢任务和高内存任务可直接定位。
- 不改变图像分析、标注修复、动作配方、渲染画面、精灵表契约、任务状态机或 `force=true` 行为。

## 2. 约束与非目标

### 2.1 硬约束

- 不修改 `vendor/AnimatedDrawings` 的 Python 源码。
- 保持 DDD 调用方向：worker 调应用服务，应用服务通过适配层调用 vendor；API 不直接调用渲染细节。
- 保留 `render_animation()` 单动作接口，诊断脚本和现有调用方继续可用。
- 保留角色 worker 单任务串行消费与子进程隔离。
- 保留 `run` 后 `jump` 的动作顺序。
- 保持 Python 3.9 兼容。

### 2.2 非目标

- 不删除或改变 `force=true`；它继续用于测试和人工强制重跑。
- 不增加角色 worker 并发，不引入 Celery/RQ。
- 不调整网格采样密度、轮廓简化阈值、ARAP 算法或动作帧数。
- 不启用 GPU，不升级 TorchServe、PyTorch、OpenMMLab 或 AnimatedDrawings。
- 不优化关卡解析；`level1.jpg` 的服务端解析实测约 1 秒，不是本次瓶颈。

## 3. 方案选择

### 3.1 采用：项目适配层内批量顺序渲染

在 `app/services/render_scene.py` 中建立项目自有的多动作渲染适配器。它使用 AnimatedDrawings 的现有类型构造一次静态角色，在同一 Scene 和 View 中依次切换动作并输出 GIF。vendor 源码保持不变。

优点：

- 消除第二次网格、ARAP 和 OpenGL 初始化。
- 保留现有单动作接口，影响面集中。
- 可用回归测试逐帧比较旧路径和新路径。
- 符合项目的 vendor 零改动原则。

代价：

- 适配层需要调用 AnimatedDrawings 的 `_initialize_retargeter_bvh()` 私有方法。
- 必须显式恢复第 0 帧并为每个动作创建全新的可变配置，防止状态串扰。
- 需要用契约测试锁定依赖的 vendor 行为。

### 3.2 不采用：修改 vendor 增加 `switch_motion()`

接口最直接，但违反 vendor 零改动约束，并增加未来同步上游代码时的补丁维护成本。

### 3.3 不采用：跨 `render.start()` 缓存 ARAP 矩阵

跨独立 Scene 序列化或共享网格、稀疏矩阵、rig 与 OpenGL 资源，生命周期复杂且容易产生不可见状态污染，收益不如单进程批量渲染确定。

## 4. TorchServe worker 配置

### 4.1 对外配置

项目对外暴露一个变量：

```dotenv
# 每个 TorchServe 模型的 worker 数。
# 正整数：检测模型和姿态模型各启动该数量的 worker。
# 留空：保持 TorchServe 原生默认，当前行为不变。
# 当前角色消费并发为 1；低内存环境建议 1，需要并发余量时可设 2。
TORCHSERVE_WORKERS_PER_MODEL=2
```

本机 `.env` 被 Git 忽略，实施时写入实际值 `2`。可提交的配置说明放在 `docker-compose.yml` 和 `README.md`。

### 4.2 映射语义

该变量映射为 TorchServe 的：

```properties
default_workers_per_model=N
```

`load_models=all` 启动加载的每个模型分别应用该值：

- `TORCHSERVE_WORKERS_PER_MODEL=1`：检测 1 + 姿态 1，共 2 个模型 worker。
- `TORCHSERVE_WORKERS_PER_MODEL=2`：检测 2 + 姿态 2，共 4 个模型 worker。
- 留空：不生成 `default_workers_per_model`，保持当前按 TorchServe 原生规则决定数量。

仅接受十进制正整数。`0`、负数、带小数或非数字均视为部署配置错误，容器打印明确错误后退出，不能静默回退。

### 4.3 启动方式

新增项目自有 `docker/torchserve-entrypoint.sh`，由 Compose 只读挂载到 TorchServe 容器并作为 entrypoint：

1. 将镜像内 `/home/torchserve/config.properties` 复制到容器临时目录。
2. 环境变量非空时先校验，再追加 `default_workers_per_model=N`。
3. 环境变量为空时不追加任何默认 worker 配置。
4. 用临时配置以前台模式启动 TorchServe，使其成为容器主进程并正确接收退出信号。

该方式不修改镜像内基础配置，也不要求重建 TorchServe 镜像。修改 `.env` 后只需重新创建容器。若未来改为把脚本复制进镜像，Dockerfile 中应把该 `COPY` 放在模型下载之后，保证昂贵依赖和模型层继续命中缓存。

## 5. 多动作渲染架构

### 5.1 接口

保留现有：

```python
render_animation(char_anno_dir, motion_cfg_fn, out_gif,
                 use_mesa=None, retarget_cfg=None) -> Path
```

新增批量接口：

```python
render_animations(char_anno_dir,
                  motions: Sequence[Tuple[str, Path, Path]],
                  use_mesa=None, retarget_cfg=None) -> Dict[str, Path]
```

每个 `motions` 元素依次为 `(动作名, motion 配置路径, GIF 输出路径)`。返回值按动作名映射到生成的 GIF 路径。接口保留调用方传入的顺序；动作名必须唯一，空序列和重复动作名立即抛出 `ValueError`。`CharacterPipeline.render_character()` 固定传入 `run`、`jump`，不创建线程、进程或异步任务。

### 5.2 生命周期

批量适配器负责一次完整生命周期：

1. 为所有动作解析绝对路径并生成各自动作场景配置。
2. 为第一个动作构造 `CharacterConfig`、`MotionConfig`、`RetargetConfig`。
3. 先保存 SceneConfig 中的角色配置，再把 `animated_characters` 置为空来创建不含角色的 Scene；随后构造项目适配的 AnimatedDrawing 子类并通过 `scene.add_child()` 注入。不得 monkeypatch vendor 模块。
4. 创建一个 View；项目自有的顺序视频控制器复用该 View，每个动作只关闭自己的 writer，不调用 `view.cleanup()`。
5. AnimatedDrawing 只执行一次 mask/texture 加载、mesh 生成、关节到三角面映射、ARAP 初始化和 OpenGL 缓冲初始化。
6. 创建第一个动作的 writer/controller，顺序渲染全部帧并关闭 writer，但不销毁 View。
7. 在切换动作前，使用当前动作的第 0 帧恢复角色原始姿态。
8. 为下一个动作重新创建 `MotionConfig` 与 `RetargetConfig`，重新执行角色姿态运行时检查，再替换 retargeter。
9. 将 Scene 与 AnimatedDrawing 时间重置为 `0.0`，更新到新动作第 0 帧。
10. 创建新的 writer/controller，顺序渲染该动作。
11. 全部动作结束或发生异常时，在最外层 `finally` 中只清理一次 View/OpenGL 上下文。

### 5.3 为什么每个动作必须使用新配置

AnimatedDrawings 的初始化会修改 `RetargetConfig` 内的集合与列表，例如移除映射、逐项 `pop()` 计算肢体长度。复用同一个配置对象会让第二个动作读取被消费过的状态。因此每次切换都必须从配置文件重新构造对象；只复用静态角色资源和 ARAP。

### 5.4 第 0 帧恢复

`motion_2d` 的现有硬约束是每个动作第 0 帧等于修复后的原始骨架姿态。切换前先把旧动作时间设为 0 并调用更新，可确保新 retargeter 的角色肢体长度计算不受上一动作末帧影响。随后再安装新 retargeter 并将时间归零。

如果该前提在未来动作资产中不成立，批量适配器必须拒绝复用或改为保存和恢复原始 rig 变换；不能带着上一动作末帧初始化下一动作。

### 5.5 兼容边界

- `CharacterPipeline.render()` 继续走 `render_animation()`，行为不变。
- `CharacterPipeline.render_character()` 改走 `render_animations()`。
- `render_runner`、API 响应、产物路径和精灵表构建流程不变。
- `run.gif`、`jump.gif`、`run.png`、`jump.png` 名称不变。
- 任一动作失败时，异常继续交给 `render_runner`，由现有 worker 重试和错误码协议处理。

## 6. 可观测性

### 6.1 阶段耗时

使用 `time.perf_counter()` 记录并输出毫秒或秒级耗时，至少覆盖：

- `analyze`
- `repair`
- `synth_motion`
- `static_scene_init`
- `retarget_run`
- `render_run`
- `retarget_jump`
- `render_jump`
- `sprite_sheet`
- `total`

日志必须包含任务或角色目录标识和动作名。日志仅用于诊断，不进入 API 契约。

### 6.2 网格与 ARAP 规模

项目适配的 AnimatedDrawing 子类覆盖 `_generate_mesh()`：调用 vendor 原实现后、ARAP 构造前，使用已经生成的 mesh 记录：

- mask 宽高
- mesh 顶点数
- 三角形数
- 去重边数
- 骨架 pin 总数
- 根据顶点、边和 pin 总数计算的 `A1`、`G`、`A2` 与两个正规矩阵形状及稠密字节峰值估算

ARAP 构造成功后，再记录其有效 pin 数以及 `A1`、`A2` 的实际形状和 `nbytes`。

规模日志必须复用已经生成的 mesh 数据，不能为了记录日志再次执行完整分析或创建第二套 ARAP。若 ARAP 在完成构造前发生 OOM，至少应已经输出 mesh 顶点、三角形和预估矩阵规模。

## 7. 错误处理与资源安全

- TorchServe worker 配置非法：容器启动失败，日志给出变量名、非法值和合法格式。
- View 创建失败：保持现有基础设施异常语义。
- 第一个或后续动作 retargeter 初始化失败：终止批次，不发布 ready。
- GIF writer 失败：终止批次，交由 worker 的现有重试机制处理。
- `NeedsCorrection` 与 `ASSET_MISSING` 保持当前业务终态。
- View/OpenGL 清理放在批量适配器最外层 `finally`，至多执行一次。
- 每个动作 writer 独立关闭，失败时不影响 View 的最终释放。
- 不并行渲染动作；未来即使角色 worker 扩容，也是一任务一进程内顺序渲染。

## 8. 测试策略

### 8.1 TorchServe 配置测试

- 留空时临时配置不含 `default_workers_per_model`。
- `1` 与 `2` 分别正确生成配置。
- `0`、负数、浮点数、空白夹杂和非数字启动失败。
- Compose 将项目变量传给启动脚本，但不使用 `${VAR:-12}` 固化默认值。
- Docker 实测管理接口中两个模型的 `minWorkers`、`maxWorkers` 和 worker 数均为 2。
- 清空变量重建容器后，管理接口恢复 TorchServe 原生默认。

### 8.2 生命周期单元测试

- 两个动作只创建一次静态 AnimatedDrawing 和 ARAP。
- 动作执行顺序严格为 `run`、`jump`。
- 两动作之间没有线程、进程或异步并发。
- 切换动作前恢复旧动作第 0 帧。
- Scene 和角色时间均重置为 `0.0`。
- 每个动作获得新的 MotionConfig、RetargetConfig 和 writer。
- View 在成功、首动作失败、次动作失败时均只清理一次。
- 单动作 `render_animation()` 兼容测试继续通过。

### 8.3 图像与契约回归

使用体量较小且当前能稳定渲染的 garlic 样本：

1. 用旧单动作路径分别生成 `run`、`jump` 基线。
2. 用新批量路径生成同样两个动作。
3. 解码 GIF 后逐帧比较 RGBA 像素、帧数、尺寸和 duration。
4. 比较生成的精灵表像素与元数据。

若无法做到逐像素一致，必须先解释具体差异并确认其不改变画面逻辑，不能仅放宽测试阈值。

### 8.4 Docker 性能验收

使用 `testdata/myson/man1.jpg`：

- `.env` 设置 `TORCHSERVE_WORKERS_PER_MODEL=2`。
- 确认检测和姿态模型各 2 个 worker。
- 通过正式 API 用 `force=true` 执行一次受控重跑。
- 记录 queued、processing、终态时间线。
- 采集 TorchServe、角色 worker 的 CPU 与内存峰值。
- 确认没有 Docker OOM 事件，任务达到 `ready`。
- 日志能区分分析、修复、静态初始化、两个动作渲染和拼表耗时。
- 与诊断基线约 117 秒及约 3.1 GiB 角色 worker 峰值比较；性能数据如实记录，不预设未经实测的改善百分比。

最后运行常规测试集和角色端到端冒烟。关卡链路不做代码修改，但需确认容器栈健康。

## 9. 影响文件

预计修改：

- `.env`：本机设置 `TORCHSERVE_WORKERS_PER_MODEL=2`，不提交。
- `docker-compose.yml`：注入配置并挂载 TorchServe 启动脚本。
- `docker/torchserve-entrypoint.sh`：校验变量并生成临时配置。
- `app/services/render_scene.py`：新增多动作顺序渲染适配器与网格日志。
- `app/services/character_pipeline.py`：调用批量接口并记录阶段耗时。
- `README.md`：配置语义、推荐值、重建方式和性能诊断说明。
- `tests/`：新增启动脚本、Compose、批量渲染生命周期、日志与图像回归测试；更新相关管线测试。

不修改：

- `vendor/AnimatedDrawings/animated_drawings/**`
- API 契约与状态模型
- Redis 队列和 worker 并发策略
- 动作配方和关卡解析逻辑

## 10. 发布与回退

发布顺序：

1. 先上线 TorchServe worker 配置并确认常驻内存下降。
2. 再上线批量顺序渲染与日志。
3. 使用 garlic 冒烟后执行 `man1.jpg` 性能验收。

回退方式：

- TorchServe：清空 `TORCHSERVE_WORKERS_PER_MODEL` 并重新创建容器，恢复原生默认。
- 渲染：`CharacterPipeline.render_character()` 切回逐动作调用 `render_animation()`；单动作接口始终保留。
- 产物和 API 契约未变，回退不需要迁移 Redis 或磁盘数据。

## 11. 完成标准

- `.env` 的正整数配置能同时控制检测和姿态模型，留空保持原生默认。
- `run`、`jump` 顺序执行且共享一次静态网格、ARAP 和 OpenGL 初始化。
- garlic 的新旧路径输出逐帧一致。
- `man1.jpg` 不再因本次已知内存争用触发 OOM，并达到现有业务终态规则下的 `ready`。
- 日志可直接看到阶段耗时、网格规模和矩阵规模。
- 常规测试与角色端到端冒烟通过。
- 没有修改 vendor Python 源码、API 契约或动作处理逻辑。
