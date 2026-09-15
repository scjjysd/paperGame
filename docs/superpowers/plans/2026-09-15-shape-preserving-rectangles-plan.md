# 保留形状的矩形拆分实现计划

> **面向 AI 代理的工作者：** 使用 subagent-driven-development 执行单个耦合实现任务，随后分别进行规格、质量审查。代码直接修改原工程。

**目标：** 修复细长矩形重复边线与凹图形空气墙，复用现有 `blocks` 协议。

**架构：** 检测层确认闭合/实心形状，再调用独立矩形拆分模块，输出受限数量的矩形；原轮廓用于去除重复边线。发布层保留分解后的矩形，缓存盐升级 v4。

**技术栈：** Python、OpenCV、NumPy、pytest、既有 Unity BoxCollider2D。

## 任务 1：检测、拆分与发布的耦合修正

**文件：** 新增 `app/services/level_shape_rectangles.py`、`tests/test_level_shape_rectangles.py`；修改 `app/services/level_detect.py`、`app/services/level_parser.py`、`app/level_contracts.py` 及对应测试；新增真实图片回归素材到 `testdata/levels/real`。

- [ ] 编写失败测试：细长偏灰矩形只输出实体、不重复边线；空心/实心 L 形输出两个主要矩形，拐角点不覆盖；十字/阶梯主体覆盖且凹处不覆盖。模块示例断言：

```python
rectangles = decompose_shape(mask)
assert len(rectangles) == 2
assert not any(x <= 150 < x+w and y <= 40 < y+h for x, y, w, h in rectangles)
```

- [ ] 复现本次真实关卡背景：`/Users/caizhenyu/Library/Application Support/DefaultCompany/PaperGame/Levels/v1/165d62d1d7524586a6962f88573a9664/background.png`。保存回归副本；断言原 JSON 中的细长框不再退化为重复平台，两个 L 形空白点无碰撞。
- [ ] 运行红灯：`/private/tmp/papergame-marker-tests/bin/python -m pytest tests/test_level_shape_rectangles.py tests/test_level_detect.py tests/test_level_parser.py tests/test_level_contracts.py -q`，确认新增行为因未实现而失败。
- [ ] 实现独立拆分模块：先有限简化手绘轮廓，再水平区间扫描合并相邻相同区间；矩形近似有误差边界且受数量上限限制，不使用整个凹形 bbox 回退。检测层采用局部对比度闭合证据，保持开放平行线负例，避免绝对灰度门槛拒绝整张偏灰图片。单线保持原数组与厚度。
- [ ] 原始闭合轮廓用于重复边线排除，凹形 bbox 中的独立平台不得被擦除。发布层不得误删矩形分解片段，不以每个内部矩形必须带轮廓墨迹作为判据；需要沿检测证据链保留可信片段，同时保留边界、置信度及重复候选校验。
- [ ] 缓存盐 `explicit-markers-v4`，测试：新 ID 不等于 v3 ID，协议版本不变。
- [ ] 运行绿灯与全回归：`/private/tmp/papergame-marker-tests/bin/python -m pytest tests --ignore=tests/test_motion_2d.py -q`；现有 WebGL 竖屏模板失败单独记录，另运行排除此项的功能回归。不得修改无关页面。
- [ ] 自审与定向提交：只暂存本次模块、检测/发布/协议、测试与回归素材，`git diff --cached --check` 后提交。

## 任务 2：审查与最终验证

- [ ] 新规格审查者逐项检查完整设计文档与实现，独立复跑相关测试；发现问题由同一实现者返修并复审。
- [ ] 新质量审查者检查复杂度、形状覆盖与空白区误差、负例、缓存和发布证据，阻塞问题返修后复审。
- [ ] 主控复跑实际背景检测/解析，记录矩形数、平台数以及两个 L 形空白点无覆盖；复跑 Unity 已有矩形尺寸相关测试，生产代码无需求则不修改。
- [ ] 记录部署及已存在的无关失败：更新服务端并重启 API/Worker；重新上传生成 v4 任务，旧保存关卡不自动改变。
