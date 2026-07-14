"""
pipeline/handlers.py

事件处理器。每种 GitHub 事件对应一个 handler 函数。

handler 签名：
    handler(event_type: str, payload: dict, agent_config: AppConfig, auth: GitHubAppAuth) -> str

返回人类可读的结果描述字符串。
"""

from __future__ import annotations

import logging
import subprocess
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
            # Load Repo Memory (with task-aware matching for few-shot + hotspots)
            from memory.repo_memory import memory_service
            repo_memory = memory_service.load(repo_full_name)
            repo_memory_text = memory_service.render_for_prompt(
                repo_memory, task_description=task_desc)

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
                result = agent.run(task, log, repo_memory_text=repo_memory_text)

            # Record outcome to Repo Memory (best-effort)
            try:
                memory_service.record_outcome(
                    repo_memory,
                    issue_ref=f"{repo_full_name}#{task_desc[:60]}",
                    issue_title=task_desc[:100],
                    changed_files=result.changed_files,
                    patch_produced=bool(result.patch),
                    validation_summary=result.summary or "",
                )
                memory_service.save(repo_memory)
            except Exception:
                logger.debug("Failed to record memory outcome", exc_info=True)

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
    Issue 被打开 → 运行 triage 分诊，然后根据可行性决定是否派 agent 修复。

    流程：
    1. 分类（bug/enhancement/docs/question）
    2. 去重检查（对比 RepoMemory.recent_issues）
    3. 可行性门控（auto_fix / needs_triage / escalate）
    4. 添加标签 + 发布分诊评论
    5. 如果 auto_fix → 后台派 agent
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

    # ---- Run triage ----------------------------------------------------------
    from pipeline.triage import TriageEngine

    engine = TriageEngine()
    triage = engine.classify(issue_title, issue_body)

    # Load RepoMemory for dedup
    from memory.repo_memory import memory_service
    repo_memory = memory_service.load(repo_full_name)
    duplicates = engine.check_duplicates(issue_title, issue_body, repo_memory.recent_issues)

    decision, reason = engine.assess_solvability(issue_title, issue_body)

    # ---- Apply labels --------------------------------------------------------
    labels_to_add = list(triage.labels)
    if decision == "escalate":
        labels_to_add.append("needs-human")
    elif decision == "needs_triage":
        labels_to_add.append("needs-triage")
    _add_issue_labels(auth, installation_id, repo_full_name, issue_number, labels_to_add)

    # ---- Handle duplicates ---------------------------------------------------
    high_dup = next((d for d in duplicates if d.score > 0.90), None)
    if high_dup:
        dup_comment = (
            f"## Duplicate Detected\n\n"
            f"This issue appears to be a duplicate of **{high_dup.reference}** "
            f"(similarity: {high_dup.score:.0%}).\n\n"
            f"> {high_dup.title}\n\n"
            f"Closing as duplicate. If this is incorrect, please comment and a "
            f"maintainer will reopen."
        )
        try:
            gh = auth.get_github_client(installation_id)
            repo_obj = gh.get_repo(repo_full_name)
            iss = repo_obj.get_issue(issue_number)
            iss.create_comment(dup_comment)
            iss.edit(state="closed")
        except Exception:
            logger.debug("Failed to close duplicate issue", exc_info=True)
        return (
            f"Issue #{issue_number} ({repo_full_name}) — "
            f"closed as duplicate of {high_dup.reference}"
        )

    # ---- Post triage comment -------------------------------------------------
    triage_comment = engine.generate_triage_comment(triage, duplicates, decision, reason)
    _post_issue_comment(auth, installation_id, repo_full_name, issue_number, triage_comment)

    # ---- Decision gate -------------------------------------------------------
    if decision == "escalate":
        return (
            f"Issue #{issue_number} ({repo_full_name}) — "
            f"escalated (requires human expertise): {reason[:100]}"
        )

    if decision == "needs_triage":
        return (
            f"Issue #{issue_number} ({repo_full_name}) — "
            f"needs triage: {reason[:100]}"
        )

    # decision == "auto_fix" → dispatch agent
    task_desc = (
        f"Fix GitHub Issue #{issue_number}: {issue_title}\n\n{issue_body}"
    )

    clone_dir = Path("./pipeline_repos") / repo_name
    branch = f"agent/fix-issue-{issue_number}-{int(__import__('time').time())}"

    _post_issue_comment(
        auth, installation_id, repo_full_name, issue_number,
        f"Agent is working on this issue...\n\n"
        f"Model: `{config.llm.provider}/{config.llm.model}`\n"
        f"Max steps: {config.agent.max_steps}",
    )

    def _on_done(result, elapsed):
        _publish_agent_result(
            clone_dir=clone_dir, auth=auth, installation_id=installation_id,
            repo_full_name=repo_full_name, branch=branch,
            commit_message=f"[Agent] Fix #{issue_number}: {issue_title}",
            pr_title=f"[Agent] Fix #{issue_number}: {issue_title}",
            pr_body=(
                f"Fixes #{issue_number}\n\n"
                f"## Summary\n{result.summary if result else ''}\n\n"
                f"## Stats\n"
                f"- Steps: {result.steps_taken if result else 0}\n"
                f"- Tokens: {result.total_tokens if result else 0:,}\n"
                f"- Time: {elapsed:.0f}s\n"
                f"- Model: {config.llm.provider}/{config.llm.model}"
            ),
            result=result, elapsed=elapsed, issue_number=issue_number,
        )

    _run_agent_in_background(
        task_desc, repo_full_name, installation_id, clone_dir, auth, config,
        branch_name=branch, on_done_callback=_on_done,
    )

    return (
        f"Issue #{issue_number} ({repo_full_name}) — "
        f"triage: {triage.classification}/{triage.priority}, "
        f"decision: {decision}"
    )


