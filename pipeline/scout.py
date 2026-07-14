"""
pipeline/scout.py

Active Issue Scout — periodically fetch open issues from monitored repos,
rank by relevance/solvability, and optionally trigger auto-fix for
high-confidence candidates.

Designed to be run by APScheduler on a configurable interval (default: 6h).
Complements the webhook-based passive pipeline with proactive discovery.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RankedIssue:
    """An issue with a relevance score for agent attention."""
    issue_number: int
    title: str = ""
    body: str = ""
    repo_full_name: str = ""
    labels: list[str] = field(default_factory=list)
    score: float = 0.0
    score_breakdown: dict = field(default_factory=dict)
    suggested_action: str = "ignore"  # auto_fix | review | ignore


# ---------------------------------------------------------------------------
# Scout
# ---------------------------------------------------------------------------

class IssueScout:
    """Fetch and rank open issues from monitored GitHub repositories."""

    # Labels to prioritize (ordered by relevance)
    PRIORITY_LABELS = [
        "help wanted", "good first issue", "bug", "agent-fix",
    ]

    # Labels to deprioritize
    SKIP_LABELS = [
        "wontfix", "invalid", "duplicate", "question", "discussion",
        "needs-human", "escalate",
    ]

    def __init__(self, auto_fix_threshold: float = 70.0):
        self._auto_fix_threshold = auto_fix_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_and_rank(
        self,
        repo_full_name: str,
        auth,
        installation_id: int,
        max_results: int = 20,
        labels: list[str] | None = None,
    ) -> list[RankedIssue]:
        """Fetch open issues and rank them by agent-relevance.

        Returns top candidates sorted by score descending.
        """
        try:
            gh = auth.get_github_client(installation_id)
            repo = gh.get_repo(repo_full_name)

            search_labels = labels or self.PRIORITY_LABELS
            issues = []
            for label in search_labels:
                try:
                    page = repo.get_issues(
                        state="open", labels=[label],
                        sort="created", direction="desc",
                    )
                    for iss in page[:max_results // len(search_labels) + 1]:
                        issues.append(iss)
                except Exception:
                    pass

            ranked = self.rank_issues(issues, repo_full_name)
            return ranked[:max_results]

        except Exception:
            logger.exception("Scout fetch failed for %s", repo_full_name)
            return []

    def rank_issues(
        self,
        issues: list,
        repo_full_name: str,
    ) -> list[RankedIssue]:
        """Score issues using lightweight heuristics + RepoMemory signals.

        No LLM calls — purely heuristic scoring (~0 tokens).
        """
        from memory.repo_memory import memory_service

        try:
            memory = memory_service.load(repo_full_name)
        except Exception:
            memory = None

        ranked: list[RankedIssue] = []

        for iss in issues:
            title = iss.title or ""
            body = iss.body or ""
            labels = [lab.name.lower() for lab in iss.labels] if iss.labels else []
            number = iss.number

            # Skip issues with exclusion labels
            if any(skip in labels for skip in self.SKIP_LABELS):
                continue

            score = 0.0
            breakdown = {}

            # +50: issue mentions a path in preferred_paths
            if memory and memory.preferred_paths:
                matched_paths = []
                for path in memory.preferred_paths:
                    if path in title or path in body:
                        matched_paths.append(path)
                        score += 50
                if matched_paths:
                    breakdown["path_match"] = {
                        "score": 50 * len(matched_paths),
                        "paths": matched_paths[:5],
                    }

            # +30: similar to a previously resolved issue
            if memory and memory.recent_issues:
                best_sim = 0.0
                best_ref = ""
                task_text = (title + " " + body[:500]).lower()
                for past in memory.recent_issues:
                    if past.status not in ("validated", "published", "pr_opened"):
                        continue
                    past_text = (past.title + " " + past.validation_summary).lower()
                    sim = difflib.SequenceMatcher(None, task_text, past_text).ratio()
                    if sim > best_sim:
                        best_sim = sim
                        best_ref = past.reference
                if best_sim > 0.35:
                    points = int(30 * best_sim)
                    score += points
                    breakdown["similar_past"] = {
                        "score": points,
                        "reference": best_ref,
                        "similarity": round(best_sim, 2),
                    }

            # +10: has clear reproduction steps
            body_lower = (body or "").lower()
            if any(kw in body_lower for kw in [
                "steps to reproduce", "reproduction", "to reproduce:",
                "how to reproduce", "expected behavior", "actual behavior",
            ]):
                score += 10
                breakdown["has_repro"] = {"score": 10}

            # +5: has an actionable title
            if any(kw in title.lower() for kw in [
                "fix", "add", "update", "remove", "replace", "support",
                "crash", "error", "broken", "fail",
            ]):
                score += 5
                breakdown["actionable_title"] = {"score": 5}

            # +10 bonus: labeled with explicit agent-fix or help wanted
            if "agent-fix" in labels:
                score += 40
                breakdown["agent_fix_label"] = {"score": 40}
            elif "help wanted" in labels:
                score += 20
                breakdown["help_wanted_label"] = {"score": 20}
            elif "good first issue" in labels:
                score += 15
                breakdown["good_first_issue_label"] = {"score": 15}

            # -20: known validation failures on related paths
            if memory and memory.validation_signals:
                for vs in memory.validation_signals:
                    if vs.failure_count > 0:
                        if any(path in (title + body) for path in memory.preferred_paths):
                            score -= 10
                            breakdown["validation_risk"] = {"score": -10, "command": vs.command}
                            break

            # Decision
            if score >= self._auto_fix_threshold:
                action = "auto_fix"
            elif score >= 30:
                action = "review"
            else:
                action = "ignore"

            ranked.append(RankedIssue(
                issue_number=number,
                title=title,
                body=body or "",
                repo_full_name=repo_full_name,
                labels=labels,
                score=score,
                score_breakdown=breakdown,
                suggested_action=action,
            ))

        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------

class ScoutScheduler:
    """Manages periodic scout runs for monitored repositories.

    Intended to be wired into APScheduler or a simple background thread.
    """

    def __init__(self, scout: IssueScout | None = None):
        self._scout = scout or IssueScout()
        self._monitored_repos: list[str] = []

    def add_repo(self, repo_full_name: str) -> None:
        if repo_full_name not in self._monitored_repos:
            self._monitored_repos.append(repo_full_name)

    def remove_repo(self, repo_full_name: str) -> None:
        if repo_full_name in self._monitored_repos:
            self._monitored_repos.remove(repo_full_name)

    @property
    def monitored_repos(self) -> list[str]:
        return list(self._monitored_repos)

    def run_scan(
        self,
        auth,
        installation_id: int,
        on_candidate=None,
    ) -> dict[str, list[RankedIssue]]:
        """Scan all monitored repos and return candidate issues.

        Args:
            auth: GitHubAppAuth instance
            installation_id: GitHub App installation ID
            on_candidate: optional callback(ranked_issue) for candidates with
                          suggested_action == "auto_fix"

        Returns:
            Dict mapping repo_full_name → list of RankedIssue
        """
        results: dict[str, list[RankedIssue]] = {}

        for repo in self._monitored_repos:
            ranked = self._scout.fetch_and_rank(repo, auth, installation_id)
            results[repo] = ranked

            for ri in ranked:
                if ri.suggested_action == "auto_fix" and on_candidate:
                    try:
                        on_candidate(ri)
                    except Exception:
                        logger.exception("on_candidate callback failed for %s#%d",
                                         repo, ri.issue_number)

        return results
