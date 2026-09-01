# 动画尖刺验收结果（方案 A：AnimatedDrawings 自部署）

运行环境：Python 3.9.6 / `server/.venv`，TorchServe 在线，分支 `feat/doodle-animation-spike`。
测试命令：`python -m pytest tests/test_spike_batch.py -v -s`（完整输出存档于 `server/out/spike-batch-log/batch-acceptance-output.log`，不入库）。

样本说明：验收线要求 20 张样本（≥ 16 成功且单张 ≤ 60 秒）。官方示例中 `char1-4.png`、`squid.png` 在 GitHub 仓库 `main` 分支 404，已从 `download_samples.py` 的 `NAMES` 移除，仅 `garlic.png` 可下载。**手工补充儿童实画样本需家长授权，由人工负责，本次尖刺未包含**，因此当前样本数不足 20，验收裁决待定。

## ① 逐样本结果表

| 文件名 | 结果 | 耗时（秒，run+jump 两次渲染） | 失败原因 |
| --- | --- | --- | --- |
| garlic.png | success | 17.0 | — |

合计：1 个样本，1 成功，0 失败，0 需修正。

## ② 成功率与耗时对门槛的结论

验收线：20 样本中 ≥ 16 成功，且单张 ≤ 60 秒。

- 样本数：1 / 20，**不足**。测试断言 `len(SAMPLES) >= 20` 预期失败（`AssertionError: only 1 samples, need 20`），这是诚实的验收状态，不是管线缺陷。
- 可用样本实际成绩：1/1 成功（100%），单张耗时 17.0 秒，远低于 60 秒门槛；生成的 `run.gif` / `jump.gif` 均正常（`server/out/spike/garlic/`）。
- **结论：样本不足，验收裁决待定（需补足至 20 张真实儿童涂鸦后重跑 `pytest tests/test_spike_batch.py`）。** 现有 1 张样本的成绩仅作参考，不构成通过/不通过的依据。

## ③ 典型失败样例与 `needs_correction` 分类分布

本批次无失败样本，也无 `needs_correction` 样本：

| 分类 | 数量 |
| --- | --- |
| success | 1 |
| needs_correction:* | 0 |
| failed:* | 0 |

失败分类机制已在测试中就绪（`NeedsCorrection` 按 `reason` 归类、其他异常按类型名归类），待样本补足后可直接产出分布。

## ④ 降级决策

决策规则：

- 达标（20 样本 ≥ 16 成功且单张 ≤ 60 秒）→ 进入任务 3（异步服务化）。
- 不达标 → 启用引导画人 + 预置角色降级方案。

**当前状态：待定。** 现有样本不足，无法裁决。管线在唯一可用样本上端到端跑通（17.0 秒 < 60 秒门槛），初步表现积极，但须补足至 20 张真实儿童涂鸦（家长授权，人工采集）后重跑 `pytest tests/test_spike_batch.py` 再行裁决。