def handle_issue_labeled(payload: dict, auth, config) -> str:
    """Issue 被标记 → 检查是否有 'agent-fix' 标签，有则触发修复管线。"""
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    installation_id = payload.get("installation", {}).get("id")
    label = payload.get("label", {})

    if not installation_id:
        return "No installation ID in payload"

    label_name = (label.get("name", "") if label else "").lower()
    if label_name not in ("agent-fix", "agent fix", "agent_fix"):
        return f"Ignored: label '{label_name}' is not agent-fix"

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

    _post_issue_comment(
        auth, installation_id, repo_full_name, issue_number,
        f"agent-fix label detected. Dispatching agent...\n\n"
        f"Model: `{config.llm.provider}/{config.llm.model}`\n"
        f"Max steps: {config.agent.max_steps}",
    )

    def _on_done(result, elapsed):
        _publish_agent_result(
            clone_dir=clone_dir, auth=auth, installation_id=installation_id,
            repo_full_name=repo_full_name, branch=branch,
            commit_message=f"[Agent] Fix #{issue_number}: {issue_title}",
            pr_title=f"[Agent] Fix #{issue_number}: {issue_title}",
            pr_body=(
                f"Fixes #{issue_number}\n\n"
                f"## Summary\n{result.summary if result else ''}\n\n"
                f"## Stats\n"
                f"- Steps: {result.steps_taken if result else 0}\n"
                f"- Tokens: {result.total_tokens if result else 0:,}\n"
                f"- Time: {elapsed:.0f}s\n"
                f"- Model: {config.llm.provider}/{config.llm.model}"
            ),
            result=result, elapsed=elapsed, issue_number=issue_number,
        )

    _run_agent_in_background(
        task_desc, repo_full_name, installation_id, clone_dir, auth, config,
        branch_name=branch, on_done_callback=_on_done,
    )

    return (
        f"Issue #{issue_number} ({repo_full_name}) — "
        f"agent-fix label detected, agent dispatched"
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

    def _on_done(report, elapsed, _steps, _tokens, diff_text=""):
        from pipeline.review import submit_github_review, classify_pr
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

        # Save ReviewMemory with first snapshot
        try:
            from pipeline.review_memory import (
                load_review_memory, save_review_memory, record_review_snapshot,
            )
            rev_mem = load_review_memory(repo_full_name, pr_number)
            record_review_snapshot(
                rev_mem,
                head_sha=pr_head_sha,
                critical_count=report.critical_count,
                high_count=report.high_count,
                total_count=report.total_count,
                summary=report.summary,
            )
            save_review_memory(rev_mem)
        except Exception:
            logger.debug("Failed to save review memory", exc_info=True)

        # Update repo memory with review hotspots
        try:
            from memory.repo_memory import memory_service
            repo_mem = memory_service.load(repo_full_name)
            memory_service.update_review_signals(repo_mem, report.findings)
            memory_service.save(repo_mem)
        except Exception:
            logger.debug("Failed to update repo memory review signals", exc_info=True)

        # Record agent findings for recall measurement
        try:
            from pipeline.metrics import AgentFindingsRecord, FindingStore
            findings_list = [
                {"severity": f.severity, "file_path": f.file_path,
                 "line": getattr(f, "line", 0), "message": f.message}
                for f in (report.findings or [])
            ]
            FindingStore.record_agent_findings(AgentFindingsRecord(
                pr_number=pr_number,
                repo_full_name=repo_full_name,
                head_sha=pr_head_sha,
                critical_count=report.critical_count,
                high_count=report.high_count,
                total_count=report.total_count,
                findings=findings_list,
            ))
        except Exception:
            logger.debug("Failed to record agent findings", exc_info=True)

        # Classify PR and add labels
        try:
            cls = classify_pr(diff_text, pr_title, report)
            _add_pr_labels(auth, installation_id, repo_full_name, pr_number, [
                f"size:{cls['size']}", f"risk:{cls['risk']}", cls['type'],
            ])
        except Exception:
            logger.debug("Failed to classify/label PR", exc_info=True)

    def _run_review():
        import time as _time
        from pipeline.review import run_review, get_pr_diff_github
        from pipeline.review_memory import (
            load_review_memory, previous_critical_count, previous_high_count,
        )
        t0 = _time.time()
        review_memory_ctx = ""
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

            # Get diff text for classification
            diff_text = get_pr_diff_github(repo_full_name, pr_number, token)

            # Load ReviewMemory context (for incremental reviewers, not opened)
            # On first review, there's no prior context — but load anyway for consistency
            try:
                rev_mem = load_review_memory(repo_full_name, pr_number)
                if rev_mem.review_count > 0:
                    prev_crit = previous_critical_count(rev_mem)
                    prev_high = previous_high_count(rev_mem)
                    last_snapshot = rev_mem.snapshots[-1] if rev_mem.snapshots else None
                    review_memory_ctx = (
                        f"Previous review round (commit {rev_mem.last_review_commit[:8]}): "
                        f"found {prev_crit} critical, {prev_high} high issues."
                    )
                    if last_snapshot and last_snapshot.summary:
                        review_memory_ctx += f"\nPrevious summary: {last_snapshot.summary[:300]}"
            except Exception:
                pass

            report = run_review(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                repo_dir=str(clone_dir),
                token=token,
                config=config,
                review_memory_context=review_memory_ctx,
            )
            elapsed = _time.time() - t0
            _on_done(report, elapsed, report.stats.get("steps_taken", 0),
                     report.stats.get("tokens_used", 0), diff_text)
        except Exception:
            elapsed = _time.time() - t0
            logger.exception("Review agent failed")
            _on_done(None, elapsed, 0, 0, "")

    t = threading.Thread(target=_run_review, daemon=True)
    t.start()

    return (
        f"PR #{pr_number} ({repo_full_name}) — "
        f"review agent dispatched in background"
    )


def handle_pull_request_synchronize(payload: dict, auth, config) -> str:
    """PR 有新 commit → 增量 review，只审查变更文件，检查上次 findings 是否修复。"""
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    installation_id = payload.get("installation", {}).get("id")

    if not installation_id:
        return "No installation ID in payload"

    pr_number = pr["number"]
    pr_title = pr["title"]
    repo_full_name = repo["full_name"]
    repo_name = repo["name"]
    pr_head_sha = pr["head"]["sha"]

    clone_dir = Path("./pipeline_repos") / repo_name

    load_review_memory_fn = None
    save_review_memory_fn = None
    record_review_snapshot_fn = None
    mark_previous_findings_addressed_fn = None
    previous_critical_count_fn = None
    previous_high_count_fn = None

    def _on_done(report, elapsed, _steps, _tokens):
        nonlocal load_review_memory_fn, save_review_memory_fn, record_review_snapshot_fn
        nonlocal mark_previous_findings_addressed_fn, previous_critical_count_fn, previous_high_count_fn
        from pipeline.review import submit_github_review

        if report is None:
            _post_pr_comment(
                auth, installation_id, repo_full_name, pr_number,
                f"Incremental review failed after {elapsed:.0f}s.",
            )
            return

        # Submit incremental review
        try:
            token = auth.get_installation_token(installation_id)
            review_url = submit_github_review(
                repo_full_name, pr_number, report, token,
            )
            logger.info("Incremental review URL: %s", review_url)
        except Exception:
            logger.exception("Failed to submit incremental review")
            _post_pr_comment(
                auth, installation_id, repo_full_name, pr_number,
                report.to_markdown(),
            )

        # Update ReviewMemory
        try:
            rev_mem = load_review_memory_fn(repo_full_name, pr_number)
            prev_crit = previous_critical_count_fn(rev_mem)
            prev_high = previous_high_count_fn(rev_mem)
            if prev_crit == 0 and prev_high == 0:
                mark_previous_findings_addressed_fn(rev_mem)
            record_review_snapshot_fn(
                rev_mem,
                head_sha=pr_head_sha,
                critical_count=report.critical_count,
                high_count=report.high_count,
                total_count=report.total_count,
                summary=report.summary,
            )
            save_review_memory_fn(rev_mem)
        except Exception:
            logger.debug("Failed to update review memory", exc_info=True)

        # Update repo memory review hotspots
        try:
            from memory.repo_memory import memory_service
            repo_mem = memory_service.load(repo_full_name)
            memory_service.update_review_signals(repo_mem, report.findings)
            memory_service.save(repo_mem)
        except Exception:
            logger.debug("Failed to update repo memory review signals", exc_info=True)

        # Record agent findings for recall measurement
        try:
            from pipeline.metrics import AgentFindingsRecord, FindingStore
            findings_list = [
                {"severity": f.severity, "file_path": f.file_path,
                 "line": getattr(f, "line", 0), "message": f.message}
                for f in (report.findings or [])
            ]
            FindingStore.record_agent_findings(AgentFindingsRecord(
                pr_number=pr_number,
                repo_full_name=repo_full_name,
                head_sha=pr_head_sha,
                critical_count=report.critical_count,
                high_count=report.high_count,
                total_count=report.total_count,
                findings=findings_list,
            ))
        except Exception:
            logger.debug("Failed to record agent findings", exc_info=True)

    def _run_review():
        import time as _time
        from pipeline.review import (
            run_review, get_pr_diff_github, get_pr_files_github,
        )
        from pipeline.review_memory import (
            load_review_memory as lrm,
            save_review_memory as srm,
            record_review_snapshot as rrs,
            mark_previous_findings_addressed as mpfa,
            previous_critical_count as pcc,
            previous_high_count as phc,
        )

        nonlocal load_review_memory_fn, save_review_memory_fn, record_review_snapshot_fn
        nonlocal mark_previous_findings_addressed_fn, previous_critical_count_fn, previous_high_count_fn
        load_review_memory_fn = lrm
        save_review_memory_fn = srm
        record_review_snapshot_fn = rrs
        mark_previous_findings_addressed_fn = mpfa
        previous_critical_count_fn = pcc
        previous_high_count_fn = phc

        t0 = _time.time()
        try:
            token = auth.get_installation_token(installation_id)

            # Clone/fetch repo
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

            # Load previous review memory for context
            rev_mem = lrm(repo_full_name, pr_number)
            prev_crit = pcc(rev_mem)
            prev_high = phc(rev_mem)

            # Build review memory context for the agent
            review_memory_ctx = ""
            try:
                if rev_mem.review_count > 0:
                    last_snap = rev_mem.snapshots[-1] if rev_mem.snapshots else None
                    review_memory_ctx = (
                        f"Previous review (round {rev_mem.review_count}, "
                        f"commit {rev_mem.last_review_commit[:8]}): "
                        f"found {prev_crit} critical, {prev_high} high, "
                    )
                    if last_snap:
                        review_memory_ctx += (
                            f"{last_snap.medium_count} medium, "
                            f"{last_snap.total_count} total issues."
                        )
                        if last_snap.summary:
                            review_memory_ctx += (
                                f"\nPrevious summary: {last_snap.summary[:300]}"
                            )
                    review_memory_ctx += (
                        "\nVerify whether the CRITICAL and HIGH issues from "
                        "that review have been addressed in this new commit."
                    )
            except Exception:
                pass

            # Run review (full diff on new commit; review_memory tracks what we saw)
            report = run_review(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                repo_dir=str(clone_dir),
                token=token,
                config=config,
                review_memory_context=review_memory_ctx,
            )

            # If previous critical/high findings appear fixed, note it
            if prev_crit > 0 or prev_high > 0:
                note = (f"Previous review found {prev_crit} critical, {prev_high} high. "
                        f"This review: {report.critical_count} critical, {report.high_count} high.")
                if report.summary:
                    report.summary = note + " " + report.summary
                else:
                    report.summary = note

            elapsed = _time.time() - t0
            _on_done(report, elapsed, report.stats.get("steps_taken", 0),
                     report.stats.get("tokens_used", 0))
        except Exception:
            elapsed = _time.time() - t0
            logger.exception("Incremental review agent failed")
            _on_done(None, elapsed, 0, 0)

    t = threading.Thread(target=_run_review, daemon=True)
    t.start()

    return (
        f"PR #{pr_number} synchronize ({repo_full_name}) — "
        f"incremental review dispatched"
    )


def handle_pull_request_review_submitted(payload: dict, auth, config) -> str:
    """
    Maintainer submitted a PR review → if REQUEST_CHANGES, analyze comments
    and attempt to auto-address the requested changes.

    HIGH RISK: this handler pushes new commits to the PR branch. The agent
    is scoped to address only minor/specific review comments — it will not
    attempt large architectural changes.
    """
    review = payload.get("review", {})
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    installation_id = payload.get("installation", {}).get("id")
    sender = payload.get("sender", {}).get("login", "")

    if not installation_id:
        return "No installation ID in payload"

    review_state = review.get("state", "")
    if review_state != "changes_requested":
        return f"Ignored: review state is '{review_state}' (not changes_requested)"

    review_body = review.get("body", "") or ""
    pr_number = pr["number"]
    pr_title = pr["title"]
    repo_full_name = repo["full_name"]
    repo_name = repo["name"]
    pr_head_sha = pr["head"]["sha"]
    pr_head_ref = pr["head"]["ref"]
    clone_dir = Path("./pipeline_repos") / repo_name

    # Fetch inline review comments
    review_comments = _fetch_review_comments(
        auth, installation_id, repo_full_name, pr_number,
    )

    if not review_body.strip() and not review_comments:
        return f"PR #{pr_number}: REQUEST_CHANGES from @{sender} but no actionable comments to parse"

    # Record human findings for recall measurement
    try:
        from pipeline.metrics import FindingStore, HumanFindingsRecord, _extract_human_findings
        human_findings = _extract_human_findings(review_body, review_comments[:15])
        FindingStore.record_human_findings(HumanFindingsRecord(
            pr_number=pr_number,
            repo_full_name=repo_full_name,
            reviewer=sender,
            review_state=review_state,
            critical_count=sum(1 for f in human_findings if f["severity"] == "CRITICAL"),
            high_count=sum(1 for f in human_findings if f["severity"] == "HIGH"),
            total_comments=len(review_comments),
            findings=human_findings,
        ))
    except Exception:
        logger.debug("Failed to record human findings", exc_info=True)

    # Build task description from review comments
    comments_text = ""
    for i, c in enumerate(review_comments[:15], 1):
        path = c.get("path", "?")
        body = c.get("body", "")
        line = c.get("line", c.get("original_line", "?"))
        comments_text += (
            f"  {i}. `{path}:{line}` — {body[:300]}"
            + ("..." if len(body) > 300 else "")
            + "\n"
        )

    task_desc = (
        f"Address review feedback on PR #{pr_number}: {pr_title}\n\n"
        f"## Reviewer: @{sender}\n\n"
    )
    if review_body.strip():
        task_desc += f"## Review Summary\n{review_body[:2000]}\n\n"
    if comments_text:
        task_desc += f"## Inline Comments\n{comments_text}\n"

    task_desc += (
        f"## Instructions\n"
        f"1. Read each review comment carefully.\n"
        f"2. Make the requested changes — focus on concrete, specific feedback.\n"
        f"3. Run tests to verify the changes don't break anything.\n"
        f"4. Do NOT attempt large refactors or architectural changes — only "
        f"address the specific issues raised in the review.\n"
        f"5. If a comment is unclear or requires design discussion, skip it "
        f"and note it in your summary.\n"
    )

    branch = f"agent/address-review-{pr_number}-{int(__import__('time').time())}"

    def _on_done(result, elapsed):
        if not (result and result.is_success()):
            _post_pr_comment(
                auth, installation_id, repo_full_name, pr_number,
                f"Attempted to auto-address review feedback from @{sender} "
                f"but was unable to complete all changes.\n\n"
                f"Status: {result.status.value if result else 'error'} | "
                f"Time: {elapsed:.0f}s\n\n"
                f"Summary: {result.summary if result else 'N/A'}",
            )
            return

        pr_url = _publish_agent_result(
            clone_dir=clone_dir, auth=auth, installation_id=installation_id,
            repo_full_name=repo_full_name, branch=branch,
            commit_message=f"[Agent] Address review feedback on PR #{pr_number}",
            pr_title=f"[Agent] Address review: PR #{pr_number}",
            pr_body=(
                f"Auto-addressing review feedback from @{sender} on #{pr_number}\n\n"
                f"## Changes\n{result.summary if result else ''}\n\n"
                f"## Stats\n"
                f"- Steps: {result.steps_taken}\n"
                f"- Tokens: {result.total_tokens:,}\n"
                f"- Time: {elapsed:.0f}s"
            ),
            result=result, elapsed=elapsed,
        )
        if pr_url:
            _post_pr_comment(
                auth, installation_id, repo_full_name, pr_number,
                f"Addressed review feedback from @{sender} in {pr_url}",
            )

    # Post acknowledgement
    _post_pr_comment(
        auth, installation_id, repo_full_name, pr_number,
        f"Analyzing review feedback from @{sender}...\n\n"
        f"Model: `{config.llm.provider}/{config.llm.model}`\n"
        f"Max steps: {config.agent.max_steps}",
    )

    _run_agent_in_background(
        task_desc, repo_full_name, installation_id, clone_dir, auth, config,
        branch_name=branch, on_done_callback=_on_done,
    )

    return (
        f"PR #{pr_number} ({repo_full_name}) — "
        f"REVIEW_REQUESTED_CHANGES from @{sender}, agent dispatched "
        f"({len(review_comments)} inline comments)"
    )


def _fetch_review_comments(
    auth, installation_id: int, repo_full_name: str, pr_number: int,
) -> list[dict]:
    """Fetch inline review comments for a PR.

    Returns list of {path, body, line, original_line, user} dicts.
    """
    try:
        gh = auth.get_github_client(installation_id)
        repo = gh.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        comments = pr.get_review_comments()
        return [
            {
                "path": c.path or "",
                "body": c.body or "",
                "line": c.position or c.original_position or 0,
                "original_line": c.original_position or 0,
                "user": c.user.login if c.user else "",
            }
            for c in comments
        ]
    except Exception:
        logger.debug("Failed to fetch review comments", exc_info=True)
        return []


def handle_check_run_failed(payload: dict, auth, config) -> str:
    """CI check_run failure → dispatch agent to analyze logs and attempt fix.

    Requires check_run details_url or output field with failure logs.
    """
    import time as _time

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
    branch = f"agent/fix-ci-{check_name.replace(' ', '-')}-{int(_time.time())}"

    def _on_done(result, elapsed):
        _publish_agent_result(
            clone_dir=clone_dir, auth=auth, installation_id=installation_id,
            repo_full_name=repo_full_name, branch=branch,
            commit_message=f"[Agent] Fix CI: {check_name}",
            pr_title=f"[Agent] Fix CI: {check_name}",
            pr_body=(
                f"Auto-fix for CI failure `{check_name}`\n\n"
                f"## Summary\n{result.summary if result else ''}\n\n"
                f"## Stats\n"
                f"- Steps: {result.steps_taken if result else 0}\n"
                f"- Tokens: {result.total_tokens if result else 0:,}\n"
                f"- Time: {elapsed:.0f}s\n"
                f"- Model: {config.llm.provider}/{config.llm.model}"
            ),
            result=result, elapsed=elapsed,
        )

    _run_agent_in_background(
        task_desc, repo_full_name, installation_id, clone_dir, auth, config,
        branch_name=branch, on_done_callback=_on_done,
    )

    return (
        f"Check run '{check_name}' ({repo_full_name}) — "
        f"debug agent dispatched in background"
    )


def handle_issues_closed(payload: dict, auth, config) -> str:
    """Issue 关闭 → 更新 RepoMemory 中对应 issue outcome 的状态。

    纯数据操作，无 LLM 调用。
    """
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "")
    issue_number = issue.get("number", 0)
    issue_title = issue.get("title", "")
    state_reason = payload.get("state_reason", "")

    try:
        from memory.repo_memory import memory_service
        memory = memory_service.load(repo_full_name)
        ref = f"{repo_full_name}#{issue_number}"
        for ri in memory.recent_issues:
            if ri.reference == ref:
                if state_reason == "completed":
                    ri.status = "published"
                elif state_reason == "not_planned":
                    ri.status = "failed"
                else:
                    ri.status = "closed"
                memory_service.save(memory)
                return (
                    f"Issue #{issue_number} ({repo_full_name}) closed — "
                    f"RepoMemory outcome updated to '{ri.status}'"
                )
    except Exception:
        logger.debug("Failed to update RepoMemory on issue close", exc_info=True)

    return f"Issue #{issue_number} ({repo_full_name}) closed — no matching memory entry"


