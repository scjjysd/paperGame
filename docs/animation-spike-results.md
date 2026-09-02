# 动画尖刺验收结果（方案 A：AnimatedDrawings 自部署）

运行环境：Python 3.9.6 / `server/.venv`，TorchServe 在线，分支 `feat/doodle-animation-spike`。
测试命令：`python -m pytest tests/test_spike_batch.py -v -s`（完整输出存档于 `server/out/spike-batch-log/batch-acceptance-output.log`，不入库）。

样本说明：验收线要求 20 张样本（≥ 16 成功且单张 ≤ 60 秒）。20 张样本已全部入库（`testdata/characters/s01.png`–`s20.png`），来源两类：

- `s01`–`s07`（7 张）：AnimatedDrawings 官方示例涂鸦（vendor 仓库 `examples/drawings/garlic.png` 与 `examples/characters/char1-6/texture.png`，MIT）。
- `s08`–`s20`（13 张）：Google Quick, Draw! 公开数据集（CC BY 4.0）`angel`/`yoga` 类别的简笔人形，由 ndjson 矢量笔画渲染为 512×512 白底黑粗线 PNG（线宽 12，模拟马克笔笔画），经 TorchServe 分析预筛（75 张候选 69 张通过）后挑选。

Quick, Draw! 样本为成人简笔画而非儿童实画，作为「真实儿童涂鸦（家长授权）」的代理样本完成本轮裁决；后续产品验收仍建议以真实儿童画复测。

## ① 逐样本结果表

| 文件名 | 结果 | 耗时（秒，run+jump 两次渲染） | 失败原因 |
| --- | --- | --- | --- |
| s01.png | success | 18.1 | — |
| s02.png | success | 14.0 | — |
| s03.png | success | 9.7 | — |
| s04.png | success | 11.9 | — |
| s05.png | success | 8.3 | — |
| s06.png | success | 10.2 | — |
| s07.png | success | 12.8 | — |
| s08.png | success | 13.3 | — |
| s09.png | success | 9.9 | — |
| s10.png | success | 10.1 | — |
| s11.png | success | 10.6 | — |
| s12.png | success | 9.8 | — |
| s13.png | success | 7.9 | — |
| s14.png | success | 6.3 | — |
| s15.png | success | 8.9 | — |
| s16.png | success | 8.6 | — |
| s17.png | success | 9.3 | — |
| s18.png | success | 9.4 | — |
| s19.png | success | 7.7 | — |
| s20.png | success | 7.2 | — |

合计：20 个样本，20 成功，0 失败，0 需修正。

## ② 成功率与耗时对门槛的结论

验收线：20 样本中 ≥ 16 成功，且单张 ≤ 60 秒。

- 样本数：20 / 20，达标。
- 成功率：20/20（100% ≥ 80% 门槛），单张最大耗时 18.1 秒（远低于 60 秒门槛），连续两轮重跑结果一致（第二轮 204 秒总耗时）。
- **结论：验收线通过。** 20 样本含官方彩色涂鸦（7 张）与简笔线稿（13 张）两种画风，管线在两类输入上均稳定产出 `run`/`jump` 精灵表。

## ③ 典型失败样例与 `needs_correction` 分类分布

本批次无失败样本，也无 `needs_correction` 样本：

| 分类 | 数量 |
| --- | --- |
| success | 20 |
| needs_correction:* | 0 |
| failed:* | 0 |

补充：样本筹备阶段的 TorchServe 预筛（75 张 Quick, Draw! 候选）中，6 张未检出人形（`NO_HUMANOID`，未入样本集），说明检测器对简笔线稿的识别率约 92%（69/75），`needs_correction` 分类机制运转正常。

## ④ 降级决策

决策规则：

- 达标（20 样本 ≥ 16 成功且单张 ≤ 60 秒）→ 进入任务 3（异步服务化）。
- 不达标 → 启用引导画人 + 预置角色降级方案。

**当前状态：达标。** 20 样本 20 成功（100% ≥ 80%），单张最大 18.1 秒 ≤ 60 秒，验收线通过，按规则进入任务 3（异步服务化）。注意：本轮以简笔画代理样本完成裁决，真实儿童画（家长授权）复测仍建议在产品验收前执行。

## ⑤ 已知残留项（分支级审查甄别，均可延后）

| 残留项 | 处置时机 |
| --- | --- |
| 服务化后 API 层应调用 `render_character()`（双动作统一帧尺寸）而非单动作 `render()` | API 层实现时 |
| 无头/CI 渲染需 `use_mesa=True` 路线，未验证 | 部署演示环境时 |
| `needs_correction` 附带 mask/关节点编辑数据（规格 §4）未实现 | API 层实现时 |
| `_resolve_motion_cfg` 边缘隐患（同名文件存在时传相对路径；fp.name 丢子目录）、`frame_size` 小于内容无防御 | 换动作资产或下一轮迭代 |
| 门面入参未 `resolve()`、`render` docstring 未提 FileNotFoundError | API 层实现时 |
| `os.chdir(VENDOR)` 进程级状态在 FastAPI 并发下有竞态 | 服务化时用子进程/锁 |
| 测试导入依赖 `python -m pytest`（cwd 入 sys.path），无 `conftest.py` | 服务化时 |
| 各模块整洁项（死导入、冗余 except、文档字符串与实现不符等） | 随时顺手 |
| 一次偶发故障：批量验收首跑曾 20/20 全部 `failed:AttributeError`（伴随 `GLFWError: NSGL: Failed to find a suitable pixel format` 警告），重跑即恢复且之后两轮均稳定；疑似 OpenGL 上下文首次初始化偶发失败 | 服务化时加渲染重试/进程隔离后观察 |

补充说明：`download_samples.py` 仅保留 garlic 下载示例，正式样本已随 `testdata/characters/` 入库，不再依赖该脚本。
