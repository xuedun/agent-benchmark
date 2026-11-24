# USE 方法论总结表：8× 超分重放劣化全证据（当前实验数据汇总）

机器：debian188 = **双路 Kunpeng 950 7592C**（384 CPU / 24 CCL / 4 NUMA；L3 22.75MB×24；**24×64GB DDR5-6400，理论峰值 1229 GB/s**；2.6GHz schedutil；nvme）。
负载：轨迹重放 2 vCPU/4GB VM；1×=384 VM vs 8×=3072 VM（128 VM/CCL）；重点对象：四个 4×+ 劣化 build action（过滤口径 wall≥3s）。
数据源：第一阶段分解（usg 轮）、A1 v3 双轮（percpu 6 事件 + bw.csv + cpu 计时探针）、E2 受控加压、E3-1 相关性、带宽 sweep v1/v2。

---

## 一、USE 总表（每资源：使用率 / 饱和度 / 错误）

| 资源 | Utilization（1× → 8×） | Saturation（1× → 8×） | Errors | 判定 |
|---|---|---|---|---|
| **CPU 核（384）** | busy ~14% → **~50%**（vCPU run 均值 155 核） | 全程均值无排队；**齐射窗（rel 660-1020s）wait/run 0.65-2.56（~900 可运行 vCPU/384 核）** | 无 throttle | **✓ 间歇饱和**（steal 之源） |
| **L3（22.75MB×24 CCL）** | 容量型：每域活跃工作集 ~25-100MB ≫ 22.75MB | **MPKI 7-8.8→12.2-13.7（1.56-2.04×）；ch8_before 18-25→128-178ns（7-8.4×，驻留崩塌）**；E2 容量臂复现指纹 | — | **✓✓ 重度饱和（根因 #1）** |
| **DRAM 时延（随机）** | — | **ch128 133-136→183-213ns（1.39-1.58×）**；E2 延迟臂（B512）单独复现 | — | **✓ 中度饱和（根因 #2）** |
| **DDR 带宽（1229 GB/s）** | 28→**322 GB/s = 峰值 26%**（读 226/写 96；写增 22×） | **sweep 证明非全机带宽瓶颈**：顺序压到 1036 GB/s（84% 峰值）时延才 203ns；真实 8× 仅 322 GB/s 却 203ns → **瓶颈在每 CCL 本地 MC 排队 + 随机行缓冲冲突**（随机效率=顺序的 65%） | — | **✓ 本地 MC 饱和**（非全局带宽） |
| **内存容量（1.5TB）** | 8×mult1 峰值 ~975GB（安全）；8×mult2 AnonPages→1408GB | mult2 **15.6 分钟耗尽**：kswapd 70k 页/s + direct reclaim → vCPU wait/run 1.8-4.2（仅 mult2 长尾） | dmesg ndisc 邻居表溢出（6k netns，非致命）；看门狗 <15GB 触发 | **△ 仅 8×2 场景饱和** |
| **磁盘（nvme）** | util 12-24%，~7k IOPS | await≈0ms；PSI io_some 0.2%；guest 冷读探针 ×1.2 | 无 | **✗ 排除** |
| **频率** | 2.6GHz 恒定（负载下实测 scaling_cur_freq） | 无降频 | — | **✗ 排除** |
| **TLB/页走** | dtlb_walk 1.5-21M→86-476M/ep（16-57×） | L3 饱和的级联症状（页表页被逐出） | — | △ 症状（占 guest 残差 ~2-4pp） |
| **i-cache** | l1i_refill 27-146M→1705-8917M/ep（31-64×） | L3 饱和的级联症状（代码页被逐出） | — | △ 症状（占 guest 残差 ~7-15pp） |
| **缺页** | minflt 65-209k→196k-1.1M/ep（3-5×，随 wall 拉长） | 率（/s）1×→8× 反降 → 非负载敏感 | majflt≈0 | **✗ 排除**（内禀属性） |
| **控制面（server/ublk/envd）** | server ~2 核 / ublk ~3-5.7 核 | command_start 2.7→20.4ms；监控器采样退化 1.7→12-70s（PSS 扫描自阻塞） | bench SIGINT 被吞；monitor 孤儿泄漏 | △ 绝对值小（<1% wall） |
| **网络** | egress REJECT（构建无网） | — | mvn IPv6 确定性快失败（两格恒定 ×8 线性，不污染对比） | **✗ 排除** |
| **SMT sibling** | 1× vCPU 近独占物理核 → 8× sibling 被占 | 书中低 IPC 负载 SMT 惩罚小；未单独定量 | — | △ 候选残差（~5-10pp） |

## 二、四重点 action 的分解账（过滤后，每 VM 2 vCPU）

