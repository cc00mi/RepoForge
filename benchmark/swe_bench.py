"""
benchmark/swe_bench.py

SWE-bench 基准测试适配器。

流程：
1. 从 HuggingFace 加载 SWE-bench-lite 数据集
2. 对每个 instance：克隆仓库 → 切到 base_commit → 运行 agent → 提取 diff
3. 输出 SWE-bench 兼容的 predictions JSONL
4. 可选：调用 SWE-bench evaluation harness 做正式评测

用法：
    from benchmark.swe_bench import run_benchmark
    run = run_benchmark(limit=10)
    print(f"Agent resolved {run.resolved}/{run.total}")

独立运行：
    python -m benchmark.swe_bench --limit 10
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 确保项目根在 sys.path 中
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkResult:
    """单个 benchmark instance 的执行结果。"""
    instance_id: str
    repo: str
    base_commit: str
    status: str          # "completed" | "failed" | "error"
    steps_taken: int
    tokens_used: int
    elapsed_seconds: float
    patch: str = ""
    error_message: str = ""
    summary: str = ""


@dataclass
class BenchmarkRun:
    """一次完整 benchmark 运行的汇总数据。"""
    results: list[BenchmarkResult] = field(default_factory=list)
    model_name: str = ""
    started_at: str = ""
    finished_at: str = ""

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def completed(self) -> int:
        return sum(1 for r in self.results if r.status == "completed")

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.status == "error")

    @property
    def completion_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.completed / self.total

    @property
    def avg_steps(self) -> float:
        done = [r for r in self.results if r.status != "error"]
        if not done:
            return 0.0
        return sum(r.steps_taken for r in done) / len(done)

    @property
    def avg_elapsed(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.elapsed_seconds for r in self.results) / len(self.results)

    @property
    def total_tokens(self) -> int:
        return sum(r.tokens_used for r in self.results)

    @property
    def patches_produced(self) -> int:
        return sum(1 for r in self.results if r.patch.strip())

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "total": self.total,
            "completed": self.completed,
            "errored": self.errored,
            "completion_rate": round(self.completion_rate, 4),
            "patches_produced": self.patches_produced,
            "avg_steps": round(self.avg_steps, 1),
            "avg_elapsed_seconds": round(self.avg_elapsed, 1),
            "total_tokens": self.total_tokens,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def print_summary(self) -> None:
        """终端友好的汇总输出。"""
        print("\n" + "=" * 60)
        print("  SWE-bench Lite — Benchmark Results")
        print("=" * 60)
        print(f"  Model          : {self.model_name}")
        print(f"  Instances      : {self.total}")
        print(f"  Completed      : {self.completed} ({self.completion_rate:.0%})")
        print(f"  Errored        : {self.errored}")
        print(f"  Patches        : {self.patches_produced}")
        print(f"  Avg steps      : {self.avg_steps:.1f}")
        print(f"  Avg time       : {self.avg_elapsed:.0f}s")
        print(f"  Total tokens   : {self.total_tokens:,}")
        print(f"  Started        : {self.started_at}")
        print(f"  Finished       : {self.finished_at}")
        print("=" * 60)
        print()
        print("  注意：以上 'completed' 指 agent 自己报告完成任务。")
        print("  正式评测需要运行 SWE-bench evaluation harness 做测试验证。")
        print("  生成 predictions 文件后执行：")
        print("    python -m swebench.harness.run_evaluation \\")
        print("        --dataset_name princeton-nlp/SWE-bench_Lite \\")
        print("        --predictions_path <predictions.jsonl> \\")
        print("        --max_workers 4 --run_id repoforge")


# ---------------------------------------------------------------------------
# 数据集加载
# ---------------------------------------------------------------------------

def load_swebench_lite(
    limit: int | None = None,
    split: str = "test",
    instance_ids: list[str] | None = None,
) -> list[dict]:
    """
    从 HuggingFace 加载 SWE-bench-lite 数据集。

    Args:
        limit: 限制加载的 instance 数量，None = 全部
        split: 数据集 split（默认 "test"）
        instance_ids: 只加载指定 ID 的 instance

    Returns:
        instance 列表，每个包含 instance_id / repo / base_commit / problem_statement
    """
    try:
        from datasets import load_dataset
    except ImportError:
        sys.exit(
            "请先安装 datasets 库：pip install datasets\n"
            "SWE-bench-lite 数据集通过 HuggingFace datasets 加载。"
        )

    logger.info("Loading SWE-bench_Lite (%s split) ...", split)
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split=split)
    logger.info("Dataset loaded: %d instances total", len(ds))

    instances = []
    for row in ds:
        iid = row["instance_id"]
        if instance_ids and iid not in instance_ids:
            continue
        instances.append({
            "instance_id": iid,
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "problem_statement": row["problem_statement"],
            "hints_text": row.get("hints_text", "") or "",
            "created_at": str(row.get("created_at", "")),
        })
        if limit and len(instances) >= limit:
            break

    logger.info("Filtered to %d instances", len(instances))
    return instances


# ---------------------------------------------------------------------------
# 仓库管理
# ---------------------------------------------------------------------------

def _ensure_repo(repo_name: str, base_commit: str, repos_dir: Path) -> Path:
    """确保仓库已克隆并位于指定 commit。返回仓库路径。"""
    safe_name = repo_name.replace("/", "__")
    repo_dir = repos_dir / safe_name

    # 克隆
    if not (repo_dir / ".git").exists():
        url = f"https://github.com/{repo_name}.git"
        logger.info("Cloning %s -> %s", repo_name, repo_dir)
        subprocess.run(
            ["git", "clone", "--depth=1", url, str(repo_dir)],
            capture_output=True, text=True, timeout=600, check=True,
        )

    # Fetch 到完整历史（SWE-bench 的 base_commit 可能不在 shallow clone 中）
    subprocess.run(
        ["git", "fetch", "--unshallow", "origin"],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=120,
    )
    # 如果已经是完整历史，unshallow 会失败，忽略
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=120,
    )

    # 强制切到 base_commit + 清理
    subprocess.run(
        ["git", "checkout", "--force", base_commit],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=60, check=True,
    )
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=30,
    )
    # 清理 agent 可能创建的分支
    subprocess.run(
        ["git", "checkout", "--force", base_commit],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=60,
    )

    return repo_dir


# ---------------------------------------------------------------------------
# Patch 提取
# ---------------------------------------------------------------------------

def _get_patch(repo_dir: Path, base_commit: str) -> str:
    """提取从 base_commit 到当前状态的所有变更（含 committed + staged + unstaged）。"""
    try:
        # 暂存所有变更
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=30,
        )
        # 获取 staged diff vs base_commit
        proc = subprocess.run(
            ["git", "diff", "--cached", base_commit],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=60,
        )
        # 取消暂存（不影响工作区）
        subprocess.run(
            ["git", "reset", "-q"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=30,
        )
        return proc.stdout.strip()
    except Exception as e:
        logger.warning("Failed to get patch for %s: %s", repo_dir, e)
        return ""


# ---------------------------------------------------------------------------
# 单 instance 执行
# ---------------------------------------------------------------------------

def _build_task_description(instance: dict) -> str:
    """把 SWE-bench instance 转成 agent 可理解的任务描述。"""
    parts = [instance["problem_statement"]]
    if instance.get("hints_text"):
        parts.append(f"\n---\nHints:\n{instance['hints_text']}")
    return "\n".join(parts)


def run_single_instance(
    instance: dict,
    repos_dir: Path,
    backend,
    config,
) -> BenchmarkResult:
    """
    对单个 SWE-bench instance 运行 Repoforge。

    Args:
        instance: SWE-bench instance 字典
        repos_dir: 仓库缓存目录
        backend: LLMBackend 实例
        config: AppConfig 实例

    Returns:
        BenchmarkResult
    """
    from agent.core import Agent, AgentConfig
    from agent.event_log import EventLog
    from agent.task import Task
    from entry.cli import _build_registry

    instance_id = instance["instance_id"]
    repo_name = instance["repo"]
    base_commit = instance["base_commit"]

    # 1. 准备仓库
    try:
        repo_dir = _ensure_repo(repo_name, base_commit, repos_dir)
    except Exception as e:
        return BenchmarkResult(
            instance_id=instance_id, repo=repo_name,
            base_commit=base_commit, status="error",
            steps_taken=0, tokens_used=0, elapsed_seconds=0,
            error_message=f"repo setup: {e}",
        )

    # 2. 构建 agent
    registry = _build_registry(config)
    agent_cfg = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        stream=False,
    )
    agent = Agent(backend, registry, agent_cfg)

    task = Task(
        description=_build_task_description(instance),
        repo_path=str(repo_dir),
        issue_url=f"https://github.com/{repo_name}",
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    # 3. 运行 agent
    t0 = time.time()
    log_dir = os.path.join(config.agent.log_dir, "benchmark")
    try:
        with EventLog.create(task, log_dir=log_dir) as log:
            result = agent.run(task, log)
        elapsed = time.time() - t0
        patch = _get_patch(repo_dir, base_commit)

        return BenchmarkResult(
            instance_id=instance_id,
            repo=repo_name,
            base_commit=base_commit,
            status="completed" if result.is_success() else "failed",
            steps_taken=result.steps_taken,
            tokens_used=result.total_tokens,
            elapsed_seconds=elapsed,
            patch=patch,
            summary=result.summary or "",
        )
    except Exception as e:
        elapsed = time.time() - t0
        logger.exception("Error running instance %s", instance_id)
        return BenchmarkResult(
            instance_id=instance_id,
            repo=repo_name,
            base_commit=base_commit,
            status="error",
            steps_taken=0, tokens_used=0,
            elapsed_seconds=elapsed,
            error_message=str(e),
        )


# ---------------------------------------------------------------------------
# 批量执行
# ---------------------------------------------------------------------------

def _save_progress(results: list[BenchmarkResult], path: Path) -> None:
    """增量保存结果，防止中途崩溃丢失数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for r in results:
        data.append({
            "instance_id": r.instance_id,
            "repo": r.repo,
            "base_commit": r.base_commit,
            "status": r.status,
            "steps_taken": r.steps_taken,
            "tokens_used": r.tokens_used,
            "elapsed_seconds": r.elapsed_seconds,
            "error_message": r.error_message,
            "summary": r.summary,
            # patch 只存到 predictions 文件，不存进度（太长）
        })
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_progress(path: Path) -> set[str]:
    """加载已完成 instance 的 ID 集合，用于断点续跑。"""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {r["instance_id"] for r in data}
    except Exception:
        return set()


