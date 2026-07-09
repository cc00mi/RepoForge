"""
pipeline/cli.py

Pipeline CLI 入口。

用法：
    repoforge-pipe serve --port 8000
    repoforge-pipe setup
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import click

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@click.group()
def main() -> None:
    """Repoforge — CI 原生 agent 管线。"""


@main.command()
@click.option("--port", "-p", type=int, default=8000, help="监听端口（默认 8000）")
@click.option("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
@click.option("--config", "-c", "config_path", default=None, help="配置 YAML 路径")
@click.option("--webhook-secret", default=None, help="GitHub webhook secret")
@click.option("--app-id", default=None, help="GitHub App ID")
@click.option("--private-key-path", default=None, help="GitHub App 私钥文件路径（推荐，避免 .env 多行值警告）")
@click.option("--debug", is_flag=True, help="Flask debug 模式")
@click.option("--verbose", "-v", is_flag=True, help="详细日志")
def serve(
    port: int,
    host: str,
    config_path: str | None,
    webhook_secret: str | None,
    app_id: str | None,
    private_key_path: str | None,
    debug: bool,
    verbose: bool,
) -> None:
    """启动 GitHub App webhook server。

    监听 GitHub 事件，自动触发 agent 处理。

    \\b
    环境变量（与命令行参数等效）：
        GITHUB_APP_ID            — GitHub App ID
        GITHUB_APP_PRIVATE_KEY_PATH — 私钥文件路径（推荐，避免 .env 多行值警告）
        GITHUB_WEBHOOK_SECRET    — Webhook secret

    \\b
    注意：
        建议通过文件路径指定私钥（GITHUB_APP_PRIVATE_KEY_PATH），
        而不是将 PEM 内容直接写在 .env 中，以免 python-dotenv 发出警告。

    \\b
    示例：
        repoforge-pipe serve --port 8000 --debug
        repoforge-pipe serve --app-id 123456 --private-key-path key.pem
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )

    from config.schema import load_config
    from pipeline.auth import GitHubAppAuth
    from pipeline.server import create_app

    config = load_config(config_path)

    # GitHub App 认证参数，优先级：CLI > 环境变量 > 配置项
    _app_id = app_id or os.environ.get("GITHUB_APP_ID", "")
    _key_path = private_key_path or os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "")
    _key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
    _secret = webhook_secret or os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    if not _app_id:
        click.echo(
            "错误：需要 GitHub App ID。\n"
            "  通过命令行 --app-id 或环境变量 GITHUB_APP_ID 设置。",
            err=True,
        )
        sys.exit(1)

    if not _key and not _key_path:
        click.echo(
            "错误：需要 GitHub App 私钥。\n"
            "  推荐：通过 --private-key-path 或环境变量 GITHUB_APP_PRIVATE_KEY_PATH 指定 PEM 文件路径。\n"
            "  也可通过环境变量 GITHUB_APP_PRIVATE_KEY 设置 PEM 内容（不推荐，可能触发 .env 警告）。",
            err=True,
        )
        sys.exit(1)

    # 创建 auth
    try:
        if _key:
            auth = GitHubAppAuth(app_id=_app_id, private_key=_key)
        else:
            auth = GitHubAppAuth(app_id=_app_id, private_key_path=_key_path)
    except Exception as e:
        click.echo(f"错误：无法加载私钥 — {e}", err=True)
        sys.exit(1)

    if not _secret:
        click.echo(
            "警告：未设置 webhook secret（GITHUB_WEBHOOK_SECRET）。"
            "将跳过签名验证，不推荐在生产环境使用。",
        )

    # 创建 Flask app
    app = create_app(auth, config, webhook_secret=_secret)

    click.echo()
    click.echo("=" * 55)
    click.echo("  Repoforge — Webhook Server")
    click.echo("=" * 55)
    click.echo(f"  Model:      {config.llm.provider}/{config.llm.model}")
    click.echo(f"  Max steps:  {config.agent.max_steps}")
    click.echo(f"  Listen:     http://{host}:{port}")
    click.echo(f"  Webhook:    http://{host}:{port}/webhook")
    click.echo(f"  Health:     http://{host}:{port}/")
    click.echo(f"  Dashboard:  http://{host}:{port}/dashboard/")
    click.echo(f"  Debug:      {debug}")
    click.echo("=" * 55)
    click.echo()
    click.echo("等待 GitHub 事件...")

    app.run(host=host, port=port, debug=debug)


