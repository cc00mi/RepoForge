"""
pipeline/handlers.py

事件处理器。每种 GitHub 事件对应一个 handler 函数。

handler 签名：
    handler(event_type: str, payload: dict, agent_config: AppConfig, auth: GitHubAppAuth) -> str

返回人类可读的结果描述字符串。
"""

from __future__ import annotations

import logging
import threading
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 后台任务执行器
# ---------------------------------------------------------------------------

_pending_tasks: list[dict] = []
_task_lock = threading.Lock()
_worker_started = False


def _run_agent_in_background(
    task_desc: str,
    repo_full_name: str,
    installation_id: int,
    clone_dir: Path,
    auth,
    config,
    *,
    branch_name: str = "agent/auto-fix",
    on_done_callback=None,
) -> None:
    """
    在后台线程中运行 agent，完成后回调。
    不阻塞 webhook 响应。
    """
    import os
    import subprocess
    import time

    from agent.core import Agent, AgentConfig
    from agent.event_log import EventLog
    from agent.task import Task
    from entry.cli import _build_registry
    from llm.router import create_backend_from_config

    def _run():
        logger.info("Background agent started: %s", task_desc[:100])
        t0 = time.time()
        try:
            # 获取 access token 用于 clone
            token = auth.get_installation_token(installation_id)

            # Clone
            clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
            if not (clone_dir / ".git").exists():
                subprocess.run(
                    ["git", "clone", "--depth=1", clone_url, str(clone_dir)],
                    capture_output=True, text=True, timeout=300, check=True,
                )
            else:
                subprocess.run(
                    ["git", "fetch", "origin"],
                    cwd=str(clone_dir), capture_output=True, text=True, timeout=120,
                )
                subprocess.run(
                    ["git", "checkout", "--force", "origin/main"],
                    cwd=str(clone_dir), capture_output=True, text=True, timeout=60,
                )
                subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=str(clone_dir), capture_output=True, text=True, timeout=30,
                )

            # 创建分支
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=str(clone_dir), capture_output=True, text=True, timeout=30,
            )

            # 组装 agent
            backend = create_backend_from_config({
                "provider": config.llm.provider,
                "model": config.llm.model,
                "api_key": config.llm.api_key or None,
                "base_url": config.llm.base_url or None,
                "max_tokens": config.llm.max_tokens,
            })
            registry = _build_registry(config)
            agent_cfg = AgentConfig(
                max_steps=config.agent.max_steps,
                budget_tokens=config.agent.budget_tokens,
                stream=False,
            )
            agent = Agent(backend, registry, agent_cfg)
            task = Task(
                description=task_desc,
                repo_path=str(clone_dir),
                issue_url=f"https://github.com/{repo_full_name}",
                max_steps=config.agent.max_steps,
                budget_tokens=config.agent.budget_tokens,
            )

            log_dir = os.path.join(config.agent.log_dir, "pipeline")
            with EventLog.create(task, log_dir=log_dir) as log:
                result = agent.run(task, log)

            elapsed = time.time() - t0
            logger.info(
                "Background agent finished: status=%s steps=%d tokens=%d elapsed=%.0fs",
                result.status.value, result.steps_taken, result.total_tokens, elapsed,
            )

            if on_done_callback:
                on_done_callback(result, elapsed)

        except Exception:
            elapsed = time.time() - t0
            logger.exception("Background agent failed after %.0fs", elapsed)
            if on_done_callback:
                on_done_callback(None, elapsed)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# 事件处理器
# ---------------------------------------------------------------------------

