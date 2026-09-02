# 角色异步服务化 —— 端到端冒烟与全量回归结论

记录人：任务 8（端到端冒烟脚本 + 全量回归 + 结论记录）
分支：`feat/character-async-service`
执行环境：宿主机 macOS，4 容器栈（torchserve:8080 / api:8000 / redis / worker）已 Up 且健康，worker BRPOP 监听中；宿主 `server/.venv`（系统 python3 亦自带 PIL 11.3.0）。

---

## ① 冒烟结果

脚本：`server/scripts/smoke_e2e.sh`（真实 HTTP 全链路：健康检查 → 上传 → 轮询 → ready → 下载精灵表验证 PNG → 幂等复验）。

- **结果：SMOKE_E2E_PASS**
- **jobId：** `char_9c3ff81ce4ea`
- **样本：** `../testdata/characters/s01.png`
- **总耗时：** 20s（脚本超时上限 180s，余量充足）

逐状态轮询时间线：

| 时刻 | 状态 |
|------|------|
| 0s | processing |
| 5s | processing |
| 10s | processing |
| 15s | processing |
| 20s | ready |

精灵表尺寸（下载后 PIL 校验，模式均为 RGBA）：

| 动作 | 尺寸 (px) | 模式 |
|------|-----------|------|
| run | 3107 × 339 | RGBA |
| jump | 2868 × 339 | RGBA |

幂等验证：同一张图重复提交返回相同 `jobId=char_9c3ff81ce4ea`，幂等未被破坏。

---

## ② 回归结果

命令：`source .venv/bin/activate && python -m pytest tests/ -v --ignore=tests/test_spike_batch.py`

- **收集用例数：41**
- **通过数：41（全绿）**
- 失败/错误：0
- 警告：15（均为 vendor `np.bool8` 弃用告警与 urllib3/LibreSSL 告警，非功能性）
- 耗时：34.57s

说明：`tests/test_spike_batch.py` 为尖刺验收专用（宿主直调管线），不属于服务层回归，按简报排除，单独跑不阻塞。

---

## ③ Mesa 尖刺结论

- **结论：GO**（回退分支未启用）。
- 依据：任务 1 实证 `MESA_SMOKE_PASS` —— 容器内 OSMesa 软渲染成功产出 13 帧透明 GIF。
- 本次冒烟在 worker 容器内走 Mesa 软渲染路径完成 run/jump 两套动作的精灵表产出并 PASS，二次验证容器内 Mesa 渲染在真实服务栈下端到端可用。
- 因此部署形态维持全容器化（torchserve + redis + api + worker 4 容器），无需切换为「worker 宿主进程 + GLFW 本地渲染」的回退分支。

---

## ④ 残留问题与下一步

**残留问题：**
- 无阻塞性缺陷。冒烟与全量回归均通过。
- 非功能性告警（vendor `np.bool8` 弃用、urllib3/LibreSSL）来源于第三方依赖，不影响结果，暂不处理。

**下一步：**
1. **对接 P0 任务 6 Unity 客户端联调：** 将本服务 `POST /v1/characters` → 轮询 `GET /v1/characters/{jobId}` → 下载 `/artifacts/{jobId}/run.png`、`jump.png` 精灵表接入 Unity 侧，验证五态契约（queued/processing/ready/needs_correction/failed）与精灵表元数据在客户端的消费。
2. **演示阶段对象存储升级：** 当前产物由 api 容器本地静态目录 `/artifacts` 提供；演示阶段将精灵表与产物落至对象存储（如 OSS/S3），替换静态挂载，`spriteSheetUrl` 指向对象存储 CDN 地址，以支撑多实例与持久化。
