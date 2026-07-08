"""
benchmark — 基准测试适配层。

为 Repoforge 接入业界标准评测基准（SWE-bench 等），
提供数据集加载、批量执行、指标收集、预测输出等功能。
"""

from benchmark.swe_bench import (
    BenchmarkResult,
    BenchmarkRun,
    load_swebench_lite,
    run_benchmark,
    run_single_instance,
    save_predictions,
)

__all__ = [
    "BenchmarkResult",
    "BenchmarkRun",
    "load_swebench_lite",
    "run_benchmark",
    "run_single_instance",
    "save_predictions",
]