def handle_issue_comment_created(payload: dict, auth, config) -> str:
    """Issue/PR 评论 → 检查是否包含 /agent-fix 或 /agent-review 命令。

    纯规则匹配，无 LLM 调用。
    """
    comment = payload.get("comment", {})
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    installation_id = payload.get("installation", {}).get("id")
    sender = payload.get("sender", {}).get("login", "")

    if not installation_id:
        return "No installation ID in payload"

    comment_body = (comment.get("body", "") or "").strip()
    if not comment_body:
        return "Empty comment — ignored"

    # Check for slash commands
    body_lower = comment_body.lower()
    is_pr = "pull_request" in issue

    if "/agent-fix" in body_lower and not is_pr:
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

        _post_issue_comment(
            auth, installation_id, repo_full_name, issue_number,
            f"`/agent-fix` command received from @{sender}. Dispatching agent...\n\n"
            f"Model: `{config.llm.provider}/{config.llm.model}`",
        )

        def _on_done(result, elapsed):
            _publish_agent_result(
                clone_dir=clone_dir, auth=auth, installation_id=installation_id,
                repo_full_name=repo_full_name, branch=branch,
                commit_message=f"[Agent] Fix #{issue_number}: {issue_title}",
                pr_title=f"[Agent] Fix #{issue_number}: {issue_title}",
                pr_body=(
                    f"Triggered by @{sender}'s `/agent-fix` command on #{issue_number}\n\n"
                    f"## Summary\n{result.summary if result else ''}\n\n"
                    f"## Stats\n"
                    f"- Steps: {result.steps_taken if result else 0}\n"
                    f"- Tokens: {result.total_tokens if result else 0:,}\n"
                    f"- Time: {elapsed:.0f}s"
                ),
                result=result, elapsed=elapsed, issue_number=issue_number,
            )

        _run_agent_in_background(
            task_desc, repo_full_name, installation_id, clone_dir, auth, config,
            branch_name=branch, on_done_callback=_on_done,
        )
        return (
            f"Issue #{issue_number} ({repo_full_name}) — "
            f"/agent-fix from @{sender}, agent dispatched"
        )

    elif "/agent-review" in body_lower and is_pr:
        # Equivalent to requesting a review through comments — just acknowledge
        pr_number = issue["number"]
        repo_full_name = repo["full_name"]
        _post_pr_comment(
            auth, installation_id, repo_full_name, pr_number,
            f"`/agent-review` command received from @{sender}. "
            f"A review will be triggered on the next PR event.",
        )
        return f"PR #{pr_number} ({repo_full_name}) — /agent-review from @{sender}"

    return f"Ignored: no actionable command in comment from @{sender}"


