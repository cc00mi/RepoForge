"""
pipeline/review_memory.py

Per-PR review state that accumulates across synchronize events.

Design:
- ReviewMemory: one JSON file per PR under ~/.repoforge/memory/
  (owner__repo__pr{N}.json) — tracks review rounds and whether previous
  findings were addressed.
- FileReviewSignal lives in memory/repo_memory.py alongside RepoMemory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ReviewSnapshot:
    """One round of review on a PR."""
    reviewed_at: str = ""
    head_sha: str = ""
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    total_count: int = 0
    summary: str = ""
    resolution: str = "pending"  # pending | addressed | dismissed


@dataclass
class ReviewMemory:
    """Per-PR review state that accumulates across synchronize events."""
    pr_number: int = 0
    repo_full_name: str = ""
    last_review_commit: str = ""
    review_count: int = 0
    snapshots: list[ReviewSnapshot] = field(default_factory=list)  # max 20


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _memory_dir() -> Path:
    home = os.environ.get("REPOFORGE_HOME", os.path.join(os.path.expanduser("~"), ".repoforge"))
    return Path(home) / "memory"


def _repo_key(repo_full_name: str) -> str:
    return repo_full_name.replace("/", "__")


def _pr_key(repo_full_name: str, pr_number: int) -> str:
    return f"{_repo_key(repo_full_name)}__pr{pr_number}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def default_review_memory(repo_full_name: str, pr_number: int) -> ReviewMemory:
    return ReviewMemory(repo_full_name=repo_full_name, pr_number=pr_number)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _snapshot_to_dict(s: ReviewSnapshot) -> dict:
    return {
        "reviewed_at": s.reviewed_at, "head_sha": s.head_sha,
        "critical_count": s.critical_count, "high_count": s.high_count,
        "medium_count": s.medium_count, "low_count": s.low_count,
        "total_count": s.total_count, "summary": s.summary,
        "resolution": s.resolution,
    }


def _dict_to_snapshot(d: dict) -> ReviewSnapshot:
    return ReviewSnapshot(
        reviewed_at=d.get("reviewed_at", ""), head_sha=d.get("head_sha", ""),
        critical_count=d.get("critical_count", 0), high_count=d.get("high_count", 0),
        medium_count=d.get("medium_count", 0), low_count=d.get("low_count", 0),
        total_count=d.get("total_count", 0), summary=d.get("summary", ""),
        resolution=d.get("resolution", "pending"),
    )


def review_memory_to_dict(m: ReviewMemory) -> dict:
    return {
        "pr_number": m.pr_number, "repo_full_name": m.repo_full_name,
        "last_review_commit": m.last_review_commit, "review_count": m.review_count,
        "snapshots": [_snapshot_to_dict(s) for s in m.snapshots],
    }


def review_memory_from_dict(d: dict) -> ReviewMemory:
    return ReviewMemory(
        pr_number=d.get("pr_number", 0), repo_full_name=d.get("repo_full_name", ""),
        last_review_commit=d.get("last_review_commit", ""),
        review_count=d.get("review_count", 0),
        snapshots=[_dict_to_snapshot(s) for s in d.get("snapshots", [])],
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_review_memory(repo_full_name: str, pr_number: int) -> ReviewMemory:
    path = _memory_dir() / f"{_pr_key(repo_full_name, pr_number)}.json"
    if not path.exists():
        return default_review_memory(repo_full_name, pr_number)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return review_memory_from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return default_review_memory(repo_full_name, pr_number)


def save_review_memory(memory: ReviewMemory) -> None:
    dir_ = _memory_dir()
    dir_.mkdir(parents=True, exist_ok=True)
    target = dir_ / f"{_pr_key(memory.repo_full_name, memory.pr_number)}.json"
    tmp = dir_ / f"{target.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(
            json.dumps(review_memory_to_dict(memory), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Mutation helpers
# ---------------------------------------------------------------------------

def record_review_snapshot(
    memory: ReviewMemory,
    *,
    head_sha: str,
    critical_count: int,
    high_count: int,
    medium_count: int = 0,
    low_count: int = 0,
    total_count: int = 0,
    summary: str = "",
) -> None:
    """Append a review round snapshot and update tracking fields."""
    snapshot = ReviewSnapshot(
        reviewed_at=_now_iso(),
        head_sha=head_sha,
        critical_count=critical_count,
        high_count=high_count,
        medium_count=medium_count,
        low_count=low_count,
        total_count=total_count,
        summary=summary[:500],
        resolution="pending",
    )
    memory.snapshots.append(snapshot)
    memory.review_count += 1
    memory.last_review_commit = head_sha
    if len(memory.snapshots) > 20:
        memory.snapshots = memory.snapshots[-20:]


def mark_previous_findings_addressed(memory: ReviewMemory) -> int:
    """Mark all pending findings from previous review rounds as addressed."""
    count = 0
    for s in memory.snapshots:
        if s.resolution == "pending":
            s.resolution = "addressed"
            count += 1
    return count


def previous_critical_count(memory: ReviewMemory) -> int:
    """Return total critical findings from the last review round."""
    if not memory.snapshots:
        return 0
    return memory.snapshots[-1].critical_count


def previous_high_count(memory: ReviewMemory) -> int:
    """Return total high findings from the last review round."""
    if not memory.snapshots:
        return 0
    return memory.snapshots[-1].high_count
