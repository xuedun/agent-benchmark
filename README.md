# agent-benchmark

使用内置 55 条 Agent 轨迹，在 AgentENV 上对比持续运行与暂停/恢复模式的耗时和资源开销。以下命令均在仓库根目录执行。

## 安装

需要 Python 3.11+。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r agent_replay/requirements.txt
```

## 数据集与服务

轨迹、镜像和映射已配好，直接使用：

- `artifacts/traces/replays/`：55 条轨迹。
- `artifacts/traces/image-map.json`：轨迹对应的 template 名称。
- `artifacts/images/manifest.csv`：55 组 ARM64 / x86_64 镜像地址。

真实压测需要启动 AgentENV，并按下节构建映射中的 55 个模板。回放默认连接本机服务并读取默认密钥文件，例如：

```bash
export E2B_API_URL=http://127.0.0.1:8000
export E2B_API_KEY_FILE=/var/lib/aenv/secrets/api-key
```

服务部署不同时调整这两个值，也可用 `E2B_API_KEY` 直接提供密钥。

## 构建 AENV 模板

在 AgentENV 主机上安装 Docker 和 `aenv` CLI，并用 `aenv auth` 配置服务地址和密钥。目标 Registry 须已启动、允许推送且能被 AgentENV 访问；默认使用 `127.0.0.1:5000/swerebench`。

```bash
# 自动识别本机架构，只下载对应的 55 个镜像
bash artifacts/images/download-images.sh

# 预览模板构建命令
python artifacts/images/build_template.py --dry-run

# 按本机架构构建全部 55 个模板
python artifacts/images/build_template.py
```

下载和构建分开执行。`build_template.py` 读取本地当前架构镜像，校验架构对应 `image-map.json`。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--arch` | 本机架构 | `arm64` 或 `x86`，须与 AgentENV 主机一致。 |
| `--registry` | `127.0.0.1:5000/swerebench` | 推送目标前缀，不带 `http://`；远程 Registry 按需先执行 `docker login`。 |
| `--template` | 全部 55 个 | 指定映射中的模板名，可重复传入。 |
| `--timeout` | `1800` | 每个模板构建的等待秒数；超时后先检查服务端状态。 |
| `--dry-run` | 关闭 | 仅打印命令，不查询或修改服务。 |
| `--list` | 关闭 | 仅列出轨迹、模板名和源镜像。 |

## 监控环境变量

自动监控应在 AgentENV 主机运行。在启动回放前设置以下变量，`continuous` 和 `pause-resume` 每轮自动启动的监控都会继承这些配置；直接运行 `agent_replay/observability/spotbox_monitor.py` 时同样生效。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SNAP_BASE` | `/var/lib/aenv/persisted-sandboxes/artifacts` | 服务端实际保存快照的 artifacts 目录，采集器读取其下的 `<sandbox_id>/<generation>/`；未设置或为空时使用默认值。 |
| `DISKS` | `sda,sdb,sdc` | 逗号分隔的磁盘设备名，对应 `/sys/block/<设备名>/stat`，不带 `/dev/` 前缀；忽略两侧空格和空项，设置为空字符串可关闭磁盘指标采集。不存在的设备会跳过。 |

例如：

```bash
export SNAP_BASE=/var/lib/aenv/persisted-sandboxes/artifacts
export DISKS=sda,sdb
```

## 运行

暂停/恢复模式，循环使用 55 条源轨迹组成 384 个任务，并发上限为 384，以下为运行示例，参数根据实际负载情况配置：

```bash
python -m agent_replay.runner bench \
  --mode pause-resume --base-count 384 \
  --trajectory-dir artifacts/traces/replays \
  --image-map artifacts/traces/image-map.json \
  --ratios 1 \
  --command-timeout-continue \
  --command-timeout-sec 300 \
  --launch-interval-sec 0.2 \
  --total-multiplier 2