| Action | wall 1×→8× | guest 比 | steal 8×(vCPU·s) | steal 占 wall | 访存模型 | 覆盖率 |
|---|---|---|---|---|---|---|
| googleapis-59 | 16.2→144.4s（×8.93） | **3.48×** | 180.4 | 62% | MPKI1.68×DRAM1.51=2.54× | 73% |
| nats-71 | 13.3→64.2s（×4.82） | 1.86× | 80.1 | 62% | 2.44×（**超预测**，steal 掩盖） | 131% |
| opossum-42 | 9.8→52.3s（×5.32） | **3.09×** | 50.4 | 48% | 2.42× | 78% |
| plexsystems-58 | 3.6→25.8s（×7.26） | **3.50×** | 32.8 | 64% | 3.13× | **89%** |

一致性：guest 的 VM 间 CV 0.09-0.18（MPKI/ch128 更一致）；wall 的 CV 0.21-0.85（steal 相位彩票）。

## 三、因果链（每环有独立证据）

```
轨迹 delay 相位锁定
  → 重型构建同窗齐射（rel 660-1020s，~900 可运行 vCPU/384 核）        [hcollect schedstat]
  → ① CPU 排队 → steal 48-64% of wall                                [E3-1: Pearson 0.935 + 分位单调]
  → ② 24 CCL 同时失配 → L3 驻留崩塌（ch8 7-8×，MPKI 1.6-2.0×）        [E2 容量臂复现；A1 per-CCL PMC]
  → ③ 随机 miss 流 → 本地 MC 行缓冲冲突 → DRAM 时延 +39-58%           [E2 延迟臂；sweep 对照]
  → ②×③ = 访存停顿 ×2.4-3.1 → guest ×1.9-3.5                         [模型核算，覆盖 73-89%]
  → guest 变慢 → vCPU 占核更久 → 齐射更超订（convoy 互锁）→ ①加重
残差 11-27%：TLB/i-cache 级联（16-64×）+ SMT + MLP
```

## 四、关键修正记录（数据驱动的自我纠错）

| 修正 | 原结论 | 新证据 |
|---|---|---|
| 带宽瓶颈定位 | "322GB/s=90% 实用峰值→全机带宽饱和" | 硬件实查 DDR5-6400×24=1229 峰值（26%）；sweep 顺序 1036GB/s 才 203ns → **瓶颈是每 CCL 本地 MC+随机行冲突** |
| "排队≈0" | 稳态 vCPU wait≈0 → 排除 CPU | 齐射窗 wait/run 0.65-2.56（host 均值盲区）；guest steal 记账 + E3-1 相关 0.935 → **steal 是 wall 最大成分** |
| 短 episode | opossum/nats 大量 wall≈0-5s | watcher 正则误匹配 brief 命令（grep/find）→ **wall≥3s 过滤**（opossum 剔 30/142） |
| per-FC perf | 可测 victim MPKI | KVM guest 模式 PMU 切换，捕获率 1-10% → **per-CPU 系统级（-a -A）含 guest** |
| 探针计时 | MONOTONIC | steal 混入 → **CLOCK_THREAD_CPUTIME_ID** |

## 五、硬件/软件优化排序（按证据强度）

| # | 措施 | 打击目标 | 预期 | 依据 |
|---|---|---|---|---|
| 1 | **每 CCL 增大 L3 / 增加 L3 域数；或软件限每域并发构建** | 根因 #1（MPKI） | guest ×3.5→~×1.5-2 | E2 容量臂 + A1 MPKI + ch8 崩塌 |
| 2 | **错峰调度（打散 delay 相位）** | steal（48-64%） | wall 比 ×5-9→~×2-3 | E3-1 相关 0.935；**零硬件成本** |
| 3 | 更多内存通道/降随机惩罚（更强 MC 交织） | 根因 #2（本地 MC） | ch128 +39-58%→+15-25% | sweep 曲线 + 随机 65% 效率 |
| 4 | 内存容量 3TB（仅 8×2 场景） | mult2 长尾 | P95/P99 回稳态倍数 | profile_8x 耗尽实测 |
| 5 | 加核（须以更多 CCL 形式） | ①+② | 每域 VM 减半 | 单纯加核无效（核已 50% 闲） |
| 6 | 提频 / 换盘 / 网络 | — | ≈0 | 已排除 |

## 六、数据工件索引

| 数据 | 位置（188） |
|---|---|
| 分解（步骤一） | /tmp/opencode/usg_{1x,8x}/decomposition.json |
| A1 v3 全字段 | /tmp/opencode/a1_{1x,8x}/analysis_full.json + percpu.csv + bw.csv |
| E2 受控加压 | /tmp/opencode/l3exp2/l3exp2_results.json |
| E3-1 相关性 | /tmp/opencode/a1_8x/e3_correlation.json |
| 带宽 sweep | /tmp/opencode/bw_sweep{,2}/sweep.csv |
| 带宽校准 | /tmp/opencode/bw_cal_result.txt |
| 文档族 | 本机 analysis/（总纲/分解/实验一二三/Guest分析/模型验证/逐VM明细/残差指标/书籍视角） |
