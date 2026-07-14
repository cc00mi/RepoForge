"""
pipeline/welcome.py

Community engagement: first-time contributor welcome, PR/issue template
completeness check, CLA/DCO verification, contribution hints.

Most checks are rule-based (no LLM). Only issue completeness for ambiguous
cases escalates to a small LLM prompt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

WELCOME_COMMENT_TEMPLATE = """\
## Welcome to {repo_name}, @{username}!

This is your first contribution here — thanks for taking the time!

### Quick Checklist
- [ ] Tests added/updated for your changes
- [ ] Documentation updated if needed
- [ ] Commit messages follow project conventions

### Useful Links
- [Contributing Guide]({contributing_url})

A maintainer will review your PR shortly. Feel free to @mention if you have questions!
"""

FIRST_ISSUE_WELCOME = """\
## Welcome, @{username}!

This is your first issue in {repo_name} — thanks for reporting!

A maintainer will take a look shortly. In the meantime, make sure you've included:
- Steps to reproduce the issue
- Expected behavior vs actual behavior
- Your environment (OS, version, etc.)

*Automated welcome by RepoForge*
"""

PR_TEMPLATE_CHECK_COMMENT = """\
## PR Completeness Check

Thanks for the PR, @{username}! I noticed a few things that would help reviewers:

{missing_items}

No worries — just update the PR description when you have a moment.
"""

ISSUE_TEMPLATE_CHECK_COMMENT = """\
## Issue Completeness Check

Thanks for the report, @{username}! To help us diagnose this faster, could you add:

{missing_items}

Just edit the issue description when ready.
"""

CLA_MISSING_COMMENT = """\
## CLA / DCO Check

Hi @{username}, this repository requires a Developer Certificate of Origin (DCO) sign-off.

Please add `Signed-off-by: {username} <{email}>` to your commit message.

```
git commit --amend -s
git push --force-with-lease
```
"""

CONTRIBUTION_HINTS_TEMPLATE = """\
## Contribution Hints

Based on the files you're changing, here are some tips:

