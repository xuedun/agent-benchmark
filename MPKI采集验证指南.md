# MPKI 采集验证指南（collect_metrics.sh / Zen4）

目的：在使用 `scripts/collect_metrics.sh` 采集 L3 MPKI 之前，验证所选 perf 事件
**既可用、语义也正确**。适用于 AMD Zen 系（重点 Zen4/family 0x19；Zen5 见文末差异）。
核心风险：事件显示"在跑"但计数的不是缺失（语义错位）或根本没有计数（假采集）——
两步探测都通过才算可信。

## ① 事件可用性探测（1 分钟）

```bash
# 三条各跑一次，看输出是否出现 <not supported> / <not counted>
perf stat -a -e cycles,instructions -- sleep 1
perf stat -a -e ls_dmnd_fills_from_sys.dram_io_near,ls_dmnd_fills_from_sys.dram_io_far -- sleep 1
perf stat -a -e LLC-loads,LLC-load-misses -- sleep 1
```

| 结果 | 含义 |
|---|---|
| DRAM-fill 对有数 | ✅ 脚本首选路径可用（Zen4 预期如此，该事件自 Zen1 起存在） |
| LLC 对也有数 | 备用口径也活着（Zen4 内核对 0x19 的通用事件映射好于 Zen5） |
| 两对都 `<not supported>` | 脚本自动降级为"仅 IPC"并在 `collect.log` 写 WARN |

口径说明（选型依据）：

- **DRAM-fill 对**（`ls_dmnd_fills_from_sys.dram_io_near` + `...dram_io_far` 两事件求和）：穿透**所有**缓存到 DRAM
  的填充——**容量/超分/拐点分析应使用此口径**（脚本默认优先选择它）。
- **LLC-loads/misses**：L3 级缺失，含跨 CCD 远端 L3 命中部分。
- 两口径绝对值不可混比；跨机器/跨配置对比用各自的变化倍数。

## ② 值合理性验证（最关键——二分法）

先跑一次 90 秒短测，让脚本编译出指针追逐探针：

```bash
sudo ./scripts/collect_metrics.sh 90 /tmp/cmtest
grep -E 'miss_kind|mean_mpki|mean_ipc|mean_cycles' /tmp/cmtest/summary.txt
```

然后手动二分验证——同一探针、两个工作集（8MB 装得进 L3 vs 1GB 必然溢出）：

```bash
E='ls_dmnd_fills_from_sys.dram_io_near,ls_dmnd_fills_from_sys.dram_io_far'
perf stat -e cycles,instructions,"$E" taskset -c 0 /tmp/cmtest/lat_probe 8388608 5000000
perf stat -e cycles,instructions,"$E" taskset -c 0 /tmp/cmtest/lat_probe 1073741824 5000000
# 手算: MPKI = (near + far 两个事件求和) ÷ instructions × 1000
```

**判读表（预注册，勿事后拟合）**：

| 情形 | 判定 |
|---|---|
| 8MB → MPKI < 10；1GB → MPKI 几百~1000 | ✅ 事件语义正确，采集可信 |
| 两个都 ≈ 0 | ❌ 假采集（事件没在计数） |
| 两个数值相近 | ❌ 语义错位（数的不是缺失） |
| instructions ≈ 0 | ❌ 基线坏了 |

理论预期：1GB 随机指针追逐几乎每次访问都缺失 → MPKI ≈ 700~1000
（每次访问约 1 条指令）；8MB 装进 L3 → 接近零。**两个数量级的差距是健康签名**。

## ③ 端到端与降级检查（30 秒）

```bash
cat /tmp/cmtest/collect.log | grep -i warn      # 静默降级只有日志里能看到
grep 'miss_kind' /tmp/cmtest/summary.txt        # 应为 dram_fill（首选命中）
# summary.txt 的 mean_cycles_per_s ≈ 核数 × 实测频率——顺手验证频率真实性
```

注意：脚本事件探测失败是**静默降级**（IPC-only 模式），不刷屏——短测后必须检查
`collect.log` 有无 `WARN: no miss event set`。出现该句则 MPKI 数据为空。

## 前置条件

- root（system-wide perf 需要）
- perf、cc（编译探针）可用
- `/proc/pressure/*` 可选（无 PSI 的内核会自动跳过该项，不影响 MPKI）

## Zen4 与 Zen5 的差异备忘

| 项 | Zen4 (family 0x19) | Zen5 (family 0x1A) |
|---|---|---|
| DRAM-fill 对 | 预期可用 | 可用（已实测） |
| 通用 LLC-loads/misses | 有机会可用 | 大概率不可用（内核太旧不识别 0x1A） |
| 验证结论的可信度 | ①+② 双过即可信 | 同左；LLC 路径基本只剩 DRAM-fill 一条 |