def handle_issues_opened(payload: dict, auth, config) -> str:
    """
    Issue 被打开 → 后台运行 agent 尝试修复。

    GitHub 会给 issue body 填充 markdown，agent 会从中提取
    任务描述并尝试在仓库中定位和修复问题。
    """
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    installation_id = payload.get("installation", {}).get("id")

    if not installation_id:
        return "No installation ID in payload"

    issue_number = issue["number"]
    issue_title = issue["title"]
    issue_body = issue.get("body", "") or ""
    repo_full_name = repo["full_name"]
    repo_name = repo["name"]

    task_desc = (
        f"Fix GitHub Issue #{issue_number}: {issue_title}\n\n{issue_body}"
    )

    clone_dir = Path("./pipeline_repos") / repo_name
    branch = f"agent/fix-issue-{issue_number}-{int(__import__('time').time())}"

    # 回复 issue 表示 agent 开始工作
    _post_issue_comment(
        auth, installation_id, repo_full_name, issue_number,
        f"Agent is analyzing this issue...\n\n"
        f"Model: `{config.llm.provider}/{config.llm.model}`\n"
        f"Max steps: {config.agent.max_steps}",
    )

    def _on_done(result, elapsed):
        if result and result.is_success():
            # 提取 diff, push 分支, 创建 PR
            import subprocess
            repo_dir = str(clone_dir)
            subprocess.run(
                ["git", "add", "-A"],
                cwd=repo_dir, capture_output=True, text=True, timeout=30,
            )
            diff_proc = subprocess.run(
                ["git", "diff", "--cached", "HEAD"],
                cwd=repo_dir, capture_output=True, text=True, timeout=60,
            )
            subprocess.run(
                ["git", "reset", "-q"],
                cwd=repo_dir, capture_output=True, text=True, timeout=30,
            )
            diff_text = diff_proc.stdout.strip()

            if diff_text:
                # 提交 + 推送
                subprocess.run(
                    ["git", "commit", "-am", f"[Agent] Fix #{issue_number}: {issue_title}"],
                    cwd=repo_dir, capture_output=True, text=True, timeout=30,
                )
                token = auth.get_installation_token(installation_id)
                push_url = (
                    f"https://x-access-token:{token}@github.com/"
                    f"{repo_full_name}.git"
                )
                subprocess.run(
                    ["git", "push", "--set-upstream", push_url, branch],
                    cwd=repo_dir, capture_output=True, text=True, timeout=120,
                )
                # 创建 PR
                _create_pull_request(
                    auth, installation_id, repo_full_name, branch,
                    f"[Agent] Fix #{issue_number}: {issue_title}",
                    (
                        f"Fixes #{issue_number}\n\n"
                        f"## Summary\n{result.summary}\n\n"
                        f"## Stats\n"
                        f"- Steps: {result.steps_taken}\n"
                        f"- Tokens: {result.total_tokens:,}\n"
                        f"- Time: {elapsed:.0f}s\n"
                        f"- Model: {config.llm.provider}/{config.llm.model}"
                    ),
                )
                _post_issue_comment(
                    auth, installation_id, repo_full_name, issue_number,
                    f"PR created with proposed fix.\n\n"
                    f"Steps: {result.steps_taken} | "
                    f"Tokens: {result.total_tokens:,} | "
                    f"Time: {elapsed:.0f}s",
                )
            else:
                _post_issue_comment(
                    auth, installation_id, repo_full_name, issue_number,
                    f"Agent analyzed the issue but did not produce code changes.\n\n"
                    f"Summary: {result.summary[:500]}",
                )
        else:
            status = result.status.value if result else "error"
            _post_issue_comment(
                auth, installation_id, repo_full_name, issue_number,
                f"Agent was unable to fix this issue.\n\n"
                f"Status: `{status}` | Time: {elapsed:.0f}s",
            )

    _run_agent_in_background(
        task_desc, repo_full_name, installation_id, clone_dir, auth, config,
        branch_name=branch, on_done_callback=_on_done,
    )

    return (
        f"Issue #{issue_number} ({repo_full_name}) — "
        f"agent dispatched in background"
    )


def handle_pull_request_opened(payload: dict, auth, config) -> str:
    """PR 被打开 → 后台运行 agent 做结构化 code review。"""
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    installation_id = payload.get("installation", {}).get("id")

    if not installation_id:
        return "No installation ID in payload"

    pr_number = pr["number"]
    pr_title = pr["title"]
    repo_full_name = repo["full_name"]
    repo_name = repo["name"]
    base_branch = pr["base"]["ref"]

    clone_dir = Path("./pipeline_repos") / repo_name
    pr_head_sha = pr["head"]["sha"]

    def _on_done(report, elapsed, _steps, _tokens):
        from pipeline.review import submit_github_review
        if report is None:
            _post_pr_comment(
                auth, installation_id, repo_full_name, pr_number,
                f"Automated review failed after {elapsed:.0f}s.",
            )
            return
        try:
            token = auth.get_installation_token(installation_id)
            review_url = submit_github_review(
                repo_full_name, pr_number, report, token,
            )
            logger.info("Review URL: %s", review_url)
        except Exception:
            logger.exception("Failed to submit review")
            _post_pr_comment(
                auth, installation_id, repo_full_name, pr_number,
                report.to_markdown(),
            )

    def _run_review():
        import time as _time
        from pipeline.review import run_review, submit_github_review
        t0 = _time.time()
        try:
            token = auth.get_installation_token(installation_id)

            # Clone repo at PR head
            clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
            if not (clone_dir / ".git").exists():
                subprocess.run(
                    ["git", "clone", "--depth=1", clone_url, str(clone_dir)],
                    capture_output=True, text=True, timeout=300, check=True,
                )
            else:
                subprocess.run(
                    ["git", "fetch", "origin"], cwd=str(clone_dir),
                    capture_output=True, text=True, timeout=120,
                )
            subprocess.run(
                ["git", "checkout", "--force", pr_head_sha],
                cwd=str(clone_dir), capture_output=True, text=True, timeout=60,
            )
            subprocess.run(
                ["git", "clean", "-fd"], cwd=str(clone_dir),
                capture_output=True, text=True, timeout=30,
            )

            # 通过 GitHub API 直接获取 diff（避免本地 git diff 复杂逻辑）
            report = run_review(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                repo_dir=str(clone_dir),
                token=token,
                config=config,
            )
            elapsed = _time.time() - t0
            _on_done(report, elapsed, report.stats.get("steps_taken", 0),
                     report.stats.get("tokens_used", 0))
        except Exception:
            elapsed = _time.time() - t0
            logger.exception("Review agent failed")
            _on_done(None, elapsed, 0, 0)

    t = threading.Thread(target=_run_review, daemon=True)
    t.start()

    return (
        f"PR #{pr_number} ({repo_full_name}) — "
        f"review agent dispatched in background"
    )