@main.command()
@click.option("--port", "-p", type=int, default=8000, help="监听端口（默认 8000）")
@click.option("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
@click.option("--config", "-c", "config_path", default=None, help="配置 YAML 路径")
def dashboard(port: int, host: str, config_path: str | None) -> None:
    """启动独立的 Dashboard 服务（不含 webhook）。

    用于查看管线运行统计，不需要 GitHub App 认证。

    \\b
    示例：
        repoforge-pipe dashboard --port 8000
    """
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )

    from config.schema import load_config
    from pipeline.dashboard import create_dashboard_app

    config = load_config(config_path)

    app = create_dashboard_app(config)

    click.echo()
    click.echo("=" * 55)
    click.echo("  Repoforge — Dashboard")
    click.echo("=" * 55)
    click.echo(f"  Model:      {config.llm.provider}/{config.llm.model}")
    click.echo(f"  Dashboard:  http://{host}:{port}/dashboard/")
    click.echo(f"  API:        http://{host}:{port}/dashboard/api/stats")
    click.echo("=" * 55)
    click.echo()

    app.run(host=host, port=port, debug=False)


@main.command()
def setup() -> None:
    """打印 GitHub App 设置指引。

    创建 GitHub App 的步骤说明。
    """
    click.echo("""
╔══════════════════════════════════════════════════════════════╗
║         GitHub App 设置指南                                  ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Step 1: 创建 GitHub App                                     ║
║    1. 打开 https://github.com/settings/apps                  ║
║    2. 点击 "New GitHub App"                                  ║
║    3. 填写 App 名称（如 "Repoforge"）              ║
║    4. 设置 Homepage URL                                      ║
║    5. 设置 Webhook URL（需要公网可达的地址）                   ║
║       - 例如使用 ngrok: https://xxxx.ngrok.io/webhook        ║
║    6. 生成 Webhook secret（随机字符串，保存好）               ║
║                                                              ║
║  Step 2: 配置权限                                            ║
║    Repository permissions:                                   ║
║      - Contents:      Read & Write                           ║
║      - Issues:        Read & Write                           ║
║      - Pull requests: Read & Write                           ║
║      - Checks:        Read only                              ║
║                                                              ║
║  Step 3: 订阅事件                                            ║
║    Subscribe to events:                                      ║
║      - Issues                                                ║
║      - Pull request                                          ║
║      - Check run                                             ║
║                                                              ║
║  Step 4: 安装 App 到目标仓库                                  ║
║    在 App 设置页 "Install App" → 选择仓库                    ║
║                                                              ║
║  Step 5: 配置环境变量                                        ║
║    推荐方式（文件路径）：                                      ║
║      将私钥 .pem 文件放在项目目录下（如 github-app.pem），      ║
║      然后在 .env 中设置：                                     ║
║        GITHUB_APP_PRIVATE_KEY_PATH=./github-app.pem           ║
║                                                              ║
║    备用方式（不推荐，可能触发 .env 多行值警告）：               ║
║      export GITHUB_APP_PRIVATE_KEY="$(cat your-key.pem)"      ║
║                                                              ║
║  Step 6: 启动 server                                         ║
║    repoforge-pipe serve --port 8000                            ║
║                                                              ║
║  本地调试技巧：                                               ║
║    ngrok http 8000  # 得到一个公网 URL                        ║
║    然后在 GitHub App 设置中更新 Webhook URL                   ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")


@main.command()
@click.option("--issue", "-i", required=True, type=int, help="Issue 编号")
@click.option("--repo", "-r", required=True, help="仓库名（owner/repo）")
@click.option("--config", "-c", "config_path", default=None, help="配置 YAML")
def test_issue(issue: int, repo: str, config_path: str | None) -> None:
    """
    手动触发 issue 修复（调试用）。

    直接用 GitHub 个人 token 拉 issue 并运行 agent，
    不需要 GitHub App 和 webhook。

    \\b
    示例：
        repoforge-pipe test-issue -r myorg/myrepo -i 42
    """
    import os
    import subprocess
    import time

    from config.schema import load_config
    from entry.github_issue import fetch_issue

    config = load_config(config_path)
    config.llm.api_key = config.llm.api_key or os.environ.get("DEEPSEEK_API_KEY", "")

    click.echo(f"Fetching issue #{issue} from {repo} ...")
    title, body, url = fetch_issue(repo, issue)
    click.echo(f"  Title: {title}")
    click.echo(f"  URL: {url}")

    task_desc = f"Fix GitHub Issue #{issue}: {title}\n\n{body}"

    from agent.core import Agent, AgentConfig
    from agent.event_log import EventLog
    from agent.task import Task
    from entry.cli import _build_registry
    from llm.router import create_backend_from_config

    backend = create_backend_from_config({
        "provider": config.llm.provider,
        "model": config.llm.model,
        "api_key": config.llm.api_key or None,
        "base_url": config.llm.base_url or None,
        "max_tokens": config.llm.max_tokens,
    })

    repo_name = repo.split("/")[1]
    clone_dir = Path(f"./pipeline_repos/{repo_name}")

    if not (clone_dir / ".git").exists():
        token = os.environ.get("GITHUB_TOKEN", "")
        clone_url = (
            f"https://{token}@github.com/{repo}.git" if token
            else f"https://github.com/{repo}.git"
        )
        click.echo(f"Cloning {repo} ...")
        subprocess.run(
            ["git", "clone", "--depth=1", clone_url, str(clone_dir)],
            capture_output=True, text=True, timeout=300, check=True,
        )

    branch = f"agent/fix-issue-{issue}-{int(time.time())}"
    subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=str(clone_dir), capture_output=True, text=True, timeout=30,
    )

    registry = _build_registry(config)
    agent_cfg = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        stream=True,
    )
    agent = Agent(backend, registry, agent_cfg)
    task = Task(
        description=task_desc, repo_path=str(clone_dir),
        issue_url=url, max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    click.echo(f"\nRunning agent on issue #{issue} ...\n")
    t0 = time.time()
    log_dir = os.path.join(config.agent.log_dir, "pipeline")
    with EventLog.create(task, log_dir=log_dir) as log:
        result = agent.run(task, log)
    elapsed = time.time() - t0

    click.echo(f"\nStatus : {result.status.value}")
    click.echo(f"Steps  : {result.steps_taken}")
    click.echo(f"Tokens : {result.total_tokens:,}")
    click.echo(f"Time   : {elapsed:.0f}s")
    click.echo(f"Summary: {result.summary[:500]}")


@main.command()
@click.option("--repo", "-r", "repo_name", default=None, help="GitHub 仓库（owner/repo）")
@click.option("--pr", "-p", type=int, default=None, help="PR 编号")
@click.option("--local", is_flag=True, help="本地模式（对比两个分支）")
@click.option("--base", default="main", help="Base 分支（本地模式，默认 main）")
@click.option("--head", default="HEAD", help="Head 分支（本地模式，默认 HEAD）")
@click.option("--repo-dir", default=".", help="本地仓库路径（默认当前目录）")
@click.option("--config", "-c", "config_path", default=None, help="配置 YAML 路径")
@click.option("--submit", is_flag=True, help="提交 review 到 GitHub（需要 token）")
@click.option("--verbose", "-v", is_flag=True, help="详细日志")
def review(
    repo_name: str | None,
    pr: int | None,
    local: bool,
    base: str,
    head: str,
    repo_dir: str,
    config_path: str | None,
    submit: bool,
    verbose: bool,
) -> None:
    """
    运行 PR 代码审查。

    支持两种模式：
    \\b
    1. 本地模式 — 对当前仓库的两个分支/commit 做 review
       repoforge-pipe review --local --base main --head feature-x
       repoforge-pipe review --local --base HEAD~3 --head HEAD

    \\b
    2. GitHub PR 模式 — 通过 API 拉取 PR diff 做 review
       repoforge-pipe review --repo myorg/myrepo --pr 42

    \\b
    可选 --submit 将审查结果提交为 GitHub PR Review（APPROVE/REQUEST_CHANGES）。
    """
    import os

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )

    from config.schema import load_config
    from llm.router import create_backend_from_config
    from pipeline.review import run_review, submit_github_review

    config = load_config(config_path)
    backend = create_backend_from_config({
        "provider": config.llm.provider,
        "model": config.llm.model,
        "api_key": config.llm.api_key or None,
        "base_url": config.llm.base_url or None,
        "max_tokens": config.llm.max_tokens,
    })

    token = os.environ.get("GITHUB_TOKEN", "")

    if local:
        click.echo(f"Reviewing local diff: {base}..{head}")
        report = run_review(
            repo_dir=repo_dir, base=base, head=head,
            backend=backend, config=config,
        )
    elif repo_name and pr:
        click.echo(f"Reviewing PR: {repo_name}#{pr}")
        report = run_review(
            repo_full_name=repo_name, pr_number=pr,
            repo_dir=repo_dir, token=token,
            backend=backend, config=config,
        )

        if submit and token:
            url = submit_github_review(repo_name, pr, report, token)
            click.echo(f"\nReview submitted: {url}")
    else:
        click.echo("Use --local for local review, or --repo + --pr for GitHub PR review.", err=True)
        sys.exit(1)

    # 打印报告
    click.echo()
    click.echo(report.to_markdown())
    click.echo()
    click.echo(f"Findings: {report.total_count} total | "
               f"{report.critical_count} critical | {report.high_count} high")
    click.echo(f"Verdict: {'REQUEST CHANGES' if report.needs_changes() else 'COMMENT' if not report.is_clean() else 'APPROVE'}")
    click.echo(f"Steps: {report.stats.get('steps_taken', '?')} | "
               f"Tokens: {report.stats.get('tokens_used', '?')}")


if __name__ == "__main__":
    main()
