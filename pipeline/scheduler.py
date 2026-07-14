"""
pipeline/scheduler.py

Background scheduler for periodic maintenance tasks.  Uses stdlib threading
so no external dependency (no APScheduler required).

Jobs:
  Scout & Rank   — every 6 hours
  Stale Scan     — every 24 hours (daily)
  Security Scan  — every 4 hours

Wire into the Flask app via scheduler.start() / scheduler.stop().
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------

def _job_scout(auth, config, monitored_repos: list[str], installation_id: int):
    """Scout: fetch + rank open issues, trigger auto-fix for high-score candidates."""
    from pipeline.scout import IssueScout

    scout = IssueScout()
    for repo in monitored_repos:
        try:
            ranked = scout.fetch_and_rank(repo, auth, installation_id)
            for ri in ranked:
                if ri.suggested_action == "auto_fix":
                    logger.info(
                        "Scout auto-fix candidate: %s#%d (score=%.0f) — %s",
                        repo, ri.issue_number, ri.score, ri.title[:80],
                    )
                elif ri.suggested_action == "review":
                    logger.debug(
                        "Scout review candidate: %s#%d (score=%.0f)",
                        repo, ri.issue_number, ri.score,
                    )
        except Exception:
            logger.exception("Scout job failed for repo %s", repo)


def _job_stale(auth, config, monitored_repos: list[str], installation_id: int):
    """Stale scan: find inactive PRs and escalate (warn → label → close)."""
    from pipeline.stale_manager import StaleManager

    manager = StaleManager()
    for repo in monitored_repos:
        try:
            gh = auth.get_github_client(installation_id)
            gh_repo = gh.get_repo(repo)
            open_prs = []
            for pr in gh_repo.get_pulls(state="open", sort="updated", direction="asc"):
                open_prs.append({
                    "number": pr.number,
                    "title": pr.title,
                    "user": {"login": pr.user.login} if pr.user else {"login": "unknown"},
                    "labels": [lab.name for lab in pr.labels],
                    "updated_at": pr.updated_at.isoformat() if pr.updated_at else "",
                    "repo_full_name": repo,
                })

            actions = manager.scan_prs(open_prs)

            # Log stale metrics for reduction tracking
            try:
                from pipeline.metrics import StaleMetricsLogger, StaleScanRecord
                exempt_labels = {"blocked", "on-hold", "security", "keep-open", "wip", "draft"}
                exempt_count = sum(
                    1 for pr in open_prs
                    if any(lab.lower() in exempt_labels
                           for lab in (pr.get("labels", []) if isinstance(pr.get("labels", [{}])[0], str)
                                       else [l.get("name", "") for l in pr.get("labels", [])]))
                )
                warn_count = sum(1 for a in actions if a.action == "warn")
                label_count = sum(1 for a in actions if a.action == "label_stale")
                close_count = sum(1 for a in actions if a.action == "close")
                StaleMetricsLogger.log_scan(StaleScanRecord(
                    repo_full_name=repo,
                    total_scanned=len(open_prs),
                    exempt_count=exempt_count,
                    stale_count=len(actions),
                    warn_count=warn_count,
                    label_count=label_count,
                    close_count=close_count,
                    dry_run=False,
                ))
            except Exception:
                logger.debug("Failed to log stale metrics", exc_info=True)

            if actions:
                logger.info("Stale scan: %d actions for %s", len(actions), repo)
                results = manager.execute(actions, auth, installation_id, dry_run=False)
                for r in results:
                    logger.info("  %s", r)
        except Exception:
            logger.exception("Stale scan failed for repo %s", repo)


def _job_security(auth, config, monitored_repos: list[str], installation_id: int):
    """Security scan: check Dependabot alerts for monitored repos."""
    for repo in monitored_repos:
        try:
            gh = auth.get_github_client(installation_id)
            gh_repo = gh.get_repo(repo)

            alerts = gh_repo.get_vulnerability_alert()
            for alert in alerts:
                # Build a minimal payload matching the webhook shape
                payload = {
                    "action": "created",
                    "alert": alert.raw_data if hasattr(alert, "raw_data") else {},
                    "repository": {"full_name": repo},
                }
                from pipeline.handlers import handle_dependabot_alert
                try:
                    result = handle_dependabot_alert(payload, auth, config)
                    logger.info("Security scan: %s → %s", repo, result)
                except Exception:
                    logger.exception("Security handler failed for alert in %s", repo)
        except Exception:
            logger.exception("Security scan failed for repo %s", repo)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class BackgroundScheduler:
    """Simple periodic job runner backed by threading.Timer.

    Usage::

        sched = BackgroundScheduler(auth, config, monitored_repos, installation_id)
        sched.start()
        ...
        sched.stop()
    """

    def __init__(
        self,
        auth,
        config,
        monitored_repos: list[str],
        installation_id: int,
        *,
        scout_interval_hours: float = 6.0,
        stale_interval_hours: float = 24.0,
        security_interval_hours: float = 4.0,
    ):
        self._auth = auth
        self._config = config
        self._repos = list(monitored_repos)
        self._installation_id = installation_id

        self._intervals: dict[str, float] = {
            "scout":    scout_interval_hours * 3600,
            "stale":    stale_interval_hours * 3600,
            "security": security_interval_hours * 3600,
        }
        self._jobs: dict[str, callable] = {
            "scout":    lambda: _job_scout(auth, config, self._repos, installation_id),
            "stale":    lambda: _job_stale(auth, config, self._repos, installation_id),
            "security": lambda: _job_security(auth, config, self._repos, installation_id),
        }

        self._timers: dict[str, threading.Timer] = {}
        self._stopped = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all periodic jobs. Each fires immediately on first run,
        then repeats at its configured interval."""
        if not self._repos:
            logger.warning("Scheduler: no monitored repos configured, skipping start")
            return

        logger.info(
            "Scheduler starting: scout=%.0fh stale=%.0fh security=%.0fh, repos=%d",
            self._intervals["scout"] / 3600,
            self._intervals["stale"] / 3600,
            self._intervals["security"] / 3600,
            len(self._repos),
        )

        for name in self._jobs:
            self._schedule_next(name)

    def stop(self) -> None:
        """Cancel all pending timers."""
        self._stopped.set()
        for name, timer in self._timers.items():
            timer.cancel()
        self._timers.clear()
        logger.info("Scheduler stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _schedule_next(self, name: str) -> None:
        """Schedule the next run of *name* after its interval."""
        if self._stopped.is_set():
            return

        interval = self._intervals[name]
        timer = threading.Timer(interval, self._run_and_reschedule, args=[name])
        timer.daemon = True
        self._timers[name] = timer
        timer.start()

    def _run_and_reschedule(self, name: str) -> None:
        """Execute the job, then reschedule."""
        if self._stopped.is_set():
            return

        logger.debug("Scheduler job starting: %s", name)
        try:
            self._jobs[name]()
        except Exception:
            logger.exception("Scheduler job %s raised unexpected error", name)

        self._schedule_next(name)


# ---------------------------------------------------------------------------
# Convenience: attach to Flask app
# ---------------------------------------------------------------------------

_scheduler_instance: BackgroundScheduler | None = None


def start_scheduler(
    auth,
    config,
    monitored_repos: list[str],
    installation_id: int,
) -> BackgroundScheduler:
    """Create and start the background scheduler. Safe to call multiple times
    (subsequent calls are no-ops)."""
    global _scheduler_instance
    if _scheduler_instance is not None:
        return _scheduler_instance

    _scheduler_instance = BackgroundScheduler(
        auth, config, monitored_repos, installation_id,
    )
    _scheduler_instance.start()
    return _scheduler_instance


def stop_scheduler() -> None:
    """Stop the background scheduler if running."""
    global _scheduler_instance
    if _scheduler_instance is not None:
        _scheduler_instance.stop()
        _scheduler_instance = None