def handle_pr_closed(payload: dict, auth, config) -> str:
    """PR 关闭（合并或未合并）→ 更新 RepoMemory outcome。

    如果是 merged，将相关 issue outcome 标记为 published。
    纯数据操作，无 LLM 调用。
    """
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "")
    pr_number = pr.get("number", 0)
    merged = pr.get("merged", False)

    if not merged:
        return f"PR #{pr_number} ({repo_full_name}) closed without merge — no memory update"

    try:
        from memory.repo_memory import memory_service
        memory = memory_service.load(repo_full_name)
        # Find the associated issue outcome
        for ri in memory.recent_issues:
            if ri.pr_url and str(pr_number) in ri.pr_url:
                ri.status = "published"
                break
        # Also update review_hotspots — merged PR validates the signals
        changed_files = []
        for f in pr.get("requested_reviewers", []):
            pass  # PR object doesn't have file list in webhook payload
        memory_service.save(memory)
        return (
            f"PR #{pr_number} ({repo_full_name}) merged — "
            f"RepoMemory updated"
        )
    except Exception:
        logger.debug("Failed to update RepoMemory on PR merge", exc_info=True)
        return f"PR #{pr_number} ({repo_full_name}) merged — memory update failed"


def handle_pr_labeled(payload: dict, auth, config) -> str:
    """PR 被添加标签 → 检查 auto-merge 条件是否满足。

    只在添加 'auto-merge' 标签时触发评估。
    纯条件检查，无 LLM 调用。
    """
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    installation_id = payload.get("installation", {}).get("id")
    label = payload.get("label", {})

    if not installation_id:
        return "No installation ID in payload"

    label_name = (label.get("name", "") if label else "").lower()
    if label_name != "auto-merge":
        return f"Ignored: label '{label_name}' is not auto-merge"

    pr_number = pr["number"]
    repo_full_name = repo["full_name"]
    pr_title = pr["title"]
    additions = pr.get("additions", 0)
    deletions = pr.get("deletions", 0)
    total_size = additions + deletions

    # Check basic conditions
    issues = []
    if total_size > 500:
        issues.append(f"PR too large ({total_size} lines > 500 limit)")

    # Post assessment comment
    if issues:
        _post_pr_comment(
            auth, installation_id, repo_full_name, pr_number,
            f"## Auto-Merge Assessment (triggered by @{payload.get('sender', {}).get('login', '?')})\n\n"
            f"Auto-merge is **NOT** eligible:\n"
            + "\n".join(f"- {i}" for i in issues)
            + f"\n\nPlease address these before auto-merge can proceed.",
        )
        return (
            f"PR #{pr_number} ({repo_full_name}) — "
            f"auto-merge label added but not eligible: {'; '.join(issues)}"
        )

    # Basic checks passed, run full eligibility check
    from pipeline.auto_merge import AutoMergePolicy, _run_auto_merge_check

    try:
        token = auth.get_installation_token(installation_id)
        eligible, reason = _run_auto_merge_check(
            auth, installation_id, repo_full_name, pr_number, token,
        )
    except Exception:
        eligible, reason = False, "Failed to run full eligibility check"

    if eligible:
        _post_pr_comment(
            auth, installation_id, repo_full_name, pr_number,
            f"## Auto-Merge Assessment\n\n"
            f"All conditions satisfied! This PR is eligible for auto-merge.\n\n"
            f"A maintainer should confirm before merging.",
        )
    else:
        _post_pr_comment(
            auth, installation_id, repo_full_name, pr_number,
            f"## Auto-Merge Assessment\n\n"
            f"Auto-merge is **NOT** eligible:\n"
            f"- {reason}\n\n"
            f"Address these issues and re-add the `auto-merge` label.",
        )

    return (
        f"PR #{pr_number} ({repo_full_name}) — "
        f"auto-merge check: {'eligible' if eligible else 'not eligible'}"
    )


