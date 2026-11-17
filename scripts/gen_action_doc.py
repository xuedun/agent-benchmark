#!/usr/bin/env python3
"""Generate the standalone most-slowed-actions document from analysis CSVs."""
import csv
from pathlib import Path
from collections import defaultdict

BASE = Path("/home/cxd/agent-benchmark/logs_mix_v3/analysis")
OUT = BASE / "慢化最多action.md"

# ---- category summary ----
cat_rows = []
with open(BASE / "category_summary.csv") as f:
    for r in csv.DictReader(f):
        cat_rows.append(r)
cat_rows.sort(key=lambda r: -float(r["dt_total_81"]))
total_dt = sum(float(r["dt_total_81"]) for r in cat_rows)

cat_table = ["| 类别 | 步数 | 1×均值(s) | 8×均值(s) | 倍数 | ΔT合计(s) | 占比 | 失败@1× | 失败@8× |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
for r in cat_rows:
    share = float(r["dt_total_81"]) / total_dt * 100 if total_dt else 0
    cat_table.append(
        f"| {r['category']} | {r['steps']} | {r['mean_cmd_1x']} | {r['mean_cmd_8x']} "
        f"| {r['ratio']} | {float(r['dt_total_81']):,.0f} | {share:.1f}% "
        f"| {r['fail_1x']} | {r['fail_8x']} |")

# ---- top actions ----
top = []
with open(BASE / "top_steps.csv") as f:
    for r in csv.DictReader(f):
        top.append(r)


def esc(s):
    return (s or "").replace("|", "\\|").replace("`", "'").strip()[:110]


top_table = ["| # | 轨迹#步骤 | 类别 | 1×(s) | 8×(s) | 倍数 | ΔT(s) | 命令 |",
             "|---:|---|---|---:|---:|---:|---:|---|"]
for i, r in enumerate(top[:100], 1):
    top_table.append(
        f"| {i} | {r['traj']}#{r['step']} | {r['cat']} | {float(r['mean_1x']):.1f} "
        f"| {float(r['mean_8x']):.1f} | {r['ratio']} | {float(r['dt81']):,.0f} "
        f"| `{esc(r['action'])}` |")

# ratio distribution among top100
import statistics
ratios = [float(r["ratio"]) for r in top[:100]]
cats = Counter = defaultdict(int)
for r in top[:100]:
    cats[r["cat"]] += 1

doc = f"""# 慢化最多的 Action（1×→8× 超分）

数据：logs_mix_v3 原始 4 格，按 (轨迹, 步骤号) 匹配对比 1_1 vs 8_1（每 VM 负载不变，唯一变量 = 并发 384→3072 VM）。
口径：
- **1×/8×均值** = 该 action 在对应格内所有拷贝的 command_sec 均值（拷贝数：1_1 约 7 份，8_1 约 56 份）
- **ΔT** = (8×均值 − 1×均值) × 8_1 拷贝数 = 该 action 对全池总执行时间的增量贡献
- 全部 4543 个 (轨迹,步骤) 对齐，action 文本零不匹配；失败命令数随拷贝数精确线性（×8），无负载相关失败污染
- 总 ΔT(8_1 − 1_1) = 194,603 核·秒

## 一、类别汇总

{chr(10).join(cat_table)}

**构建/包管理三巨头（go + npm/node + maven）= ΔT 的 94%**；倍数全类别均匀（×2.3-2.8，含 rm/find 等元数据操作）——"每次随机内存访问变贵"的指纹（详见 USE_report.md §3.1）。

## 二、Top 100 慢化 Action 明细（按 ΔT 排序）

{chr(10).join(top_table)}

## 三、规律

1. **头部全是重构建命令**：go build / go test、npm test / npm run build / npx tsc、mvn clean compile——随机访问密集、工作集数十-上百 MB 的编译/解析/测试负载。
2. **倍数与命令时长正相关**：Top100 中短命令（<5s）倍数 ×5-8，长命令（>30s）倍数 ×1.4-2.6；随机访问延迟通胀随命令内访问次数累积。
3. **Top100 倍数中位 ~4.6**（类别整体均值 ×2.4-2.8）：头部 action 被双重打击（更长 + 更多随机访问）。
4. Top100 类别分布：{dict(sorted(cats.items(), key=lambda kv: -kv[1]))}。
5. 高频受害轨迹：tikv/pd、fhir/sushi、lingui、fabien0102/ts-to-zod、ktsn/vuex-smart-module、opossum-tool、ontodev/robot、typescript-eslint 等多步骤构建密集型仓库。

## 四、数据文件

- 完整 Top 200：`analysis/top_steps.csv`；类别聚合：`analysis/category_summary.csv`
- 步骤原始聚合：`analysis/step_agg_{{1_1,1_2,8_1,8_2}}.csv`
- 生成脚本：`scripts/gen_action_doc.py`（本文档可复现）
"""
OUT.write_text(doc)
print(f"-> {OUT} ({len(doc)} bytes, top100 rows={len(top_table)-2})")
