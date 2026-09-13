# 关卡起终点规则（2026-09-14）

当前生成流程要求纸上有且仅有一个圆圈起点、一个三角旗帜终点。标记按形状识别，支持黑色空心笔画及轻微倾斜；请保持轮廓闭合、旗杆可见。严重断笔、任意旗面形状不在本轮支持范围。

- `playerStart` 取圆圈底部中央，`source=detected`；这是客户端脚底坐标，不吸附平台。
- `goalRegion` 为三角旗面和向下旗杆的识别范围；没有平台承载也允许生成。
- 缺圆圈返回 `failed / START_NOT_FOUND`；缺旗帜返回 `failed / GOAL_NOT_FOUND`；都缺返回 `START_AND_GOAL_NOT_FOUND`。
- 多个标记返回 `AMBIGUOUS_START` 或 `AMBIGUOUS_GOAL`。上述错误均 `retryable=false`，需要调整纸面重新拍照，而非自动重试。
- 识别及几何契约通过后返回 `ready`，`analysis.playability=not_checked`，路径和告警为空。不执行承载、跳距、着陆宽度及可达性分析。
- 纸张、平台墨迹和坐标边界校验仍保留；图像无法可靠解释时仍可返回 `needs_review`。
- 历史 `needs_fix` 契约保留供旧数据读取，新生成流程不产生该状态。任务哈希加入 `explicit-markers-v1`，避免相同照片复用旧的推断起点结果。

真实样本 `testdata/levels/real/low-contrast-paper.jpg` 有黑旗但没有圆圈，因此新规则下应失败。测试会在内存中的副本补画圆圈，验证它能通过纸张定位、标记识别、平台提取和完整关卡生成；原始样本保持不变。