def handle_push_tag(payload: dict, auth, config) -> str:
    """Push with tag ref → generate release notes.

    Triggered on refs/tags/v* pushes. Fetches merged PRs between the
    previous tag and the new tag, generates structured release notes.
    """
    ref = payload.get("ref", "")
    if not ref.startswith("refs/tags/v"):
        return f"Ignored: ref '{ref}' is not a version tag"

    tag_name = ref.replace("refs/tags/", "")
    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "")
    installation_id = payload.get("installation", {}).get("id")
    default_branch = repo.get("default_branch", "main")

    if not installation_id:
        return "No installation ID in payload"

    try:
        from pipeline.release_notes import (
            ReleaseNotesGenerator,
            fetch_merged_prs_between_tags,
        )

        # Find previous tag
        gh = auth.get_github_client(installation_id)
        repo_obj = gh.get_repo(repo_full_name)
        tags = list(repo_obj.get_tags())
        tag_names = [t.name for t in tags if t.name.startswith("v")]
        from_tag = tag_names[1] if len(tag_names) > 1 else default_branch

        # Fetch PRs between tags
        merged_prs = fetch_merged_prs_between_tags(
            auth, installation_id, repo_full_name, from_tag, tag_name,
        )

        generator = ReleaseNotesGenerator()
        notes = generator.generate(repo_full_name, merged_prs, from_tag, tag_name)
        rendered = generator.render(notes)

        # Post as release or comment
        try:
            repo_obj.create_git_release(
                tag=tag_name,
                name=f"Release {tag_name}",
                message=rendered,
                draft=False,
                prerelease="rc" in tag_name or "alpha" in tag_name or "beta" in tag_name,
            )
            return (
                f"Release {tag_name} ({repo_full_name}) — "
                f"generated from {len(notes.entries)} PRs ({from_tag}...{tag_name})"
            )
        except Exception:
            logger.exception("Failed to create release, skipping")
            return f"Release {tag_name} — failed: see logs"

    except Exception:
        logger.exception("Release notes generation failed")
        return f"Release {tag_name} — generation failed"