def save_predictions(
    results: list[BenchmarkResult],
    output_path: Path,
    model_name: str = "repoforge",
) -> Path:
    """保存为 SWE-bench 兼容的 predictions JSONL 文件。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for r in results:
            if not r.patch.strip():
                continue
            pred = {
                "instance_id": r.instance_id,
                "model_name_or_path": model_name,
                "model_patch": r.patch,
            }
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")
            count += 1

    logger.info("Predictions saved: %s (%d entries)", output_path, count)
    return output_path


def run_benchmark(
    backend=None,
    config=None,
    *,
    limit: int | None = None,
    instance_ids: list[str] | None = None,
    repos_dir: str | Path = "./benchmark_repos",
    output_dir: str | Path = "./benchmark_results",
    resume: bool = True,
) -> BenchmarkRun:
    """
    批量执行 SWE-bench-lite benchmark。

    Args:
        backend: LLMBackend（None = 从 config 自动创建）
        config: AppConfig（None = 加载默认配置）
        limit: 限制执行的 instance 数量
        instance_ids: 指定要执行的 instance ID 列表
        repos_dir: 仓库缓存目录
        output_dir: 结果输出目录
        resume: 是否支持断点续跑

    Returns:
        BenchmarkRun 汇总对象
    """
    from config.schema import load_config
    from llm.router import create_backend_from_config

    repos_dir = Path(repos_dir)
    output_dir = Path(output_dir)
    repos_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 加载配置
    if config is None:
        config = load_config()
    if backend is None:
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "model": config.llm.model,
            "api_key": config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })

    model_name = f"{config.llm.provider}/{config.llm.model}"

    # 加载数据集
    instances = load_swebench_lite(limit=limit, instance_ids=instance_ids)

    # 断点续跑
    progress_path = output_dir / "progress.json"
    completed_ids: set[str] = set()
    if resume:
        completed_ids = _load_progress(progress_path)
        if completed_ids:
            logger.info("Resuming: %d instances already completed", len(completed_ids))

    pending = [i for i in instances if i["instance_id"] not in completed_ids]

    if not pending:
        logger.info("All instances already completed.")

    # 加载已有结果
    results: list[BenchmarkResult] = []
    if resume and progress_path.exists():
        # 从进度文件重建结果列表
        try:
            data = json.loads(progress_path.read_text(encoding="utf-8"))
            for r in data:
                results.append(BenchmarkResult(
                    instance_id=r["instance_id"],
                    repo=r["repo"],
                    base_commit=r["base_commit"],
                    status=r["status"],
                    steps_taken=r["steps_taken"],
                    tokens_used=r["tokens_used"],
                    elapsed_seconds=r["elapsed_seconds"],
                    patch="",
                    error_message=r.get("error_message", ""),
                    summary=r.get("summary", ""),
                ))
        except Exception:
            results = []

    run = BenchmarkRun(
        model_name=model_name,
        started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    # 逐 instance 执行
    total_pending = len(pending)
    for idx, instance in enumerate(pending):
        iid = instance["instance_id"]
        print(f"\n[{idx + 1}/{total_pending}] {iid}  ({instance['repo']})")
        print(f"  issue: {instance['problem_statement'][:120]}...")

        result = run_single_instance(instance, repos_dir, backend, config)
        results.append(result)

        status_icon = {"completed": "+", "failed": "-", "error": "!"}.get(result.status, "?")
        print(f"  [{status_icon}] status={result.status}  steps={result.steps_taken}  "
              f"time={result.elapsed_seconds:.0f}s  tokens={result.tokens_used:,}")

        # 增量保存
        _save_progress(results, progress_path)

    run.results = results
    run.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 保存预测文件
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pred_path = output_dir / f"predictions_{ts}.jsonl"
    save_predictions(results, pred_path, model_name)

    # 保存完整报告
    report_path = output_dir / f"report_{ts}.json"
    report = run.to_dict()
    report["predictions_file"] = str(pred_path)
    report["instances"] = []
    for r in results:
        report["instances"].append({
            "instance_id": r.instance_id,
            "repo": r.repo,
            "status": r.status,
            "steps_taken": r.steps_taken,
            "tokens_used": r.tokens_used,
            "elapsed_seconds": r.elapsed_seconds,
            "error_message": r.error_message,
        })
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("Report saved: %s", report_path)

    return run
