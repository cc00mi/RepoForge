"""
pipeline/dashboard.py

Dashboard 数据聚合 + 路由。

从 event log、benchmark 结果中读取数据，
汇总为 Dashboard 页面所需的结构化数据。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from flask import Blueprint, render_template_string

logger = logging.getLogger(__name__)

_dashboard_bp = Blueprint("dashboard", __name__, template_folder="templates")
_server_start_time = time.time()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RunSummary:
    task_id: str = ""
    status: str = ""
    steps: int = 0
    tokens: int = 0
    elapsed: float = 0.0
    started_at: str = ""


@dataclass
class DashboardData:
    total_runs: int = 0
    success_count: int = 0
    fail_count: int = 0
    error_count: int = 0
    total_tokens: int = 0
    avg_steps: float = 0.0
    avg_elapsed: float = 0.0
    recent_runs: list[RunSummary] = field(default_factory=list)
    benchmark_summary: dict | None = None
    server_uptime: str = ""
    model_name: str = ""
    handlers: list[str] = field(default_factory=list)
    log_dirs: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        done = self.success_count + self.fail_count
        if done == 0:
            return 0.0
        return self.success_count / done


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _collect_event_logs(log_dirs: list[str], limit: int = 50) -> list[RunSummary]:
    """扫描 event log JSONL 文件，提取运行摘要。"""
    runs: list[RunSummary] = []

    for log_dir in log_dirs:
        p = Path(log_dir)
        if not p.exists():
            continue
        for f in sorted(p.glob("*.jsonl"), reverse=True):
            try:
                summary = _parse_event_log(f)
                if summary:
                    runs.append(summary)
            except Exception:
                logger.debug("Failed to parse %s", f)

    runs.sort(key=lambda r: r.started_at, reverse=True)
    return runs[:limit]


def _parse_event_log(path: Path) -> RunSummary | None:
    """从单个 JSONL event log 中提取摘要。"""
    first_line = None
    last_line = None
    line_count = 0
    steps = 0
    status = "unknown"

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            line_count += 1
            if first_line is None:
                first_line = line
            last_line = line

    if first_line is None:
        return None

    try:
        first = json.loads(first_line)
        last = json.loads(last_line)
    except json.JSONDecodeError:
        return None

    task_id = first.get("task_id", path.stem[:12])
    started_at = first.get("timestamp", "")

    # 统计 steps
    # 重新扫描以精确计数
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                if evt.get("event_type") == "action":
                    steps += 1
                elif evt.get("event_type") == "task_complete":
                    status = "completed"
                elif evt.get("event_type") == "task_failed":
                    status = "failed"
            except json.JSONDecodeError:
                pass

    # 估算 token（从 action/observation 输出长度 sum / 4）
    tokens_est = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                pl = evt.get("payload", {})
                if isinstance(pl, dict):
                    output = pl.get("output", "") or pl.get("summary", "") or ""
                    tokens_est += len(str(output))
            except json.JSONDecodeError:
                pass
    tokens_est = tokens_est // 4  # rough estimate

    # 计算 elapsed
    elapsed = 0.0
    try:
        t0 = datetime.fromisoformat(first.get("timestamp", ""))
        t1 = datetime.fromisoformat(last.get("timestamp", ""))
        elapsed = (t1 - t0).total_seconds()
    except Exception:
        pass

    return RunSummary(
        task_id=task_id,
        status=status,
        steps=steps,
        tokens=tokens_est,
        elapsed=elapsed,
        started_at=started_at,
    )


def _collect_benchmark_results() -> dict | None:
    """读取最近的 benchmark 报告。"""
    results_dir = Path("./benchmark_results")
    if not results_dir.exists():
        return None

    reports = sorted(results_dir.glob("report_*.json"), reverse=True)
    if not reports:
        return None

    try:
        data = json.loads(reports[0].read_text(encoding="utf-8"))
        return {
            "file": reports[0].name,
            "model": data.get("model_name", "?"),
            "total": data.get("total", 0),
            "completed": data.get("completed", 0),
            "completion_rate": data.get("completion_rate", 0),
            "patches_produced": data.get("patches_produced", 0),
            "avg_steps": data.get("avg_steps", 0),
            "avg_elapsed": data.get("avg_elapsed_seconds", 0),
            "total_tokens": data.get("total_tokens", 0),
            "started_at": data.get("started_at", ""),
        }
    except Exception:
        return None


def collect_dashboard_data(config) -> DashboardData:
    """收集全部 dashboard 数据。"""
    log_dirs = [
        config.agent.log_dir,
        os.path.join(config.agent.log_dir, "pipeline"),
        os.path.join(config.agent.log_dir, "benchmark"),
        os.path.join(config.agent.log_dir, "review"),
        "./logs",
        "./logs/pipeline",
        "./logs/benchmark",
        "./logs/review",
    ]
    log_dirs = [d for d in log_dirs if Path(d).exists()]

    runs = _collect_event_logs(log_dirs)

    success_count = sum(1 for r in runs if r.status == "completed")
    fail_count = sum(1 for r in runs if r.status == "failed")
    error_count = sum(1 for r in runs if r.status == "unknown")

    total_tokens = sum(r.tokens for r in runs)
    avg_steps = sum(r.steps for r in runs) / len(runs) if runs else 0.0
    avg_elapsed = sum(r.elapsed for r in runs) / len(runs) if runs else 0.0

    uptime_seconds = int(time.time() - _server_start_time)
    h, m = divmod(uptime_seconds, 3600)
    mm, s = divmod(m, 60)
    uptime_str = f"{h}h {mm}m {s}s" if h > 0 else f"{mm}m {s}s"

    return DashboardData(
        total_runs=len(runs),
        success_count=success_count,
        fail_count=fail_count,
        error_count=error_count,
        total_tokens=total_tokens,
        avg_steps=round(avg_steps, 1),
        avg_elapsed=round(avg_elapsed, 1),
        recent_runs=runs[:20],
        benchmark_summary=_collect_benchmark_results(),
        server_uptime=uptime_str,
        model_name=f"{config.llm.provider}/{config.llm.model}",
        handlers=["issues", "pull_request", "check_run"],
        log_dirs=log_dirs,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@_dashboard_bp.route("/")
def index():
    """Dashboard 主页。"""
    # config 和 data 在注册 blueprint 时注入
    config = _dashboard_bp.config_ref
    data = collect_dashboard_data(config)
    return render_template_string(_DASHBOARD_HTML, data=data)


@_dashboard_bp.route("/api/stats")
def api_stats():
    """Dashboard 数据 API（JSON）。"""
    config = _dashboard_bp.config_ref
    data = collect_dashboard_data(config)
    return {
        "total_runs": data.total_runs,
        "success_count": data.success_count,
        "fail_count": data.fail_count,
        "success_rate": round(data.success_rate, 4),
        "avg_steps": data.avg_steps,
        "avg_elapsed": data.avg_elapsed,
        "total_tokens": data.total_tokens,
        "server_uptime": data.server_uptime,
        "model": data.model_name,
        "recent_runs": [
            {
                "task_id": r.task_id,
                "status": r.status,
                "steps": r.steps,
                "tokens": r.tokens,
                "elapsed": r.elapsed,
                "started_at": r.started_at,
            }
            for r in data.recent_runs[:10]
        ],
        "benchmark": data.benchmark_summary,
    }


def register_dashboard(app, config):
    """注册 dashboard 路由到 Flask app。"""
    _dashboard_bp.config_ref = config
    app.register_blueprint(_dashboard_bp, url_prefix="/dashboard")


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repoforge — Dashboard</title>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --muted: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d2991d;
    --blue: #58a6ff;
    --purple: #bc8cff;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.5; padding: 24px 32px;
  }
  h1 { font-size: 1.5rem; font-weight: 600; margin-bottom: 4px; }
  h2 { font-size: 1.1rem; font-weight: 600; margin: 24px 0 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .subtitle { color: var(--muted); font-size: 0.85rem; margin-bottom: 24px; }

  /* Cards */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 8px; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 16px 20px;
  }
  .card .label { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
  .card .value { font-size: 1.6rem; font-weight: 600; }
  .card .sub { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }
  .green { color: var(--green); }
  .red { color: var(--red); }
  .yellow { color: var(--yellow); }
  .blue { color: var(--blue); }
  .purple { color: var(--purple); }

  /* Table */
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }
  tr:hover { background: rgba(255,255,255,0.03); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.72rem; font-weight: 500; }
  .badge-ok { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-fail { background: rgba(248,81,73,0.15); color: var(--red); }
  .badge-err { background: rgba(210,153,29,0.15); color: var(--yellow); }

  /* Progress bar */
  .bar-wrap { height: 6px; background: var(--border); border-radius: 3px; margin-top: 8px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }

  /* Sections */
  .section { margin-bottom: 8px; }
  .empty { color: var(--muted); font-style: italic; padding: 16px 0; }

  /* Footer */
  .footer { margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--border); color: var(--muted); font-size: 0.78rem; display: flex; justify-content: space-between; }

  /* Responsive */
  @media (max-width: 600px) {
    body { padding: 12px 16px; }
    .cards { grid-template-columns: repeat(2, 1fr); }
    .card .value { font-size: 1.3rem; }
  }
</style>
</head>
<body>

<h1>Repoforge Pipeline</h1>
<p class="subtitle">{{ data.model_name }} &middot; Uptime: {{ data.server_uptime }} &middot; Handlers: {{ data.handlers|join(", ") }}</p>

<!-- Stats Cards -->
<div class="cards">
  <div class="card">
    <div class="label">Total Runs</div>
    <div class="value">{{ data.total_runs }}</div>
    <div class="sub">across {{ data.log_dirs|length }} log dirs</div>
  </div>
  <div class="card">
    <div class="label">Success Rate</div>
    <div class="value {% if data.success_rate >= 0.8 %}green{% elif data.success_rate >= 0.5 %}yellow{% else %}red{% endif %}">
      {{ "%.0f"|format(data.success_rate * 100) }}%
    </div>
    <div class="sub">{{ data.success_count }} completed / {{ data.fail_count }} failed</div>
    <div class="bar-wrap"><div class="bar-fill" style="width:{{ data.success_rate * 100 }}%; background:{% if data.success_rate >= 0.8 %}var(--green){% elif data.success_rate >= 0.5 %}var(--yellow){% else %}var(--red){% endif %}"></div></div>
  </div>
  <div class="card">
    <div class="label">Avg Steps</div>
    <div class="value blue">{{ data.avg_steps }}</div>
    <div class="sub">per task</div>
  </div>
  <div class="card">
    <div class="label">Total Tokens</div>
    <div class="value purple">{{ "{:,}".format(data.total_tokens) }}</div>
    <div class="sub">avg {{ "%.0f"|format(data.avg_elapsed) }}s per run</div>
  </div>
</div>

<!-- Benchmark -->
{% if data.benchmark_summary %}
<h2>Benchmark (SWE-bench Lite)</h2>
{% set b = data.benchmark_summary %}
<div class="cards">
  <div class="card">
    <div class="label">Instances</div>
    <div class="value">{{ b.total }}</div>
    <div class="sub">{{ b.completed }} completed</div>
  </div>
  <div class="card">
    <div class="label">Completion Rate</div>
    <div class="value {% if b.completion_rate >= 0.8 %}green{% elif b.completion_rate >= 0.5 %}yellow{% else %}red{% endif %}">
      {{ "%.0f"|format(b.completion_rate * 100) }}%
    </div>
    <div class="sub">agent self-reported</div>
  </div>
  <div class="card">
    <div class="label">Patches Produced</div>
    <div class="value blue">{{ b.patches_produced }}</div>
    <div class="sub">out of {{ b.total }} instances</div>
  </div>
  <div class="card">
    <div class="label">Avg Time / Instance</div>
    <div class="value purple">{{ "%.0f"|format(b.avg_elapsed) }}s</div>
    <div class="sub">{{ b.avg_steps }} avg steps &middot; {{ "{:,}".format(b.total_tokens) }} tokens</div>
  </div>
</div>
{% endif %}

<!-- Recent Runs -->
<h2>Recent Runs</h2>
{% if data.recent_runs %}
<div class="table-wrap">
<table>
<thead>
  <tr><th>Task ID</th><th>Status</th><th>Steps</th><th>Tokens</th><th>Time</th><th>Started</th></tr>
</thead>
<tbody>
  {% for r in data.recent_runs %}
  <tr>
    <td><code style="font-size:0.8rem">{{ r.task_id[:16] }}</code></td>
    <td>
      {% if r.status == "completed" %}
        <span class="badge badge-ok">COMPLETED</span>
      {% elif r.status == "failed" %}
        <span class="badge badge-fail">FAILED</span>
      {% else %}
        <span class="badge badge-err">UNKNOWN</span>
      {% endif %}
    </td>
    <td>{{ r.steps }}</td>
    <td>{{ "{:,}".format(r.tokens) }}</td>
    <td>{{ "%.0f"|format(r.elapsed) }}s</td>
    <td style="color:var(--muted);font-size:0.78rem">{{ r.started_at[:19] }}</td>
  </tr>
  {% endfor %}
</tbody>
</table>
</div>
{% else %}
<p class="empty">No runs recorded yet. Start the server and trigger some events.</p>
{% endif %}

<div class="footer">
  <span>Repoforge Pipeline</span>
  <span>Model: {{ data.model_name }}</span>
</div>

</body>
</html>"""


def create_dashboard_app(config):
    """创建一个独立的 Flask app，仅包含 dashboard（不包含 webhook）。"""
    try:
        from flask import Flask
    except ImportError:
        raise ImportError("Flask is required for dashboard. Run: pip install flask")

    app = Flask(__name__, template_folder="templates")

    import logging as _logging
    flask_log = _logging.getLogger("werkzeug")
    flask_log.setLevel(_logging.WARNING)

    register_dashboard(app, config)
    return app
