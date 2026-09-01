# 涂鸦角色动画管线（方案 A）设计

> 对应 `2026-09-01-paper-game-p0.md` 中的服务端工作包 S1 与任务 2。
> 范围锁定：P0 的 run + jump 侧视两套动画，透明 PNG 精灵表输出。
> 8 方向扩展不在本设计范围，留给未来"图生视频"路线评估。

## 1. 结论

采用 facebookresearch/AnimatedDrawings（MIT，已归档、行为冻结）自部署路线：

```
character.png
  → ① TorchServe 分析（检测/分割/6 关节骨架，Docker）
  → ② BVH 动作重定向渲染（run.bvh / jump.bvh，透明 GIF 帧序列）
  → ③ 精灵表后处理器（自写：拆帧、统一画布、脚底对齐、拼表）
  → run.png / jump.png + 元数据 JSON
```

组件边界：①② 为 AnimatedDrawings 现成能力，只配置不改源码；③ 为唯一新代码（`server/app/services/character_pipeline.py`）。组件间以文件 + JSON 交接，任一步可独立重跑。

## 2. 部署（docker-compose 管理）

- `server/docker-compose.yml` 管理 TorchServe 容器（`paper-game/ad-torchserve:local`），端口 8080（推理）/ 8081（管理），内存上限 16GB，内置 python 探活健康检查。
- `server/scripts/setup-vendor.sh` 幂等拉取 AnimatedDrawings 源码到 `server/vendor/`（不入库），作为镜像构建上下文。
- 启动命令：`bash scripts/setup-vendor.sh && docker compose up -d --build`；探活 `curl http://localhost:8080/ping` 返回 `{"status":"Healthy"}`。
- **网络前置条件（已验证）**：本机网络无法直连 docker.io（IPv4/IPv6 均超时），必须在 Docker Desktop → Settings → Resources → Proxies 配置手动代理 `http://host.docker.internal:7890`（不能用 `127.0.0.1`，构建在 VM 内执行）；shell 里 `export all_proxy` 对守护进程无效。GitHub 可慢速直连，镜像内 `wget` 下载 .mar 模型权重依赖该链路。
- 动画渲染器（②）与后处理器（③）跑在宿主侧独立 Python 环境（conda/venv，Python 3.8，`pip install -e` vendor 源码），通过 HTTP 调用 8080。后续并入 FastAPI + Redis 队列时不改变该边界。

## 3. 输出契约

每个动作产出一张横向拼接的透明 PNG 精灵表与一份元数据：

```json
{
  "run":  { "spriteSheetUrl": "run.png",  "frameCount": 13, "fps": 12, "frameWidth": 256, "frameHeight": 256, "footAnchor": { "x": 128, "y": 250 } },
  "jump": { "spriteSheetUrl": "jump.png", "frameCount": 12, "fps": 12, "frameWidth": 256, "frameHeight": 256, "footAnchor": { "x": 128, "y": 250 } }
}
```

规则：

- 帧尺寸取该动作所有帧的最大包围盒，逐帧居中粘贴，保证切帧无裁切。
- 帧尺寸取该角色所有动作帧内容包围盒的并集（由 `render_character` 保证）。
- `footAnchor` 为脚底锚点像素坐标，供 Unity 落地对齐；同角色两动作的帧尺寸保持一致。
- FPS 固定 12，帧数由 BVH 时长采样决定（实测 run 13 帧，jump 12 帧）。

## 4. 失败处理

- 无人形 / 检测置信度不足 / 骨架异常 → 返回 `needs_correction`，附带 `mask.png` 与关节点坐标（供骨架确认页编辑），不抛 500。
- 骨架可由 `fix_annotations.py` 流程人工修正后重渲染（对应 P0 的"骨架点确认"兜底）。
- 单张超过 60 秒未完成 → 记为失败样本，纳入 16/20 统计。

## 5. 动作资产准备

- 从 Mixamo 获取 Running、Jumping 动捕，导出后转成 BVH（run 优先选双腿交叉幅度小的跑动，缓解正面纹理做侧向跑时的挤压观感）。
- 为每个 BVH 编写 AnimatedDrawings 的 motion config 与 retarget config；资产放 `server/app/assets/motions/`。
- 该步骤是尖刺中唯一需要手工调参的环节，观感以 20 样本评审为准。

## 6. 验收与测试

- 契约测试：`CharacterPipeline.render(input_path, motion)` 返回精灵表 + 元数据；非法输入返回 `needs_correction`。
- 尖刺验收线（P0 锁定）：20 张样本 ≥ 16 张产出可用 run/jump，单张 ≤ 60 秒；记录在 `docs/animation-spike-results.md`。
- 未达标时启用既定降级：引导画人 + 预置角色，不改玩法范围。
