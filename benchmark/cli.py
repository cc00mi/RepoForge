"""
benchmark/cli.py

Benchmark CLI 入口。

用法：
    repoforge-bench run --limit 10
    repoforge-bench run --instance-ids "django__django-10097,sympy__sympy-12481"
    repoforge-bench report --report-file benchmark_results/report_xxx.json
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@click.group()
def main() -> None:
    """Repoforge Benchmark — 基准测试工具集。"""


@main.command()
@click.option("--limit", "-n", type=int, default=None, help="限制执行的 instance 数量")
@click.option("--instance-ids", default=None, help="逗号分隔的 instance ID 列表")
@click.option("--repos-dir", default="./benchmark_repos", help="仓库缓存目录")
@click.option("--output-dir", default="./benchmark_results", help="结果输出目录")
@click.option("--config", "-c", "config_path", default=None, help="配置 YAML 路径")
@click.option("--model", "-m", default=None, help="模型名（覆盖配置文件）")
@click.option("--no-resume", is_flag=True, help="禁用断点续跑")
@click.option("--use-pipeline", is_flag=True, help="使用四阶段流水线引擎")
@click.option("--evaluate", is_flag=True, help="跑完后自动调用 SWE-bench evaluation harness")
@click.option("--verbose", "-v", is_flag=True, help="详细日志")
def run(
    limit: int | None,
    instance_ids: str | None,
    repos_dir: str,
    output_dir: str,
    config_path: str | None,
    model: str | None,
    no_resume: bool,
    use_pipeline: bool,
    evaluate: bool,
    verbose: bool,
) -> None:
    """运行 SWE-bench Lite benchmark。

    对每个 instance：克隆仓库 → 切到 base_commit → 运行 agent → 提取 diff。
    完成后输出 predictions JSONL 文件和统计报告。

    \\b
    示例：
        repoforge-bench run --limit 5
        repoforge-bench run --limit 30 --model deepseek-v4-pro
        repoforge-bench run --instance-ids "django__django-10097"
        repoforge-bench run --limit 10 --output-dir ./results
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )

    from benchmark.swe_bench import run_benchmark
    from config.schema import load_config, merge_cli_overrides

    config = load_config(config_path)
    if model:
        config = merge_cli_overrides(config, model=model)

    # 解析 instance_ids
    ids_list: list[str] | None = None
    if instance_ids:
        ids_list = [x.strip() for x in instance_ids.split(",") if x.strip()]

    run_result = run_benchmark(
        config=config,
        limit=limit,
        instance_ids=ids_list,
        repos_dir=repos_dir,
        output_dir=output_dir,
        resume=not no_resume,
        use_pipeline=use_pipeline,
        evaluate=evaluate,
    )

    run_result.print_summary()
    if run_result.total == 0:
        sys.exit(0)
    # completion_rate < 50% 视为异常（可能是配置问题）
    if run_result.completion_rate < 0.5 and run_result.total >= 3:
        sys.exit(1)


@main.command()
@click.option("--report-file", "-r", required=True, help="JSON 报告文件路径")
def report(report_file: str) -> None:
    """查看 benchmark 报告。

    \\b
    示例：
        repoforge-bench report -r benchmark_results/report_20260101_120000.json
    """
    import json

    path = Path(report_file)
    if not path.exists():
        click.echo(f"报告文件不存在: {report_file}", err=True)
        sys.exit(1)

    data = json.loads(path.read_text(encoding="utf-8"))

    click.echo()
    click.echo("=" * 60)
    click.echo("  Benchmark Report")
    click.echo("=" * 60)
    click.echo(f"  Model         : {data.get('model_name', '?')}")
    click.echo(f"  Total         : {data.get('total', 0)}")
    click.echo(f"  Completed     : {data.get('completed', 0)}")
    click.echo(f"  Errored       : {data.get('errored', 0)}")
    click.echo(f"  Patches       : {data.get('patches_produced', 0)}")
    click.echo(f"  Completion %  : {data.get('completion_rate', 0):.1%}")
    click.echo(f"  Avg steps     : {data.get('avg_steps', 0):.1f}")
    click.echo(f"  Avg time      : {data.get('avg_elapsed_seconds', 0):.0f}s")
    click.echo(f"  Total tokens  : {data.get('total_tokens', 0):,}")
    click.echo(f"  Started       : {data.get('started_at', '?')}")
    click.echo(f"  Finished      : {data.get('finished_at', '?')}")
    click.echo(f"  Predictions   : {data.get('predictions_file', '?')}")
    click.echo("=" * 60)

    # 逐 instance 列表
    instances = data.get("instances", [])
    if instances:
        click.echo()
        click.echo(f"{'Instance':<42s} {'Status':<12s} {'Steps':<7s} {'Time':<8s}")
        click.echo("-" * 72)
        for inst in instances:
            sid = inst["instance_id"]
            if len(sid) > 40:
                sid = sid[:37] + "..."
            st = inst["status"]
            steps = str(inst.get("steps_taken", 0))
            t = f"{inst.get('elapsed_seconds', 0):.0f}s"
            color = {"completed": "green", "failed": "yellow", "error": "red"}.get(st, "")
            st_display = click.style(st, fg=color) if color else st
            click.echo(f"  {sid:<42s} {st_display:<12s} {steps:<7s} {t:<8s}")

    click.echo()


@main.command()
@click.option("--predictions", "-p", required=True, help="predictions JSONL 文件路径")
@click.option("--max-workers", type=int, default=4, help="并行 worker 数（默认 4）")
@click.option("--run-id", default="repoforge", help="Evaluation run ID")
@click.option("--timeout", type=int, default=900, help="每 instance 超时秒数（默认 900）")
@click.option("--namespace", default="docker.io", help="Docker image namespace")
def evaluate(
    predictions: str,
    max_workers: int,
    run_id: str,
    timeout: int,
    namespace: str,
) -> None:
    """调用 SWE-bench evaluation harness 做正式评测。

    需要先安装 swebench：pip install swebench
    需要 Docker 环境。

    \\b
    示例：
        repoforge-bench evaluate -p benchmark_results/predictions_xxx.jsonl
        repoforge-bench evaluate -p predictions.jsonl --max-workers 8 --timeout 1800
    """
    try:
        from swebench.harness.run_evaluation import main as run_eval
    except ImportError:
        click.echo(
            "请先安装 swebench：pip install swebench\n"
            "SWE-bench evaluation harness 需要 Docker 环境。",
            err=True,
        )
        sys.exit(1)

    click.echo(f"Running SWE-bench evaluation ...")
    click.echo(f"  predictions : {predictions}")
    click.echo(f"  max_workers : {max_workers}")
    click.echo(f"  run_id      : {run_id}")
    click.echo(f"  timeout     : {timeout}s")
    click.echo()

    sys.argv = [
        "run_evaluation",
        "--dataset_name", "princeton-nlp/SWE-bench_Lite",
        "--predictions_path", predictions,
        "--max_workers", str(max_workers),
        "--run_id", run_id,
        "--timeout", str(timeout),
        "--namespace", namespace,
    ]
    run_eval()


if __name__ == "__main__":
    main()