```

持续运行对照：将 `--mode` 改为 `continuous`。`--base-count` 设置基础负载，`--ratios` 设置超分倍数，并发上限为两者乘积。重复实验时使用新的输出目录。

## 参数与默认值

下表列出 `python -m agent_replay.runner bench` 的全部参数。`--mode`、`--base-count`、`--trajectory-dir` 必填；其余可省略。时间单位均为秒，开关默认值见表。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--mode` | 无，必填 | `pause-resume` 或 `continuous`。前者在轨迹等待期间暂停沙箱并释放 RUNNING 名额，后者持续占用名额。 |
| `--base-count` | 无，必填 | 基础负载数量 N，正整数。两种模式的任务并发上限和 RUNNING 沙箱上限均为 k × N；pause/resume 不再额外限制 RUNNING 数。 |
| `--trajectory-dir` | 无，必填 | 源轨迹目录，读取其中的 `.json`、`.traj` 文件。 |
| `--image-map` | 不使用映射文件 | 轨迹到镜像/template 的 JSON 或 CSV 映射；省略时从轨迹内读取，显式映射优先。 |
| `--ratios` | `1` | 超分倍数 k，逗号分隔，例如 `1,2,4`；正整数，自动去重并升序执行。 |
| `--repeats` | `1` | 每个超分倍数执行的轮数，正整数。 |
| `--total-multiplier` | `1` | 每轮任务数倍数 M；总任务数为 `M × k × N`（k 为超分倍数、N 为 `--base-count`），不改变并发上限。 |
| `--launch-interval-sec` | `0` | 任务启动之间的最小间隔，可为非负小数；`0` 表示不额外间隔。 |
| `--output-dir` | `agent_replay/report/runs/<日期-时间-微秒>-<mode>/` | 输出目录；默认位置相对于项目中的 `agent_replay` 目录，自动生成带时间戳的目录名。 |
| `--api-url` | 环境变量 `E2B_API_URL`；未设置时为 `http://127.0.0.1:8000` | AgentENV 服务地址，命令行指定值优先。 |
| `--api-key-file` | 环境变量 `E2B_API_KEY_FILE`；未设置时为 `/var/lib/aenv/secrets/api-key` | API 密钥文件路径；已有非空 `E2B_API_KEY` 环境变量时直接使用该密钥，不读取文件。 |
| `--observability` / `--no-observability` | 开启 | 每轮自动启停监控并生成报告；continuous 生成 Excel、Markdown，pause/resume 额外生成 PNG。关闭时跳过采集和报告；dry-run 和结果复用不重复执行。 |
| `--observability-interval-sec` | `1.0` | 主机指标采样间隔（秒），必须为有限正数；沙箱列表轮询最短间隔仍为 1 秒。 |
| `--command-timeout-sec` | `300` | 命令超时秒数；`0` 表示关闭命令超时。超时前等待计入命令耗时，超时后的终止和日志处理单独统计。 |
| `--request-timeout-sec` | `120` | 创建、暂停、恢复、删除等沙箱 API 请求的超时，正整数；命令请求使用 `--command-timeout-sec`。 |
| `--sandbox-timeout-sec` | `86400`（24 小时） | 创建/恢复时传入的沙箱生存超时时间，正整数；与单条命令超时不同。 |
| `--command-timeout-continue` | 关闭 | 命令超时后继续执行该轨迹的后续命令；默认超时即停止当前轨迹。 |
| `--event-loop` | `uvloop` | 回放事件循环，支持 `asyncio`、`uvloop`；仅在最外层选择，两组测试需使用独立输出目录。 |
| `--stop-on-error` | 关闭 | 普通命令非零退出或普通命令 SDK 异常时停止当前轨迹；默认记录失败并继续。超时是否继续由 `--command-timeout-continue` 控制。 |
| `--stop-on-failure` | 关闭 | 某轮未通过完整性、回放或清理检查时，不再执行后续轮次；普通业务命令非零退出不一定使该轮失败。 |
| `--create-retries` | `1` | 创建遇到可安全重试的错误时，最多额外重试次数；`0` 表示不重试，结果不确定的创建不会盲目重复。 |
| `--resume-retries` | `2` | 恢复遇到可重试错误时，最多额外重试次数；`0` 表示不重试。仅用于 `pause-resume`，`continuous` 不接受此参数。 |
| `--cooldown-sec` | `0` | 实际执行的轮次之间的等待间隔，可为非负小数。 |
| `--setup-command` | 无 | 每条轨迹命令执行前添加的 shell 初始化命令。 |
| `--dry-run` | 关闭 | 校验输入、镜像映射及任务计划并写入计划文件，不连接 AgentENV，不创建沙箱，也不要求 API 密钥。 |
| `--mock` | 关闭 | 使用真实轨迹和本地成功沙箱，不执行命令、不读取凭据，自动关闭真实监控；保留轨迹延迟、日志与普通汇总。 |
| `--skip-delays` | 关闭 | 跳过所有轨迹 delay_time，仍保留原始延迟字段；不跳过启动间隔、冷却时间或名额等待。 |
| `--reuse` | 关闭 | 配置、任务计划及源轨迹哈希匹配时复用已有轮次结果；也可接着执行同目录的 dry-run 计划。已有未完成任务产物的轮次不能续跑，应换新目录。 |
| `--skip-orphan-cleanup` | 关闭 | 跳过真实回放前后的遗留沙箱扫描与清理；默认执行清理。 |
| `--verbose` | 关闭 | 启用 DEBUG 日志，并显示 SDK/HTTP 客户端的详细日志。 |
| `-h`、`--help` | 不显示 | 显示参数帮助并退出。 |

## 结果

结果保存在 `--output-dir` 下：`ratio-*/repeat-*/tasks/` 存放逐任务日志及结果，`batch-summary.json` 汇总每轮结果。默认同时生成监控报告。
