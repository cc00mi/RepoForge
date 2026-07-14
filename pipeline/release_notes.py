"""
pipeline/release_notes.py

Release Notes Generator — produces structured, grouped release notes
from merged PRs between two tags.

Trigger: push with tag ref (refs/tags/vX.Y.Z).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ReleaseNoteEntry:
    """A single release note entry for one merged PR."""
    pr_number: int
    title: str = ""
    author: str = ""
    category: str = "other"   # breaking|feature|bugfix|docs|dependency|performance|refactor|internal
    summary: str = ""         # one-sentence summary
    breaking_change: bool = False


@dataclass
class ReleaseNotes:
    """Full release notes for a version range."""
    repo_full_name: str = ""
    from_tag: str = ""
    to_tag: str = ""
    entries: list[ReleaseNoteEntry] = field(default_factory=list)

    @property
    def breaking_changes(self) -> list[ReleaseNoteEntry]:
        return [e for e in self.entries if e.breaking_change]

    @property
    def total_prs(self) -> int:
        return len(self.entries)


# ---------------------------------------------------------------------------
# Category inference (heuristic — no LLM needed)
# ---------------------------------------------------------------------------

_CATEGORY_PATTERNS: list[tuple[str, list[str]]] = [
    ("breaking", ["breaking change", "breaking:", "BREAKING CHANGE", "deprecate",
                  "remove support", "drop support"]),
    ("security", ["security", "vulnerability", "cve-", "exploit"]),
    ("feature", ["feat:", "feat(", "feature:", "add ", "implement", "new ", "introduce",
                 "support for"]),
    ("bugfix", ["fix:", "fix(", "bug", "patch", "hotfix", "resolve", "correct"]),
    ("performance", ["perf:", "perf(", "performance", "optimize", "speed", "faster",
                     "cache", "reduce memory"]),
    ("docs", ["doc:", "docs:", "documentation", "readme", "typo", "spelling"]),
    ("dependency", ["deps:", "dependabot", "bump ", "upgrade ", "update dep",
                    "pin "]),
    ("refactor", ["refactor:", "refactor(", "restructure", "cleanup", "reorganize",
                  "simplify"]),
    ("test", ["test:", "test(", "coverage", "fixture", "test suite"]),
    ("ci/cd", ["ci:", "ci(", "github action", "workflow", "deploy:", "release:"]),
]


def _infer_category(title: str) -> str:
    """Infer release note category from PR title keywords."""
    t = title.lower()
    for category, patterns in _CATEGORY_PATTERNS:
        if any(p.lower() in t for p in patterns):
            return category
    return "other"


def _is_breaking(title: str, body: str = "") -> bool:
    """Check if a PR introduces breaking changes."""
    text = (title + " " + (body or "")).lower()
    breaking_patterns = [
        r"breaking change", r"breaking:", r"BREAKING CHANGE",
        r"deprecat\w+", r"remov\w+ support", r"drop\w+ support",
        r"backwards?.incompatible", r"api change", r"signature change",
    ]
    return any(re.search(p, text) for p in breaking_patterns)


def _summarize_pr(pr: dict) -> str:
    """Extract a one-line summary from a PR.

    For best results, use LLM summarisation. This is a heuristic fallback.
    """
    title = pr.get("title", "")
    body = pr.get("body", "")

    # Try to find the first meaningful sentence in the body
    if body:
        # Strip markdown headers, checklists, code blocks
        cleaned = re.sub(r'#{1,6}\s+.*?\n', '', body)
        cleaned = re.sub(r'```[\s\S]*?```', '', cleaned)
        cleaned = re.sub(r'\[x\]|\[]\s', '', cleaned)
        cleaned = re.sub(r'---\n', '', cleaned)
        sentences = [s.strip() for s in re.split(r'[.!?]\s+', cleaned)
                     if len(s.strip()) > 20]
        if sentences:
            return sentences[0][:200]
        # Fallback: use title
    return title[:200]


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class ReleaseNotesGenerator:
    """Generate structured release notes from merged PRs."""

    def generate(
        self,
        repo_full_name: str,
        merged_prs: list[dict],
        from_tag: str,
        to_tag: str,
        llm_summarise_fn=None,
    ) -> ReleaseNotes:
        """Build release notes from a list of merged PRs.

        Args:
            repo_full_name: "owner/repo"
            merged_prs: list of PR dicts from GitHub API (must have number,
                        title, body, user.login, merged_at)
            from_tag: starting tag
            to_tag: ending tag
            llm_summarise_fn: optional (title, body) -> str summary function.
                              If None, uses heuristic extraction.

        Returns:
            ReleaseNotes ready for rendering.
        """
        entries: list[ReleaseNoteEntry] = []

        for pr in merged_prs:
            title = pr.get("title", "Untitled")
            body = pr.get("body", "") or ""
            author = pr.get("user", {}).get("login", "unknown") if isinstance(pr.get("user"), dict) else "unknown"
            number = pr.get("number", 0)

            category = _infer_category(title)
            summary = llm_summarise_fn(title, body) if llm_summarise_fn else _summarize_pr(pr)
            breaking = _is_breaking(title, body)

            entries.append(ReleaseNoteEntry(
                pr_number=number,
                title=title,
                author=author,
                category=category,
                summary=summary,
                breaking_change=breaking,
            ))

        return ReleaseNotes(
            repo_full_name=repo_full_name,
            from_tag=from_tag,
            to_tag=to_tag,
            entries=entries,
        )

    def render(self, notes: ReleaseNotes) -> str:
        """Render release notes as grouped markdown."""
        groups = {
            "breaking": ("Breaking Changes", "warning"),
            "security": ("Security Fixes", "critical"),
            "feature": ("New Features", "added"),
            "bugfix": ("Bug Fixes", "fixed"),
            "performance": ("Performance Improvements", "improved"),
            "docs": ("Documentation", "docs"),
            "dependency": ("Dependencies", "deps"),
            "refactor": ("Refactoring", "refactored"),
            "test": ("Tests", "test"),
            "ci/cd": ("CI / CD", "ci"),
            "other": ("Other Changes", "other"),
        }

        sections: list[str] = [
            f"# Release {notes.to_tag}",
            "",
            f"**Repository:** [{notes.repo_full_name}](https://github.com/{notes.repo_full_name})  ",
            f"**Range:** `{notes.from_tag}` → `{notes.to_tag}`  ",
            f"**Merged PRs:** {notes.total_prs}  ",
            "",
        ]

        for cat, (header, _icon) in groups.items():
            items = [e for e in notes.entries if e.category == cat]
            if not items:
                continue
            sections.append(f"## {header}")
            sections.append("")
            for e in sorted(items, key=lambda x: x.pr_number):
                breaking_mark = " **[BREAKING]**" if e.breaking_change else ""
                sections.append(
                    f"- {e.summary} (#{e.pr_number}) by @{e.author}{breaking_mark}"
                )
            sections.append("")

        sections.append(
            f"[Full Changelog](https://github.com/{notes.repo_full_name}"
            f"/compare/{notes.from_tag}...{notes.to_tag})"
        )
        sections.append("")
        sections.append("*Generated by RepoForge*")

        return "\n".join(sections)


# ---------------------------------------------------------------------------
# GitHub API helper
# ---------------------------------------------------------------------------

def fetch_merged_prs_between_tags(
    auth,
    installation_id: int,
    repo_full_name: str,
    from_tag: str,
    to_tag: str,
) -> list[dict]:
    """Fetch merged PRs between two tags using GitHub compare API.

    Returns list of PR dicts with number, title, body, user.login, merged_at.
    """
    try:
        gh = auth.get_github_client(installation_id)
        repo = gh.get_repo(repo_full_name)

        # Get comparison
        comparison = repo.compare(from_tag, to_tag)
        commits = comparison.commits

        # Extract unique PR numbers from commit messages
        pr_numbers: set[int] = set()
        merge_re = re.compile(r'Merge pull request #(\d+)')
        squash_re = re.compile(r'\(#(\d+)\)')
        for commit in commits:
            msg = commit.commit.message
            m = merge_re.search(msg)
            if m:
                pr_numbers.add(int(m.group(1)))
            else:
                m = squash_re.search(msg)
                if m:
                    pr_numbers.add(int(m.group(1)))

        # Fetch PR details
        prs: list[dict] = []
        for num in sorted(pr_numbers):
            try:
                pr = repo.get_pull(num)
                prs.append({
                    "number": pr.number,
                    "title": pr.title,
                    "body": pr.body or "",
                    "user": {"login": pr.user.login} if pr.user else {"login": "unknown"},
                    "merged_at": pr.merged_at.isoformat() if pr.merged_at else "",
                })
            except Exception:
                logger.debug("Failed to fetch PR #%d", num, exc_info=True)

        return prs

    except Exception:
        logger.exception("Failed to fetch PRs between tags")
        return []