{hints}
"""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class CompletenessReport:
    """Result of a template / body completeness check."""
    complete: bool = True
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Checkers
# ---------------------------------------------------------------------------

class WelcomeBot:
    """Detect first-time contributors and provide onboarding guidance."""

    def is_first_time(
        self,
        username: str,
        repo_full_name: str,
        auth,
        installation_id: int,
    ) -> tuple[bool, int]:
        """Check if this is the user's first PR/issue in this repo.

        Returns (is_first_time, total_contributions).
        Uses GitHub contributor stats (fast, free).
        """
        try:
            gh = auth.get_github_client(installation_id)
            repo = gh.get_repo(repo_full_name)
            # Check contributor stats
            contributors = list(repo.get_contributors())
            for c in contributors:
                if c.login.lower() == username.lower():
                    return False, c.contributions
            return True, 0
        except Exception:
            logger.debug("Failed to check contributor status", exc_info=True)
            return False, 0

    def welcome_pr(
        self,
        username: str,
        repo_full_name: str,
        repo_name: str,
        auth,
        installation_id: int,
        pr_number: int,
    ) -> str | None:
        """If first-time, return welcome comment text. Else None."""
        is_first, contributions = self.is_first_time(username, repo_full_name, auth, installation_id)
        if not is_first:
            return None

        try:
            gh = auth.get_github_client(installation_id)
            repo = gh.get_repo(repo_full_name)
            pr = repo.get_pull(pr_number)
            pr.create_issue_comment(
                WELCOME_COMMENT_TEMPLATE.format(
                    repo_name=repo_name,
                    username=username,
                    contributing_url=f"https://github.com/{repo_full_name}/blob/main/CONTRIBUTING.md",
                )
            )
            return f"Welcomed first-time contributor @{username}"
        except Exception:
            logger.exception("Failed to post welcome comment")
            return None

    def welcome_issue(
        self,
        username: str,
        repo_full_name: str,
        repo_name: str,
        auth,
        installation_id: int,
        issue_number: int,
    ) -> str | None:
        """If first-time issue reporter, return welcome comment text. Else None."""
        is_first, _ = self.is_first_time(username, repo_full_name, auth, installation_id)
        if not is_first:
            return None

        try:
            gh = auth.get_github_client(installation_id)
            repo = gh.get_repo(repo_full_name)
            issue = repo.get_issue(issue_number)
            issue.create_comment(
                FIRST_ISSUE_WELCOME.format(
                    username=username,
                    repo_name=repo_name,
                )
            )
            return f"Welcomed first-time reporter @{username}"
        except Exception:
            logger.exception("Failed to post welcome comment")
            return None


def check_pr_completeness(pr_body: str | None) -> CompletenessReport:
    """Check if the PR body has enough information.

    Returns CompletenessReport with list of missing items.
    """
    issues: list[str] = []

    body = (pr_body or "").strip()

    if len(body) < 50:
        issues.append("- **PR description is very short** — please add more detail about what this changes and why")

    if not re.search(r'(fix|close|resolve|address)(s|es|d)?\s+(#\d+|issue)', body, re.IGNORECASE):
        issues.append("- **No linked issue** — reference the issue this PR fixes (e.g., 'Fixes #123')")

    if not re.search(r'(test|verify|check|validate)', body, re.IGNORECASE):
        issues.append("- **No test plan mentioned** — how was this change tested?")

    return CompletenessReport(
        complete=len(issues) == 0,
        issues=issues,
    )


def check_issue_completeness(issue_body: str | None) -> CompletenessReport:
    """Check if the issue body has enough to reproduce/diagnose.

    Returns CompletenessReport with list of missing items.
    """
    issues: list[str] = []
    body = (issue_body or "").strip()

    if len(body) < 80:
        issues.append("- **Description is very short** — please add more detail")
        return CompletenessReport(complete=False, issues=issues)

    if not _has_repro_steps(body):
        issues.append("- **No reproduction steps** — how can someone else trigger this bug?")

    if not _has_version_info(body):
        issues.append("- **No version/environment info** — what version/OS are you using?")

    if not re.search(r'(expect|actual|should|happen)', body, re.IGNORECASE):
        issues.append("- **Expected vs actual behavior not clear**")

    return CompletenessReport(
        complete=len(issues) == 0,
        issues=issues,
    )


def check_cla(username: str, commits: list[dict]) -> tuple[bool, str]:
    """Check if commits have Signed-off-by lines.

    Returns (has_signed_off, message).
    """
    for commit in commits:
        msg = commit.get("commit", {}).get("message", "")
        if f"Signed-off-by: {username}" in msg or f"Signed-off-by: {username} <" in msg:
            return True, ""
    return False, CLA_MISSING_COMMENT.format(
        username=username, email=f"{username}@users.noreply.github.com",
    )


def get_contribution_hints(
    changed_files: list[str],
    repo_full_name: str,
) -> str | None:
    """Generate contribution hints based on RepoMemory hotspots.

    Checks whether the changed files match known review hotspots and returns
    hints about common issues found in those files.
    """
    try:
        from memory.repo_memory import memory_service
        memory = memory_service.load(repo_full_name)
    except Exception:
        return None

    if not memory.review_hotspots:
        return None

    hints: list[str] = []
    for f in changed_files:
        for hs in memory.review_hotspots:
            if hs.file_path in f or f in hs.file_path:
                sev_parts = []
                if hs.critical_findings:
                    sev_parts.append(f"{hs.critical_findings} critical")
                if hs.high_findings:
                    sev_parts.append(f"{hs.high_findings} high")
                sev_str = " + ".join(sev_parts) if sev_parts else "some"

                hints.append(
                    f"- **`{hs.file_path}`**: This file has had {sev_str} review "
                    f"findings in the past ({hs.review_count} reviews)."
                )
                if hs.common_issues:
                    hints.append(f"  Common issues: {', '.join(hs.common_issues[:3])}")
                break  # one hint per file

    if not hints:
        return None

    return CONTRIBUTION_HINTS_TEMPLATE.format(
        hints="\n".join(hints),
    )


# ---------------------------------------------------------------------------
# Heuristic checks
# ---------------------------------------------------------------------------

def _has_repro_steps(body: str) -> bool:
    """Check if the issue contains reproduction steps."""
    patterns = [
        r'steps?\s*(to|for)\s*(reproduce|replicate|trigger)',
        r'(repro|reproduction|reproduce)\s*(steps|instructions|guide)?',
        r'(\d\)|-\s)(\s*)(run|execute|start|open|click|navigate|call|send)',
        r'```[\s\S]*?```',  # code block suggests repro
        r'(how to|to reproduce|to trigger|to see)',
    ]
    return any(re.search(p, body, re.IGNORECASE) for p in patterns)


def _has_version_info(body: str) -> bool:
    """Check if the issue includes version/environment information."""
    patterns = [
        r'(version|v\d+\.\d+\.\d+|release)',
        r'(python|node|go|rust|java)\s*(version)?\s*[\d.]+',
        r'(macos|windows|linux|ubuntu|debian|centos)',
        r'(browser|chrome|firefox|safari|edge)\s*[\d.]+',
        r'(docker|kubernetes|k8s|container)',
    ]
    return any(re.search(p, body, re.IGNORECASE) for p in patterns)