def handle_dependabot_alert(payload: dict, auth, config) -> str:
    """Dependabot alert → assess impact and auto-create fix PR for HIGH/CRITICAL.

    For CRITICAL/HIGH: auto bump + create PR.
    For MEDIUM/LOW: comment only for manual review.
    """
    from pipeline.security import (
        parse_dependabot_alert,
        check_package_usage,
        create_dependency_bump_pr,
        render_security_comment,
    )

    repo = payload.get("repository", {})
    installation_id = payload.get("installation", {}).get("id")
    repo_full_name = repo.get("full_name", "")
    repo_name = repo.get("name", "")

    if not installation_id:
        return "No installation ID in payload"

    parsed = parse_dependabot_alert(payload)
    severity = parsed.get("severity", "medium")
    package_name = parsed.get("package_name_raw", "")

    if not package_name:
        return "Could not parse package name from alert"

    # Clone/fetch repo for checking
    clone_dir = Path("./pipeline_repos") / repo_name
    try:
        token = auth.get_installation_token(installation_id)
        clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
        if not (clone_dir / ".git").exists():
            subprocess.run(
                ["git", "clone", "--depth=1", clone_url, str(clone_dir)],
                capture_output=True, text=True, timeout=300, check=True,
            )
    except Exception:
        logger.exception("Failed to clone repo for security check")
        return "Failed to clone repo"

    usage = check_package_usage(str(clone_dir), package_name)

    if not usage["in_use"]:
        return f"Security alert: {package_name} not used in {repo_full_name}"

    if severity in ("critical", "high"):
        # Auto-create fix
        fixed_version = parsed.get("fixed_version", "")
        if not fixed_version:
            return f"Security alert ({severity}): no fixed version available for {package_name}"

        success, msg = create_dependency_bump_pr(
            str(clone_dir), package_name, fixed_version,
            severity, cve_id=parsed.get("cve_id", ""),
        )

        if success:
            # Push fix as PR
            branch = f"agent/security-{package_name}-{int(__import__('time').time())}"
            subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=str(clone_dir), capture_output=True, text=True, timeout=30,
            )
            subprocess.run(
                ["git", "commit", "-am", f"[Security] Bump {package_name} to >={fixed_version}"],
                cwd=str(clone_dir), capture_output=True, text=True, timeout=30,
            )
            push_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
            subprocess.run(
                ["git", "push", "--set-upstream", push_url, branch],
                cwd=str(clone_dir), capture_output=True, text=True, timeout=120,
            )
            pr_url = _create_pull_request(
                auth, installation_id, repo_full_name, branch,
                f"[Security] Bump {package_name} to {fixed_version}",
                render_security_comment(parsed, True),
            )
            return (
                f"Security alert ({severity}): {package_name} — "
                f"fix PR created: {pr_url or 'failed'}"
            )
        else:
            return f"Security alert ({severity}): {package_name} — fix failed: {msg}"
    else:
        # Medium/low: comment only
        _post_pr_comment(
            auth, installation_id, repo_full_name, 0,
            render_security_comment(parsed, True),
        )
        return f"Security alert ({severity}): {package_name} — comment posted for review"


