# motions：run / jump 动作资产

AnimatedDrawings 渲染用的动捕资产，来源为 CMU Motion Capture Database 的公开
BVH 转换版（Bruce Hahne 转换），通过 GitHub 公开镜像下载（免登录）。

## 来源与可追溯性

- 首选源 cgspeed.com（`sites.google.com/a/cgspeed.com/cgspeed/motion-capture-database`）
  已下线（404），改用镜像仓库：
  <https://github.com/una-dinosauria/cmu-mocap>（README 声明该数据为 cgspeed
  BVH 转换的副本，可自由使用）。
- 下载基址：`https://raw.githubusercontent.com/una-dinosauria/cmu-mocap/master/data/`

| 资产 | 来源文件 | Subject / Motion | 原始帧率 | 动作说明 |
| --- | --- | --- | --- | --- |
| `run.bvh` | `data/009/09_01.bvh` | Subject 9（run），trial 01 | 120 fps | 直线跑动，双腿交叉幅度小 |
| `jump.bvh` | `data/016/16_01.bvh` | Subject 16（run, jump, walk），trial 01 | 120 fps | 原地起跳到落地的单段（帧 128-183） |

候选中拒绝的：`09_03`（与 09_01 同为跑动，无额外优势）；`16_02`（跳段前有
位移跑动）；`13_39~42`（跳段更长但伴随多余晃动），选 `16_01` 因其空中段
（原始帧 128-175）干净的单抛物线。

## 预处理管线（一次性脚本）

1. **关节改名**（适配 `fair1_ppf.yaml` retarget 配置，仅改 HIERARCHY 名字，
   不动通道与数据）：

   | CMU 原名 | 改为 | 原因 |
   | --- | --- | --- |
   | `LowerBack` | `Spine` | fair1 脊柱命名 |
   | `Spine` | `Spine1` | 同上（逐个顺移） |
   | `Spine1` | `Spine2` | 同上 |
   | `Neck` | `Spine3` | 同上 |
   | `Neck1` | `Neck` | 同上 |
   | `LeftFingerBase` | `LeftHandEnd` | retarget 需要 `LeftHandEnd` |
   | `RightFingerBase` | `RightHandEnd` | retarget 需要 `RightHandEnd` |
   | `LThumb` / `RThumb` | `LeftHandIndex2` / `RightHandIndex2` | 避免未引用歧义名 |

2. **抽稀**（`server/scripts/decimate_bvh.py`，可复用）：

   ```bash
   python server/scripts/decimate_bvh.py <改名后源文件> <输出> --every K --start S --end E --in-place
   ```

   | 资产 | 参数 | 抽稀前后帧数 | 时长 |
   | --- | --- | --- | --- |
   | `run.bvh` | `--every 7 --start 0 --end 86 --in-place` | 149 → 13 | ~0.76s/循环 |
   | `jump.bvh` | `--every 5 --start 128 --end 183 --in-place` | 323 → 12 | ~0.5s |

   - run 取 1 个完整跑动循环（自相关周期约 87 帧 @120fps）。
   - jump 取起跳到落地单段（髋部高度 19.1→22.5→17.2 的完整抛物线）。
   - `--in-place`：冻结根节点 X/Z 位移（跑步机式原地循环），保留 Y 起伏。
     不冻结时角色会跑出画面（CMU 跑动含水平位移）。
   - Frame Time 自动乘以 K 保持播放速度。

## motion YAML 调参结论

两个 YAML 结构相同（仅帧区间不同），关键参数与原因：

- `filepath`：本机绝对路径（渲染时 chdir 到 vendor 目录，相对路径会解析失败）。
- `up: +y`：**CMU BVH 为 Y-up**。曾按简报用 `+z`，导致高度变化被旋到水平轴，
  跳跃无腾空、跑动侧漂；改 `+y` 后恢复正常。
- `forward_perp_joint_vectors`：关节对为 **Right→Left** 顺序。CMU 骨架初始
  朝向使原顺序（Left→Right）算出的 forward 与 +X 反平行，触发
  `Quaternions.rotate_between_vectors` 零四元数奇点（全部关节位置 NaN）；
  交换顺序翻转 forward 后消除。
- `groundplane_joint: LeftFoot`：曾试 `Hips`（jump），但 AnimatedDrawings 的
  根位置映射在两种情况下都不影响腾空表现，最终以脚贴地语义保留 `LeftFoot`。
- `scale: 0.01`：角色渲染大小实际由角色骨架与 BVH 肢体长度比决定
  （`char_bvh_root_offset` 归一），scale 对成片大小影响极小，保留该值。
- `end_frame_idx`：开区间（= 帧总数），与官方 `dab.yaml`（339 帧/339）一致。

最终产出帧数：run 13（目标 10-14 ✓），jump 12（目标 8-12 ✓）。

## 残留观感说明（尖刺记录）

- 受 2D 正面纹理局限，run 中双腿会部分重叠，属已知风格化局限（简报步骤 3 已预告）。
- jump 四肢动作幅度较小，主要靠整体腾空-下落表现跳跃，风格化可接受。
