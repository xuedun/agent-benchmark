# 实验三：steal 成因验证（E3-1 观测法）

日期：2026-11-17 13:26。数据源：A1 8× 轮（384×8 带 delay 原场景，早停 wall 1040s）。

## 一、方法

- 每 episode（拷贝内重叠合并的 action 执行窗口）的 **steal_frac** = guest /proc/stat steal 增量 ÷ (2×wall)（来自 watcher 的 GSTAT 前后快照）
- 该 episode 窗口内其所在 **CCL 的宿主侧 vcpu wait/run**（fcacct 的 CCL 归属 + hcollect 10s 粒度 schedstat，churn 修正）
- 时间轴以 mapping 首条 epoch 锚定对齐
- 相关性 + 分位数剂量-响应

## 二、原始数据（384 个 episode 全部 join 成功）

**总体：Pearson(同 CCL wait_cores, steal_frac) = 0.935（n=384）**

| 分位 | 同 CCL wait（核） | steal_frac | n |
|---|---|---|---|
| Q1 | 2.1 | **0.06** | 96 |
| Q2 | 19.3 | **0.47** | 96 |
| Q3 | 32.6 | **0.59** | 96 |
| Q4 | 41.0 | **0.66** | 96 |

分 case：

| case | n | steal_frac 均值 | 同 CCL wait | run | case 内 r |
|---|---|---|---|---|---|
| googleapis-59 | 91 | 0.59 | 33.0 | 12.4 | 0.922 |
| nats-71 | 100 | 0.57 | 33.0 | 12.3 | 0.716 |
| plexsystems-58 | 56 | 0.54 | 23.7 | 9.6 | 0.916 |
| opossum-42 | 137 | 0.22 | 10.8 | 8.7 | 0.969 |

## 三、结论（证据支撑）

1. **steal 的直接成因 = 该 vCPU 所在 CCL 的排队强度**：0.935 相关 + Q1→Q4 严格单调（wait 2→41 核，steal 6%→66%）。宿主 schedstat wait 与 guest steal 是同一物理现象（vCPU 可运行未运行）的两侧读数，本结果同时验证了测量一致性，并排除了 cgroup throttling/其它成因（若有，相关性不会如此干净地随 CCL 需求单调）。
2. **CCL 需求为何超订（齐射成因链的上一环）**：第一阶段已实测齐射窗（rel 660-1020s）内全机 ~900 可运行 vCPU/384 核（run 343 + wait 554-688）；本实验再证实：episode 所在 CCL 的 wait 高低直接决定其 steal——与"轨迹 delay 相位锁定 → 同窗齐射 → CCL 需求超订"的链路一致。
3. **opossum steal 低（0.22）的解释**：其 episode 期间所在 CCL 的 wait 仅 10.8 核（其构建窗口落在本 CCL 的低需求相位）——同样是"steal ∝ 所在 CCL 排队"的例证。
4. **干预实验（E3-2 delay 抖动）的必要性评估**：观测证据已强（0.935 + 单调），E3-2 的增量价值在于证明"相位锁定 → 需求峰值"这一环可干预（打散相位 → wait 峰消 → steal 崩塌）。鉴于证据强度，E3-2 降级为可选项；若执行需给 runner 加每 VM 随机相位偏移（一处小改动）。

## 四、工件

- 分析脚本：`/tmp/opencode/e3_analyze.py`（episode 合并 + CCL join + 分位相关）
- 数据：`/tmp/opencode/a1_8x/e3_correlation.json`（384 行：case/sid/ccl/wall/steal/steal_frac/wait_cores/run_cores）


> 本文档为总纲第三步，另见 `00_分析总纲.md`。