def handle_installation_created(payload: dict, auth, config) -> str:
    """GitHub App installed on a new repo → register in system.

    Sends a brief welcome message and registers the repo for monitoring.
    """
    installation = payload.get("installation", {})
    repos = payload.get("repositories", [])
    sender = payload.get("sender", {}).get("login", "")

    repo_names = [r.get("full_name", "") for r in repos if r.get("full_name")]
    repo_list = ", ".join(repo_names[:10])
    total = len(repo_names)

    # Register each repo in the scout scheduler
    try:
        from pipeline.scout import ScoutScheduler
        scheduler = ScoutScheduler()
        for rn in repo_names:
            scheduler.add_repo(rn)
        logger.info(
            "Registered %d repos via installation by @%s: %s",
            total, sender, repo_list,
        )
    except Exception:
        logger.exception("Failed to register repos in scheduler")

    return (
        f"Installation by @{sender} — "
        f"{total} repo(s) registered: {repo_list}"
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
        try:
            from pipeline.metrics import TTRTracker
            TTRTracker.record_response(repo_full_name, issue_number, "comment")
        except Exception:
            pass
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
        try:
            from pipeline.metrics import TTRTracker
            TTRTracker.record_response(repo_full_name, pr_number, "comment")
        except Exception:
            pass
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


def _add_pr_labels(
    auth, installation_id: int, repo_full_name: str,
    pr_number: int, labels: list[str],
) -> None:
    """Add labels to a PR. Fails silently — labels are non-critical."""
    try:
        gh = auth.get_github_client(installation_id)
        repo = gh.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        for label in labels:
            try:
                pr.add_to_labels(label)
            except Exception:
                pass
        logger.info("Labels %s added to %s#%d", labels, repo_full_name, pr_number)
    except Exception:
        logger.debug("Failed to add PR labels", exc_info=True)


def _add_issue_labels(
    auth, installation_id: int, repo_full_name: str,
    issue_number: int, labels: list[str],
) -> None:
    """Add labels to an issue. Fails silently — labels are non-critical."""
    try:
        gh = auth.get_github_client(installation_id)
        repo = gh.get_repo(repo_full_name)
        issue = repo.get_issue(issue_number)
        for label in labels:
            try:
                issue.add_to_labels(label)
            except Exception:
                pass
        logger.info("Labels %s added to %s#%d", labels, repo_full_name, issue_number)
    except Exception:
        logger.debug("Failed to add issue labels", exc_info=True)


def _publish_agent_result(
    *,
    clone_dir: Path,
    auth,
    installation_id: int,
    repo_full_name: str,
    branch: str,
    commit_message: str,
    pr_title: str,
    pr_body: str,
    result,
    elapsed: float,
    issue_number: int | None = None,
) -> str | None:
    """Stage, commit, push agent changes and optionally create a PR + comment.

    Returns the PR URL if a PR was created, else None.
    When *issue_number* is provided, posts status comments on that issue.
    """
    repo_dir = str(clone_dir)

    if not (result and result.is_success()):
        if issue_number is not None:
            status = result.status.value if result else "error"
            _post_issue_comment(
                auth, installation_id, repo_full_name, issue_number,
                f"Agent was unable to fix this issue.\n\n"
                f"Status: `{status}` | Time: {elapsed:.0f}s",
            )
        return None

    # Stage changes
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

    if not diff_text:
        if issue_number is not None:
            _post_issue_comment(
                auth, installation_id, repo_full_name, issue_number,
                f"Agent analyzed but produced no code changes.\n\n"
                f"Summary: {result.summary[:500]}",
            )
        return None

    # Commit and push
    subprocess.run(
        ["git", "commit", "-am", commit_message],
        cwd=repo_dir, capture_output=True, text=True, timeout=30,
    )
    token = auth.get_installation_token(installation_id)
    push_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    subprocess.run(
        ["git", "push", "--set-upstream", push_url, branch],
        cwd=repo_dir, capture_output=True, text=True, timeout=120,
    )

    # Create PR
    pr_url = _create_pull_request(
        auth, installation_id, repo_full_name, branch, pr_title, pr_body,
    )

    if issue_number is not None:
        _post_issue_comment(
            auth, installation_id, repo_full_name, issue_number,
            f"PR created with proposed fix.\n\n"
            f"Steps: {result.steps_taken} | "
            f"Tokens: {result.total_tokens:,} | "
            f"Time: {elapsed:.0f}s",
        )

    return pr_url