def handle_check_run_failed(payload: dict, auth, config) -> str:
    """
    CI check_run 失败 → 后台运行 agent 分析日志并尝试修复。

    需要 check_run 的 details_url 或 output 字段中有失败日志。
    """
    check_run = payload.get("check_run", {})
    repo = payload.get("repository", {})
    installation_id = payload.get("installation", {}).get("id")

    if not installation_id:
        return "No installation ID in payload"

    check_name = check_run.get("name", "unknown")
    check_output = check_run.get("output", {})
    summary = check_output.get("summary", "") if check_output else ""
    log_text = check_output.get("text", "") if check_output else ""
    repo_full_name = repo["full_name"]
    repo_name = repo["name"]

    task_desc = (
        f"Fix CI Failure: {check_name}\n\n"
        f"## CI Summary\n{summary}\n\n"
        f"## CI Log\n{log_text[:4000]}\n\n"
        f"## Instructions\n"
        f"- Analyze the CI failure logs above.\n"
        f"- Locate and fix the root cause in the codebase.\n"
        f"- Run the same checks locally to verify the fix."
    )

    clone_dir = Path("./pipeline_repos") / repo_name
    branch = f"agent/fix-ci-{check_name.replace(' ', '-')}-{int(__import__('time').time())}"

    def _on_done(result, elapsed):
        if result and result.is_success():
            import subprocess
            repo_dir = str(clone_dir)
            subprocess.run(
                ["git", "add", "-A"],
                cwd=repo_dir, capture_output=True, text=True, timeout=30,
            )
            diff_proc = subprocess.run(
                ["git", "diff", "--cached", "HEAD"],
                cwd=repo_dir, capture_output=True, text=True, timeout=60,
            )
            subprocess.run(
                ["git", "reset", "-q"],
                cwd=repo_dir, capture_output=True, text=True, timeout=30,
            )
            if diff_proc.stdout.strip():
                subprocess.run(
                    ["git", "commit", "-am", f"[Agent] Fix CI: {check_name}"],
                    cwd=repo_dir, capture_output=True, text=True, timeout=30,
                )
                token = auth.get_installation_token(installation_id)
                push_url = (
                    f"https://x-access-token:{token}@github.com/"
                    f"{repo_full_name}.git"
                )
                subprocess.run(
                    ["git", "push", "--set-upstream", push_url, branch],
                    cwd=repo_dir, capture_output=True, text=True, timeout=120,
                )
                _create_pull_request(
                    auth, installation_id, repo_full_name, branch,
                    f"[Agent] Fix CI: {check_name}",
                    (
                        f"Auto-fix for CI failure `{check_name}`\n\n"
                        f"## Summary\n{result.summary}\n\n"
                        f"## Stats\n"
                        f"- Steps: {result.steps_taken}\n"
                        f"- Tokens: {result.total_tokens:,}\n"
                        f"- Time: {elapsed:.0f}s\n"
                        f"- Model: {config.llm.provider}/{config.llm.model}"
                    ),
                )

    _run_agent_in_background(
        task_desc, repo_full_name, installation_id, clone_dir, auth, config,
        branch_name=branch, on_done_callback=_on_done,
    )

    return (
        f"Check run '{check_name}' ({repo_full_name}) — "
        f"debug agent dispatched in background"
    )


# ---------------------------------------------------------------------------
# GitHub API 辅助
# ---------------------------------------------------------------------------

def _post_issue_comment(
    auth, installation_id: int, repo_full_name: str,
    issue_number: int, body: str,
) -> None:
    """在 issue 下发评论。"""
    try:
        gh = auth.get_github_client(installation_id)
        repo = gh.get_repo(repo_full_name)
        issue = repo.get_issue(issue_number)
        issue.create_comment(body)
        logger.info("Comment posted on %s#%d", repo_full_name, issue_number)
    except Exception:
        logger.exception("Failed to post issue comment")


def _post_pr_comment(
    auth, installation_id: int, repo_full_name: str,
    pr_number: int, body: str,
) -> None:
    """在 PR 下发评论。"""
    try:
        gh = auth.get_github_client(installation_id)
        repo = gh.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        pr.create_issue_comment(body)
        logger.info("Comment posted on %s#%d", repo_full_name, pr_number)
    except Exception:
        logger.exception("Failed to post PR comment")


def _create_pull_request(
    auth, installation_id: int, repo_full_name: str,
    head_branch: str, title: str, body: str, base: str = "main",
) -> str | None:
    """创建 PR，返回 URL。失败返回 None。"""
    try:
        gh = auth.get_github_client(installation_id)
        repo = gh.get_repo(repo_full_name)
        # 如果 main 不存在，尝试 master
        try:
            repo.get_branch(base)
        except Exception:
            base = "master"
        pr = repo.create_pull(title=title, body=body, head=head_branch, base=base)
        logger.info("PR created: %s", pr.html_url)
        return pr.html_url
    except Exception:
        logger.exception("Failed to create PR")
        return None
