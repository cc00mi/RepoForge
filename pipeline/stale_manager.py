"""
pipeline/stale_manager.py

Stale PR detection and lifecycle management.

Three-tier escalation:
  1. Warn (N days no activity) → @mention PR author + comment
  2. Label stale (M days) → add `stale` label
  3. Close (K days) → auto-close with comment

Exempt labels prevent action: blocked, on-hold, security, keep-open.

Scheduling: intended to run as a daily cron via APScheduler (see scheduler.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@dataclass
class StalePolicy:
    """Configurable thresholds for stale PR management."""

    warn_after_days: int = 7
    stale_label_days: int = 14
    close_after_days: int = 30
    exempt_labels: list[str] = field(default_factory=lambda: [
        "blocked", "on-hold", "security", "keep-open", "wip", "draft",
    ])


DEFAULT_STALE_POLICY = StalePolicy()


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------

@dataclass
class StaleAction:
    """A recommended action for a stale PR."""

    pr_number: int
    pr_title: str
    repo_full_name: str
    author: str
    action: str            # "warn" | "label_stale" | "close"
    days_inactive: int
    reason: str


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class StaleManager:
    """Scan open PRs and generate recommended stale actions."""

    def __init__(self, policy: StalePolicy | None = None):
        self._policy = policy or DEFAULT_STALE_POLICY

    def scan_prs(self, open_prs: list[dict], now: datetime | None = None) -> list[StaleAction]:
        """Scan a list of open PRs and return recommended actions.

        Args:
            open_prs: list of PR dicts with keys:
                number, title, user.login, labels (list of name strings),
                updated_at (ISO datetime string)
            now: override current time (for testing/simulation)

        Returns:
            List of StaleAction, ordered by severity (close > label > warn).
        """
        if now is None:
            now = datetime.now(timezone.utc)
        actions: list[StaleAction] = []

        for pr in open_prs:
            # Check exempt labels
            labels = [lab.lower() if isinstance(lab, str) else lab.get("name", "").lower()
                      for lab in pr.get("labels", [])]
            if any(ex in labels for ex in self._policy.exempt_labels):
                continue

            # Calculate days inactive
            updated_at = pr.get("updated_at", "")
            if not updated_at:
                continue
            try:
                last_activity = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            days_inactive = (now - last_activity).days

            if days_inactive < self._policy.warn_after_days:
                continue

            author = pr.get("user", {}).get("login", "unknown") if isinstance(pr.get("user"), dict) else "unknown"
            title = pr.get("title", "Untitled")
            number = pr.get("number", 0)
            repo = pr.get("repo_full_name", "")

            if days_inactive >= self._policy.close_after_days:
                actions.append(StaleAction(
                    pr_number=number, pr_title=title, repo_full_name=repo,
                    author=author, action="close",
                    days_inactive=days_inactive,
                    reason=f"No activity for {days_inactive} days (threshold: {self._policy.close_after_days})",
                ))
            elif days_inactive >= self._policy.stale_label_days:
                actions.append(StaleAction(
                    pr_number=number, pr_title=title, repo_full_name=repo,
                    author=author, action="label_stale",
                    days_inactive=days_inactive,
                    reason=f"No activity for {days_inactive} days (threshold: {self._policy.stale_label_days})",
                ))
            else:
                actions.append(StaleAction(
                    pr_number=number, pr_title=title, repo_full_name=repo,
                    author=author, action="warn",
                    days_inactive=days_inactive,
                    reason=f"No activity for {days_inactive} days (threshold: {self._policy.warn_after_days})",
                ))

        # Sort: close first, then label, then warn
        severity_order = {"close": 0, "label_stale": 1, "warn": 2}
        actions.sort(key=lambda a: (severity_order.get(a.action, 9), a.days_inactive))
        return actions

    def execute(
        self,
        actions: list[StaleAction],
        auth,
        installation_id: int,
        dry_run: bool = True,
    ) -> list[str]:
        """Apply stale actions via GitHub API.

        Args:
            actions: list of StaleAction to execute
            auth: GitHubAppAuth instance
            installation_id: GitHub App installation ID
            dry_run: if True, only log actions without executing

        Returns:
            List of action descriptions taken.
        """
        results: list[str] = []

        for action in actions:
            desc = (
                f"[{'DRY_RUN' if dry_run else 'EXEC'}] {action.action.upper()} "
                f"PR #{action.pr_number} ({action.repo_full_name}): "
                f"{action.reason}"
            )

            if dry_run:
                logger.info(desc)
                results.append(desc)
                continue

            try:
                gh = auth.get_github_client(installation_id)
                repo = gh.get_repo(action.repo_full_name)
                pr = repo.get_pull(action.pr_number)

                if action.action == "warn":
                    comment = _WARN_COMMENT_TEMPLATE.format(
                        author=f"@{action.author}",
                        days=action.days_inactive,
                    )
                    pr.create_issue_comment(comment)
                    results.append(desc)

                elif action.action == "label_stale":
                    pr.add_to_labels("stale")
                    comment = _STALE_COMMENT_TEMPLATE.format(
                        author=f"@{action.author}",
                        days=action.days_inactive,
                        close_days=self._policy.close_after_days,
                    )
                    pr.create_issue_comment(comment)
                    results.append(desc)

                elif action.action == "close":
                    comment = _CLOSE_COMMENT_TEMPLATE.format(
                        author=f"@{action.author}",
                        days=action.days_inactive,
                    )
                    pr.create_issue_comment(comment)
                    pr.edit(state="closed")
                    results.append(desc)

            except Exception:
                logger.exception("Failed to execute stale action: %s", desc)
                results.append(f"FAILED: {desc}")

        return results


# ---------------------------------------------------------------------------
# Comment templates
# ---------------------------------------------------------------------------

_WARN_COMMENT_TEMPLATE = """\
Hi {author}, this PR has been inactive for {days} days.

Is this still being worked on? If you need help or review, feel free to @mention a maintainer.

If this PR is no longer relevant, consider closing it to keep the PR queue tidy.
"""

_STALE_COMMENT_TEMPLATE = """\
This PR has been marked as **stale** after {days} days of inactivity.

It will be automatically closed in {close_days} days if there is no further activity.

To keep this PR open, please add a comment or push new commits. Adding a `keep-open` label will exempt it from automatic closure.
"""

_CLOSE_COMMENT_TEMPLATE = """\
This PR has been automatically closed after {days} days of inactivity.

If this change is still needed, feel free to reopen or create a new PR.

*Automated by RepoForge Stale Manager.*
"""
