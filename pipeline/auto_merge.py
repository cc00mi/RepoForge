"""
pipeline/auto_merge.py

Auto-merge eligibility assessment — evaluates whether a PR satisfies all
conditions for safe automatic merging.

IMPORTANT: This module only ASSESSES eligibility. It does NOT execute merges.
The final decision always rests with a human maintainer.

Gates (all must pass):
  1. Explicit auto-merge label present
  2. CI checks all passing (required checks configurable)
  3. At least one approving human review
  4. No CHANGES_REQUESTED reviews
  5. No CRITICAL findings from agent review
  6. PR size within limit (max 500 lines by default)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@dataclass
class AutoMergePolicy:
    """Defines when auto-merge is allowed.

    All conditions default to True (safety-first). Users must explicitly
    add the `auto-merge` label to opt in.
    """

    require_ci_pass: bool = True
    require_approval: bool = True
    require_no_changes_requested: bool = True
    require_no_critical_findings: bool = True
    max_pr_size_lines: int = 500
    required_check_names: list[str] = field(default_factory=list)
    auto_merge_label: str = "auto-merge"


# Singleton default policy
DEFAULT_POLICY = AutoMergePolicy()


# ---------------------------------------------------------------------------
# Eligibility check
# ---------------------------------------------------------------------------

def check_auto_merge_eligibility(
    pr: dict,
    status_checks: list[dict],
    reviews: list[dict],
    agent_review_critical_count: int = 0,
    policy: AutoMergePolicy | None = None,
) -> tuple[bool, str]:
    """Run all auto-merge gates and return (eligible, reason).

    Args:
        pr: PR object from GitHub API (must have labels, additions, deletions)
        status_checks: list of check run / status objects
        reviews: list of review objects
        agent_review_critical_count: number of CRITICAL findings from our agent
        policy: AutoMergePolicy, uses DEFAULT_POLICY if None

    Returns:
        (eligible: bool, reason: str)
    """
    if policy is None:
        policy = DEFAULT_POLICY

    # Gate 1: auto-merge label (explicit opt-in)
    labels = [lab["name"].lower() if isinstance(lab, dict) else str(lab).lower()
              for lab in pr.get("labels", [])]
    if policy.auto_merge_label not in labels:
        return False, "Missing auto-merge label"

    # Gate 2: CI must pass
    if policy.require_ci_pass:
        if policy.required_check_names:
            # Only check specific named checks
            required_set = set(policy.required_check_names)
            for check in status_checks:
                name = check.get("name", check.get("context", ""))
                if name in required_set:
                    if check.get("conclusion", check.get("state", "")) != "success":
                        return False, f"Required CI check '{name}' not passing"
        else:
            # All completed checks must pass
            for check in status_checks:
                conclusion = check.get("conclusion", check.get("state", ""))
                status = check.get("status", "")
                if status == "completed" and conclusion not in ("success", "neutral", "skipped"):
                    name = check.get("name", check.get("context", "?"))
                    return False, f"CI check '{name}' not passing (conclusion: {conclusion})"

    # Gate 3: at least one approving human review
    if policy.require_approval:
        approved = False
        for r in reviews:
            if r.get("state") == "APPROVED":
                approved = True
                break
        if not approved:
            return False, "No approving human review"

    # Gate 4: no CHANGES_REQUESTED
    if policy.require_no_changes_requested:
        for r in reviews:
            if r.get("state") == "CHANGES_REQUESTED":
                return False, "Changes requested — must be resolved first"

    # Gate 5: no CRITICAL agent findings
    if policy.require_no_critical_findings and agent_review_critical_count > 0:
        return False, f"Agent review found {agent_review_critical_count} critical issue(s)"

    # Gate 6: size check
    additions = pr.get("additions", 0)
    deletions = pr.get("deletions", 0)
    total = additions + deletions
    if total > policy.max_pr_size_lines:
        return False, f"PR too large ({total} lines > {policy.max_pr_size_lines} limit)"

    return True, "All auto-merge conditions satisfied"


# ---------------------------------------------------------------------------
# Integration helper — called from handlers
# ---------------------------------------------------------------------------

def _run_auto_merge_check(
    auth,
    installation_id: int,
    repo_full_name: str,
    pr_number: int,
    token: str,
) -> tuple[bool, str]:
    """Fetch live PR data from GitHub API and run the eligibility check.

    Used by handle_pr_labeled and the dashboard.
    """
    try:
        gh = auth.get_github_client(installation_id)
        repo = gh.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)

        # Build PR dict
        pr_dict = {
            "labels": [lab.name for lab in pr.labels],
            "additions": pr.additions,
            "deletions": pr.deletions,
        }

        # Fetch combined commit status
        head_sha = pr.head.sha
        combined = repo.get_commit(head_sha).get_combined_status()
        status_checks = [
            {"name": s.context, "state": s.state, "conclusion": s.state}
            for s in combined.statuses
        ]

        # Fetch reviews
        reviews = [
            {"state": r.state, "user": r.user.login if r.user else ""}
            for r in pr.get_reviews()
        ]

        # Check agent review findings from ReviewMemory
        agent_critical = 0
        try:
            from pipeline.review_memory import load_review_memory, previous_critical_count
            rev_mem = load_review_memory(repo_full_name, pr_number)
            agent_critical = previous_critical_count(rev_mem)
        except Exception:
            pass

        return check_auto_merge_eligibility(
            pr_dict, status_checks, reviews, agent_critical,
        )

    except Exception:
        logger.exception("Auto-merge check failed")
        return False, "Failed to query GitHub API for eligibility data"
