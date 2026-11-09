"""Offline, per-ratio workbooks: planned tasks and horizontally aligned commands."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import logging
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .results import read_json
from ..runner.prepare import natural_key
from ..runner.replay.trajectory import SUBMIT_ACTIONS, load_trajectory

LOGGER = logging.getLogger(__name__)
MAX_ROWS = 1_048_576
MAX_COLUMNS = 16_384
MAX_TEXT = 32_767
SUMMARY_COLUMNS = [
    "轨迹名称", "超分比", "轮次", "轨迹内执行序号", "任务编号", "回放状态",
    "任务总耗时（秒）", "命令累计耗时（秒）", "最后10步平均耗时（秒）",
    "已执行命令数", "失败命令数", "超时命令数", "清理成功", "错误摘要", "原始结果路径",
]
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


@dataclass
class Warnings:
    count: int = 0
    messages: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.count += 1
        if len(self.messages) < 20:
            self.messages.append(message)
            LOGGER.warning("%s", message)


@dataclass
class Source:
    original: str
    path: Path
    sha256: str
    label: str
    commands: list[str]


@dataclass
class Task:
    ratio: int
    repeat: int
    index: int
    source: str
    result_path: Path
    ordinal: int = 0
    error: str | None = None
    item_error: str | None = None


@dataclass
class Step:
    elapsed: float | None
    succeeded: bool | None
    timed_out: bool | None
    infrastructure_error: bool | None
    return_code: int | None
    comment: str | None


@dataclass
class TaskResult:
    row: list[Any]
    steps: dict[int, Step]


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _duration(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _excerpt(value: Any, limit: int = 2000) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit - 9] + " [已截断]"


def _task_result_path(trial: Path, index: int, source: str) -> Path:
    # Keep the batch.py task directory convention, including its UTF-8 byte limit.
    name = Path(source).stem.removesuffix(".replay")
    name = re.sub(r"[^\w.-]+", "-", name).strip("._-") or "task"
    name = name.encode("utf-8")[:200].decode("utf-8", errors="ignore")
    return trial / "tasks" / f"{index:06d}-{name}" / "replay-result.json"


def _trial_results(trial: Path, warnings: Warnings) -> tuple[dict[int, list[Path]], dict[int, dict]]:
    paths: dict[int, list[Path]] = defaultdict(list)
    for path in sorted((trial / "tasks").glob("*/replay-result.json")):
        match = re.match(r"^(\d{6,})-", path.parent.name)
        if match:
            paths[int(match.group(1))].append(path)
    summary_path = trial / "batch-summary.json"
    items = {}
    if not summary_path.is_file():
        warnings.add(f"No batch summary; locating task results directly: {trial}")
        return paths, items
    try:
        summary = read_json(summary_path)
        raw_items = summary["items"]
        if not isinstance(raw_items, list):
            raise ValueError("items must be an array")
        for item in raw_items:
            if not isinstance(item, dict):
                raise ValueError("items must contain objects")
            index = _positive_int(item.get("task_index"), "summary task_index")
            if index in items:
                raise ValueError(f"duplicate summary task_index: {index}")
            items[index] = item
    except (OSError, ValueError, KeyError) as exc:
        warnings.add(f"Invalid batch summary; locating task results directly: {summary_path}: {exc}")
        items = {}
    return paths, items


def load_experiment(
    experiment_dir: Path, trajectory_dir: Path | None, warnings: Warnings,
) -> tuple[dict[str, Source], dict[int, list[Task]]]:
    manifest = read_json(experiment_dir / "trajectory-selection.json")
    configuration = manifest.get("configuration")
    selected = manifest.get("selected")
    if not isinstance(configuration, dict) or not isinstance(selected, list) or not selected:
        raise ValueError("Expected configuration and nonempty selected in trajectory-selection.json")
    raw_ratios = configuration.get("ratios")
    if not isinstance(raw_ratios, list) or not raw_ratios:
        raise ValueError("configuration.ratios must be a nonempty array")
    ratios = [_positive_int(value, "ratio") for value in raw_ratios]
    if len(set(ratios)) != len(ratios):
        raise ValueError("Duplicate configured ratios")
    repeats = _positive_int(configuration.get("repeats"), "repeats")
    mode = configuration.get("execution_mode")
    if mode not in ("continuous", "pause-resume"):
        raise ValueError("Invalid execution_mode in manifest")
    selections = {}
    sources = {}
    for item in selected:
        if not isinstance(item, dict):
            raise ValueError("selected must contain objects")
        index = _positive_int(item.get("selection_index"), "selection_index")
        original = item.get("source_trajectory")
        digest = item.get("sha256")
        if not isinstance(original, str) or not original or not Path(original).is_absolute():
            raise ValueError("Expected an absolute source_trajectory in selection")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError(f"Invalid source SHA-256: {original}")
        if index in selections:
            raise ValueError(f"Duplicate selection_index: {index}")
        selections[index] = item
        if original in sources:
            if sources[original].sha256 != digest.lower():
                raise ValueError(f"Conflicting hashes for source: {original}")
            continue
        path = trajectory_dir / Path(original).name if trajectory_dir else Path(original)
        if not path.is_file():
            raise FileNotFoundError(f"Source trajectory missing: {path}; use --trajectory-dir to relocate")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest.lower():
            raise ValueError(f"Source SHA-256 mismatch: {path}")
        _, _, steps = load_trajectory(path)
        terminal = next((i for i, step in enumerate(steps)
                         if step.action.lower() in SUBMIT_ACTIONS), len(steps))
        sources[original] = Source(
            original, path.resolve(), digest.lower(), Path(original).name,
            [step.action for step in steps[:terminal]],
        )
    counts = Counter(source.label for source in sources.values())
    for source in sources.values():
        if counts[source.label] > 1:
            source.label = source.original

    tasks_by_ratio: dict[int, list[Task]] = {}
    for ratio in sorted(ratios):
        tasks = []
        for repeat in range(1, repeats + 1):
            trial = experiment_dir / f"ratio-{ratio:02d}" / f"repeat-{repeat:02d}"
            plan_path = trial / "task-plan.json"
            if not plan_path.is_file():
                warnings.add(f"Missing task plan; skipping round: {plan_path}")
                continue
            plan = read_json(plan_path)
            if (plan.get("ratio") != ratio or plan.get("repeat") != repeat
                    or plan.get("execution_mode") != mode):
                raise ValueError(f"Task plan context differs: {plan_path}")
            records = plan.get("tasks")
            if not isinstance(records, list) or not records:
                raise ValueError(f"Expected nonempty tasks: {plan_path}")
            if plan.get("total_tasks") != len(records):
                raise ValueError(f"Task plan total_tasks differs: {plan_path}")
            paths, items = _trial_results(trial, warnings)
            seen = set()
            round_tasks = []
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError(f"Invalid task record: {plan_path}")
                index = _positive_int(record.get("task_index"), "task_index")
                selection_index = _positive_int(record.get("selection_index"), "task selection_index")
                selection = selections.get(selection_index)
                if (selection is None or record.get("source_trajectory") != selection["source_trajectory"]
                        or record.get("template") != selection.get("template")):
                    raise ValueError(f"Task does not match selected source: {plan_path}, task={index}")
                if index in seen:
                    raise ValueError(f"Duplicate task_index: {plan_path}, task={index}")
                seen.add(index)
                original = record["source_trajectory"]
                expected_path = _task_result_path(trial, index, original)
                candidates = paths.get(index, [])
                item = items.get(index, {})
                error = None
                if len(candidates) > 1:
                    error = f"Multiple result files for task {index}: {trial}"
                elif candidates and candidates[0] != expected_path:
                    error = f"Task result directory does not match plan: {candidates[0]}"
                if item and (item.get("source_trajectory") != original
                             or item.get("selection_index") != selection_index):
                    error = f"Summary source does not match plan: {trial}, task={index}"
                # Read the copied experiment, never a still-existing original absolute result_path.
                round_tasks.append(Task(ratio, repeat, index, original, expected_path,
                                        error=error, item_error=_excerpt(item.get("error")) or None))
            ordinals: Counter[str] = Counter()
            for task in sorted(round_tasks, key=lambda task: task.index):
                ordinals[task.source] += 1
                task.ordinal = ordinals[task.source]
            tasks.extend(round_tasks)
        if tasks:
            tasks_by_ratio[ratio] = tasks
    if not tasks_by_ratio:
        raise ValueError("No saved task plans found in the experiment")
    return sources, tasks_by_ratio


def _unavailable(task: Task, source: Source, status: str, error: str) -> TaskResult:
    return TaskResult([
        source.label, f"1:{task.ratio}", task.repeat, task.ordinal, task.index, status,
        None, None, None, None, None, None, "未知", _excerpt(error), str(task.result_path),
    ], {})


def read_task(task: Task, source: Source, warnings: Warnings) -> TaskResult:
    if task.error:
        warnings.add(task.error)
        return _unavailable(task, source, "结果损坏", task.error)
    if not task.result_path.is_file():
        error = f"无结果，无法确认是否启动: {task.result_path}"
        if task.item_error:
            error += f"; {task.item_error}"
        warnings.add(error)
        return _unavailable(task, source, "无结果", error)
    try:
        result = read_json(task.result_path)
        if result.get("trajectory_path") not in (source.original, str(source.path)):
            raise ValueError("result trajectory_path does not match planned source")
        raw_steps = result.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("result steps must be an array")
        steps = {}
        issues = []
        for raw in raw_steps:
            if not isinstance(raw, dict):
                raise ValueError("step must be an object")
            index = raw.get("index")
            if type(index) is not int or not 0 <= index < len(source.commands) or index in steps:
                raise ValueError(f"duplicate/out-of-range step index: {index}")
            if raw.get("action") != source.commands[index]:
                raise ValueError(f"step {index + 1} action does not match source")
            elapsed = _duration(raw.get("elapsed_sec"))
            succeeded = _flag(raw.get("succeeded"))
            timed_out = _flag(raw.get("timed_out"))
            infrastructure = _flag(raw.get("infrastructure_error"))
            code = raw.get("return_code")
            if code is not None and type(code) is not int:
                raise ValueError(f"invalid return_code at step {index + 1}")
            if elapsed is None:
                issues.append(f"步骤{index + 1}缺少有效耗时")
            if succeeded is None or timed_out is None or infrastructure is None:
                issues.append(f"步骤{index + 1}状态字段缺失或无效")
            comment = None
            if succeeded is not True or timed_out or infrastructure or elapsed is None:
                comment = (
                    f"任务编号: {task.index}\n退出码: {code}\n成功: {succeeded}\n"
                    f"超时: {timed_out}\n基础设施错误: {infrastructure}\n"
                    f"错误摘要: {_excerpt(raw.get('stderr'))}\n结果: {task.result_path}"
                )
                if elapsed is None:
                    comment += "\n耗时缺失或无效，单元格留空。"
            steps[index] = Step(elapsed, succeeded, timed_out, infrastructure, code, comment)
        completed = _flag(result.get("replay_completed"))
        if completed is True and len(steps) != len(source.commands):
            raise ValueError("replay_completed but source command steps are missing")
        if completed is None:
            issues.append("回放完成状态缺失或无效")
        task_elapsed = _duration(result.get("elapsed_sec"))
        if task_elapsed is None:
            issues.append("任务总耗时缺失或无效")
        cleanup = _flag(result.get("cleanup_succeeded"))
        if cleanup is None:
            issues.append("清理状态缺失或无效")
    except (OSError, ValueError) as exc:
        error = f"Invalid task result: {task.result_path}: {exc}"
        warnings.add(error)
        return _unavailable(task, source, "结果损坏", error)

    if issues:
        warnings.add(f"Incomplete task data: {task.result_path}: {'; '.join(issues)}")
    durations = [step.elapsed for step in steps.values()]
    total = sum(durations) if all(value is not None for value in durations) else None
    tail_indexes = range(max(0, len(source.commands) - 10), len(source.commands))
    tail = [steps[index].elapsed if index in steps else None for index in tail_indexes]
    tail_mean = sum(tail) / len(tail) if tail and all(value is not None for value in tail) else None
    failed = sum(step.succeeded is False for step in steps.values())
    if any(step.succeeded is None for step in steps.values()):
        failed = None
    timeouts = sum(step.timed_out is True for step in steps.values())
    if any(step.timed_out is None for step in steps.values()):
        timeouts = None
    errors = [_excerpt(result.get("error")), _excerpt(result.get("cleanup_error")), task.item_error]
    errors.extend(issues)
    status = "完整完成" if completed is True else "未完成" if completed is False else "完成状态未知"
    return TaskResult([
        source.label, f"1:{task.ratio}", task.repeat, task.ordinal, task.index, status,
        task_elapsed, total, tail_mean, len(steps), failed, timeouts,
        "是" if cleanup is True else "否" if cleanup is False else "未知",
        _excerpt("; ".join(error for error in errors if error)), str(task.result_path),
    ], steps)


def _layout(tasks: list[Task], sources: dict[str, Source]) -> tuple[list[str], list[tuple[int, int]]]:
    originals = sorted({task.source for task in tasks},
                       key=lambda original: (natural_key(sources[original].label), original))
    maxima: dict[int, int] = defaultdict(int)
    for task in tasks:
        maxima[task.repeat] = max(maxima[task.repeat], task.ordinal)
    columns = [(repeat, ordinal) for repeat, count in sorted(maxima.items())
               for ordinal in range(1, count + 1)]
    detail_rows = sum(len(sources[original].commands) for original in originals)
    if len(tasks) + 1 > MAX_ROWS or detail_rows + 1 > MAX_ROWS:
        raise ValueError("Excel row limit exceeded; reduce the experiment size")
    if len(columns) + 3 > MAX_COLUMNS:
        raise ValueError("Excel column limit exceeded; reduce repeats or tasks per source")
    return originals, columns


class WorkbookWriter:
    def __init__(self, warnings: Warnings):
        # Do not require openpyxl for runner imports, dry-run or CLI help.
        try:
            from openpyxl import Workbook
            from openpyxl.cell import WriteOnlyCell
            from openpyxl.comments import Comment
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError as exc:
            raise RuntimeError("Excel export requires openpyxl; install it with: python -m pip install openpyxl") from exc
        self.cell_type = WriteOnlyCell
        self.comment_type = Comment
        self.column_letter = get_column_letter
        self.warnings = warnings
        self.workbook = Workbook(write_only=True)
        self.header_font = Font(bold=True)
        self.wrap = Alignment(vertical="top", wrap_text=True)
        self.fills = {
            "failed": PatternFill("solid", fgColor="FCE4D6"),
            "timeout": PatternFill("solid", fgColor="FFF2CC"),
            "infrastructure": PatternFill("solid", fgColor="E4DFEC"),
            "missing": PatternFill("solid", fgColor="E7E6E6"),
        }
        self.summary = self.workbook.create_sheet("任务汇总")
        self.details = self.workbook.create_sheet("命令详情")
        self.summary.freeze_panes = "F2"
        self.details.freeze_panes = "D2"
        for i, width in enumerate([40, 10, 8, 18, 12, 18, 20, 22, 24, 18, 16, 16, 12, 60, 70], 1):
            self.summary.column_dimensions[get_column_letter(i)].width = width
        for column, width in (("A", 40), ("B", 12), ("C", 80)):
            self.details.column_dimensions[column].width = width

    def text(self, text: str, location: str) -> tuple[str, bool]:
        cleaned = INVALID_XML.sub(lambda match: f"\\u{ord(match.group()):04x}", text)
        changed = cleaned != text
        if len(cleaned) > MAX_TEXT:
            marker = "\n[已截断；请查看原始文件]"
            cleaned = cleaned[:MAX_TEXT - len(marker)] + marker
            changed = True
        if changed:
            self.warnings.add(f"Excel text escaped/truncated: {location}")
        return cleaned, changed

    def cell(self, sheet, value: Any, *, comment: str | None = None, location: str = "cell"):
        cell = self.cell_type(sheet, value=None)
        if isinstance(value, str):
            text, changed = self.text(value, location)
            # Assign sanitized text first, then override formula/error inference.
            cell.value = text
            cell.data_type = "s"
            cell.alignment = self.wrap
            if changed:
                comment = (comment or "") + f"\n显示文本已转义/截断，原始定位: {location}"
        else:
            cell.value = value
        if comment:
            safe_comment, _ = self.text(_excerpt(comment, 6000), location + " comment")
            cell.comment = self.comment_type(safe_comment, "agent_replay")
        return cell

    def headers(self, columns: list[tuple[int, int]], context: str) -> None:
        notes = (
            context + "\n命令耗时=记录的elapsed_sec（SDK命令启动和等待耗时），不包含推理等待、暂停或恢复。"
            "\n最后10步=源命令固定末尾min(10,N)条；缺任一步耗时留空，失败命令有耗时仍纳入。"
            "\n异常底色：失败/超时/基础设施错误；灰色=有计划但无记录或耗时无效；普通空白=无该次计划。"
            "\n批注提供执行结果，完整输出请查看JSON。任务未完成不等同于命令非零退出。"
        )
        for i in range(4, len(columns) + 4):
            self.details.column_dimensions[self.column_letter(i)].width = 22
        for sheet, names in (
            (self.summary, SUMMARY_COLUMNS),
            (self.details, ["轨迹名称", "命令序号", "命令内容"]
             + [f"第{repeat}轮-第{ordinal}次耗时（秒）" for repeat, ordinal in columns]),
        ):
            cells = []
            for index, name in enumerate(names):
                cell = self.cell(sheet, name, comment=notes if index == 0 else None)
                cell.font = self.header_font
                cells.append(cell)
            sheet.append(cells)

    def append_summary(self, result: TaskResult) -> None:
        cells = [self.cell(self.summary, value, location=str(result.row[-1])) for value in result.row]
        for index in (6, 7, 8):
            cells[index].number_format = "0.000000"
        self.summary.append(cells)

    def append_source(
        self, source: Source, executions: dict[tuple[int, int], TaskResult],
        columns: list[tuple[int, int]],
    ) -> None:
        for index, command in enumerate(source.commands):
            location = f"{source.path} step={index + 1} sha256={source.sha256}"
            cells = [self.cell(self.details, source.label), self.cell(self.details, index + 1),
                     self.cell(self.details, command, location=location)]
            for key in columns:
                result = executions.get(key)
                step = result.steps.get(index) if result else None
                cell = self.cell(self.details, step.elapsed if step else None,
                                 comment=step.comment if step else None, location=location)
                cell.number_format = "0.000000"
                if result is not None and (step is None or step.elapsed is None):
                    cell.fill = self.fills["missing"]
                if step:
                    if step.timed_out:
                        cell.fill = self.fills["timeout"]
                    elif step.infrastructure_error:
                        cell.fill = self.fills["infrastructure"]
                    elif step.succeeded is False:
                        cell.fill = self.fills["failed"]
                cells.append(cell)
            self.details.append(cells)

    def finish(self, path: Path, summary_rows: int, detail_rows: int, column_count: int) -> None:
        self.summary.auto_filter.ref = f"A1:O{summary_rows + 1}"
        self.details.auto_filter.ref = f"A1:{self.column_letter(column_count)}{detail_rows + 1}"
        self.workbook.save(path)

    def close(self) -> None:
        # Explicitly release write-only XML spools even if writing/saving raises.
        for sheet in self.workbook.worksheets:
            if not sheet.closed:
                sheet.close()
            writer = sheet._writer
            if writer is not None and os.path.exists(writer.out):
                writer.cleanup()
        self.workbook.close()


def export_timings(
    experiment_dir: Path, output_dir: Path, trajectory_dir: Path | None = None,
) -> int:
    experiment_dir = experiment_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    trajectory_dir = trajectory_dir.expanduser().resolve() if trajectory_dir else None
    if trajectory_dir is not None and not trajectory_dir.is_dir():
        raise NotADirectoryError(f"Source trajectory directory not found: {trajectory_dir}")
    warnings = Warnings()
    sources, tasks_by_ratio = load_experiment(experiment_dir, trajectory_dir, warnings)
    layouts = {ratio: _layout(tasks, sources) for ratio, tasks in tasks_by_ratio.items()}
    targets = {ratio: output_dir / f"ratio-{ratio:02d}.xlsx" for ratio in tasks_by_ratio}
    for target in targets.values():
        if os.path.lexists(target):
            raise FileExistsError(f"Refusing to overwrite existing workbook: {target}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for ratio, tasks in tasks_by_ratio.items():
        originals, columns = layouts[ratio]
        grouped: dict[str, list[Task]] = defaultdict(list)
        for task in tasks:
            grouped[task.source].append(task)
        writer = WorkbookWriter(warnings)
        temporary = None
        try:
            notes = f"实验: {experiment_dir}\n超分比: 1:{ratio}"
            if warnings.messages:
                notes += "\n输入告警（最多20条）:\n" + "\n".join(warnings.messages)
            writer.headers(columns, notes)
            for original in originals:
                source = sources[original]
                executions = {}
                for task in sorted(grouped[original], key=lambda task: (task.repeat, task.index)):
                    result = read_task(task, source, warnings)
                    writer.append_summary(result)
                    executions[task.repeat, task.ordinal] = result
                writer.append_source(source, executions, columns)
            fd, filename = tempfile.mkstemp(prefix=f".ratio-{ratio:02d}-", suffix=".xlsx", dir=output_dir)
            os.close(fd)
            temporary = Path(filename)
            detail_rows = sum(len(sources[original].commands) for original in originals)
            writer.finish(temporary, len(tasks), detail_rows, len(columns) + 3)
            # Atomic publish without overwriting a target created by another exporter.
            os.link(temporary, targets[ratio])
            print(f"Exported {targets[ratio]}: tasks={len(tasks)} commands={detail_rows} "
                  f"timing_columns={len(columns)}", flush=True)
        finally:
            try:
                writer.close()
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    if warnings.count:
        LOGGER.warning("Export completed with %d warning(s); at most 20 were logged. "
                       "See task error summaries and cell comments.", warnings.count)
    return 1 if warnings.count else 0
