"""
memory/repo_memory.py

Per-repository persistent memory.  Accumulates path signals, validation
failure patterns, and issue outcomes across agent runs so that each run
builds on previous experience.

Design:
- One JSON file per repo under ~/.repoforge/memory/  (owner__repo.json)
- Atomic writes (tmp + rename) so crashes never corrupt state
- Rendered as compact natural-language text injected into the system prompt
  (~400-800 tokens) rather than raw JSON — LLMs parse prose more efficiently
"""

from __future__ import annotations

import difflib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PathSignal:
    """Per-file signal accumulated across runs."""
    path: str
    candidate_count: int = 0       # times this file was flagged as candidate
    changed_count: int = 0         # times actually edited
    successful_validation_count: int = 0  # times edits passed tests
    published_count: int = 0       # times edits resulted in merged PR
    last_seen_at: str = ""


@dataclass
class ValidationSignal:
    """A test/check command that has failed in the past."""
    command: str
    failure_count: int = 0
    last_exit_code: int = 0
    last_seen_at: str = ""
    sample_output: str = ""        # truncated first ~200 chars of output


@dataclass
class IssueOutcome:
    """Record of a single issue the agent attempted to fix."""
    reference: str                 # "owner/repo#NNN" or instance_id
    title: str
    status: str                    # drafted|patched|validated|pr_opened|merged|failed
    changed_files: list[str] = field(default_factory=list)
    validation_summary: str = ""
    pr_url: str = ""


@dataclass
class FileReviewSignal:
    """Per-file review signal, accumulated across all PR reviews in a repo."""
    file_path: str = ""
    critical_findings: int = 0
    high_findings: int = 0
    medium_findings: int = 0
    low_findings: int = 0
    review_count: int = 0
    last_reviewed_at: str = ""
    common_issues: list[str] = field(default_factory=list)  # max 5


@dataclass
class RepoMemory:
    """Complete per-repo memory, serialised as JSON."""
    repo_full_name: str
    first_seen_at: str = ""
    last_updated_at: str = ""
    detected_test_commands: list[str] = field(default_factory=list)   # max 12
    preferred_paths: list[str] = field(default_factory=list)          # max 12
    run_stats: dict = field(default_factory=lambda: {
        "total": 0, "published": 0, "real_pr": 0,
        "review_required": 0, "successful_validation": 0,
        "failed_validation": 0,
    })
    path_signals: list[PathSignal] = field(default_factory=list)      # max 50
    validation_signals: list[ValidationSignal] = field(default_factory=list)  # max 20
    recent_issues: list[IssueOutcome] = field(default_factory=list)   # max 10
    review_hotspots: list[FileReviewSignal] = field(default_factory=list)  # max 30


# ---------------------------------------------------------------------------
# Memory paths
# ---------------------------------------------------------------------------

def _memory_dir() -> Path:
    """Resolve the memory store root: ~/.repoforge/memory/"""
    home = os.environ.get("REPOFORGE_HOME", os.path.join(os.path.expanduser("~"), ".repoforge"))
    return Path(home) / "memory"


def _repo_key(repo_full_name: str) -> str:
    """Sanitise 'owner/repo' → 'owner__repo' for use as a filename."""
    return repo_full_name.replace("/", "__")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def default_memory(repo_full_name: str) -> RepoMemory:
    now = _now_iso()
    return RepoMemory(
        repo_full_name=repo_full_name,
        first_seen_at=now,
        last_updated_at=now,
    )


# ---------------------------------------------------------------------------
# Serialisation helpers  (dataclass → plain dicts)
# ---------------------------------------------------------------------------

def _signal_to_dict(s: PathSignal | ValidationSignal | IssueOutcome | FileReviewSignal) -> dict:
    if isinstance(s, PathSignal):
        return {"path": s.path, "candidate_count": s.candidate_count,
                "changed_count": s.changed_count,
                "successful_validation_count": s.successful_validation_count,
                "published_count": s.published_count, "last_seen_at": s.last_seen_at}
    if isinstance(s, ValidationSignal):
        return {"command": s.command, "failure_count": s.failure_count,
                "last_exit_code": s.last_exit_code, "last_seen_at": s.last_seen_at,
                "sample_output": s.sample_output}
    if isinstance(s, FileReviewSignal):
        return {"file_path": s.file_path, "critical_findings": s.critical_findings,
                "high_findings": s.high_findings, "medium_findings": s.medium_findings,
                "low_findings": s.low_findings, "review_count": s.review_count,
                "last_reviewed_at": s.last_reviewed_at,
                "common_issues": s.common_issues}
    # IssueOutcome
    return {"reference": s.reference, "title": s.title, "status": s.status,
            "changed_files": s.changed_files, "validation_summary": s.validation_summary,
            "pr_url": s.pr_url}


def _dict_to_signal(d: dict, cls: type):
    if cls is PathSignal:
        return PathSignal(path=d.get("path", ""), candidate_count=d.get("candidate_count", 0),
                          changed_count=d.get("changed_count", 0),
                          successful_validation_count=d.get("successful_validation_count", 0),
                          published_count=d.get("published_count", 0),
                          last_seen_at=d.get("last_seen_at", ""))
    if cls is ValidationSignal:
        return ValidationSignal(command=d.get("command", ""), failure_count=d.get("failure_count", 0),
                                last_exit_code=d.get("last_exit_code", 0),
                                last_seen_at=d.get("last_seen_at", ""),
                                sample_output=d.get("sample_output", ""))
    if cls is FileReviewSignal:
        return FileReviewSignal(
            file_path=d.get("file_path", ""),
            critical_findings=d.get("critical_findings", 0),
            high_findings=d.get("high_findings", 0),
            medium_findings=d.get("medium_findings", 0),
            low_findings=d.get("low_findings", 0),
            review_count=d.get("review_count", 0),
            last_reviewed_at=d.get("last_reviewed_at", ""),
            common_issues=d.get("common_issues", []),
        )
    # IssueOutcome
    return IssueOutcome(reference=d.get("reference", ""), title=d.get("title", ""),
                        status=d.get("status", "drafted"),
                        changed_files=d.get("changed_files", []),
                        validation_summary=d.get("validation_summary", ""),
                        pr_url=d.get("pr_url", ""))


def memory_to_dict(m: RepoMemory) -> dict:
    return {
        "repo_full_name": m.repo_full_name,
        "first_seen_at": m.first_seen_at,
        "last_updated_at": m.last_updated_at,
        "detected_test_commands": m.detected_test_commands,
        "preferred_paths": m.preferred_paths,
        "run_stats": m.run_stats,
        "path_signals": [_signal_to_dict(s) for s in m.path_signals],
        "validation_signals": [_signal_to_dict(s) for s in m.validation_signals],
        "recent_issues": [_signal_to_dict(s) for s in m.recent_issues],
        "review_hotspots": [_signal_to_dict(s) for s in m.review_hotspots],
    }


def memory_from_dict(d: dict) -> RepoMemory:
    return RepoMemory(
        repo_full_name=d.get("repo_full_name", ""),
        first_seen_at=d.get("first_seen_at", ""),
        last_updated_at=d.get("last_updated_at", ""),
        detected_test_commands=d.get("detected_test_commands", []),
        preferred_paths=d.get("preferred_paths", []),
        run_stats=d.get("run_stats", {"total": 0}),
        path_signals=[_dict_to_signal(p, PathSignal) for p in d.get("path_signals", [])],
        validation_signals=[_dict_to_signal(v, ValidationSignal) for v in d.get("validation_signals", [])],
        recent_issues=[_dict_to_signal(i, IssueOutcome) for i in d.get("recent_issues", [])],
        review_hotspots=[_dict_to_signal(r, FileReviewSignal) for r in d.get("review_hotspots", [])],
    )


# ---------------------------------------------------------------------------
# MemoryService
# ---------------------------------------------------------------------------

class MemoryService:
    """Load, mutate, and persist RepoMemory with atomic writes."""

    # ---- read ---------------------------------------------------------------

    def load(self, repo_full_name: str) -> RepoMemory:
        path = _memory_dir() / f"{_repo_key(repo_full_name)}.json"
        if not path.exists():
            return default_memory(repo_full_name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return _normalise(memory_from_dict(data), repo_full_name)
        except (json.JSONDecodeError, KeyError, TypeError):
            return default_memory(repo_full_name)

    def exists(self, repo_full_name: str) -> bool:
        path = _memory_dir() / f"{_repo_key(repo_full_name)}.json"
        return path.exists()

    # ---- write (atomic: tmp + rename) ---------------------------------------

    def save(self, memory: RepoMemory) -> None:
        memory.last_updated_at = _now_iso()
        dir_ = _memory_dir()
        dir_.mkdir(parents=True, exist_ok=True)
        target = dir_ / f"{_repo_key(memory.repo_full_name)}.json"
        tmp = dir_ / f"{target.name}.tmp.{os.getpid()}"
        try:
            tmp.write_text(
                json.dumps(memory_to_dict(memory), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, target)  # atomic on POSIX + Windows (Python ≥ 3.3)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    # ---- mutation helpers ----------------------------------------------------

    def bump_candidate_paths(self, memory: RepoMemory, paths: list[str]) -> None:
        """Called when a dossier / plan identifies candidate files."""
        now = _now_iso()
        existing = {s.path: s for s in memory.path_signals}
        for p in paths:
            p = _normalise_path(p)
            if not p:
                continue
            if p in existing:
                existing[p].candidate_count += 1
                existing[p].last_seen_at = now
            else:
                memory.path_signals.append(PathSignal(path=p, candidate_count=1, last_seen_at=now))
        # Cap at 50
        memory.path_signals.sort(key=lambda s: s.last_seen_at, reverse=True)
        memory.path_signals = memory.path_signals[:50]
        self._rebuild_preferred_paths(memory)

    def record_outcome(
        self,
        memory: RepoMemory,
        *,
        issue_ref: str,
        issue_title: str,
        changed_files: list[str],
        patch_produced: bool,
        validation_passed: bool = False,
        published: bool = False,
        pr_url: str = "",
        validation_summary: str = "",
    ) -> None:
        """Called after an agent run completes (with patch / PR result)."""
        now = _now_iso()
        stats = memory.run_stats
        stats["total"] += 1
        if published:
            stats["published"] += 1
        if pr_url:
            stats["real_pr"] += 1
        if patch_produced:
            stats["successful_validation"] += 1 if validation_passed else 0
            stats["failed_validation"] += 1 if not validation_passed else 0

        # Update path signals with changed files
        existing = {s.path: s for s in memory.path_signals}
        for f in changed_files:
            f = _normalise_path(f)
            if not f:
                continue
            if f in existing:
                sig = existing[f]
                sig.changed_count += 1
                if validation_passed:
                    sig.successful_validation_count += 1
                if published:
                    sig.published_count += 1
                sig.last_seen_at = now
            else:
                memory.path_signals.append(PathSignal(
                    path=f, changed_count=1,
                    successful_validation_count=1 if validation_passed else 0,
                    published_count=1 if published else 0,
                    last_seen_at=now,
                ))
        memory.path_signals.sort(key=lambda s: s.last_seen_at, reverse=True)
        memory.path_signals = memory.path_signals[:50]

        # Resolve status
        if published:
            status = "published"
        elif pr_url:
            status = "pr_opened"
        elif validation_passed and changed_files:
            status = "validated"
        elif patch_produced:
            status = "patched"
        elif not patch_produced:
            status = "failed"
        else:
            status = "drafted"

        # Prepend recent issue
        outcome = IssueOutcome(
            reference=issue_ref, title=issue_title, status=status,
            changed_files=changed_files,
            validation_summary=validation_summary,
            pr_url=pr_url,
        )
        # Remove duplicate reference and cap
        memory.recent_issues = [outcome] + [
            i for i in memory.recent_issues if i.reference != issue_ref
        ]
        memory.recent_issues = memory.recent_issues[:10]

        self._rebuild_preferred_paths(memory)

    def record_validation_failure(
        self, memory: RepoMemory, command: str, exit_code: int, output: str,
    ) -> None:
        """Record a test/validation failure signal."""
        now = _now_iso()
        existing = {s.command: s for s in memory.validation_signals}
        if command in existing:
            sig = existing[command]
            sig.failure_count += 1
            sig.last_exit_code = exit_code
            sig.last_seen_at = now
            if output.strip():
                sig.sample_output = output[:200]
        else:
            memory.validation_signals.append(ValidationSignal(
                command=command, failure_count=1, last_exit_code=exit_code,
                last_seen_at=now, sample_output=output[:200],
            ))
        memory.validation_signals.sort(key=lambda s: s.failure_count, reverse=True)
        memory.validation_signals = memory.validation_signals[:20]

    def update_review_signals(self, memory: RepoMemory,
                             findings: list,  # list of ReviewFinding-like objects
                             ) -> None:
        """Update review_hotspots from a set of review findings."""
        now = _now_iso()
        existing = {s.file_path: s for s in memory.review_hotspots}

        for f in findings:
            fp = _normalise_path(f.file_path)
            if not fp:
                continue
            if fp in existing:
                sig = existing[fp]
            else:
                sig = FileReviewSignal(file_path=fp)
                memory.review_hotspots.append(sig)
                existing[fp] = sig

            sig.review_count += 1
            sig.last_reviewed_at = now
            sev = getattr(f, 'severity', None)
            sev_str = sev.value if hasattr(sev, 'value') else str(sev or '')
            if sev_str == "CRITICAL":
                sig.critical_findings += 1
            elif sev_str == "HIGH":
                sig.high_findings += 1
            elif sev_str == "MEDIUM":
                sig.medium_findings += 1
            elif sev_str == "LOW":
                sig.low_findings += 1

            msg = getattr(f, 'message', '') or ''
            if msg and msg[:80] not in sig.common_issues:
                sig.common_issues.append(msg[:80])
                sig.common_issues = sig.common_issues[-5:]

        memory.review_hotspots.sort(
            key=lambda s: s.critical_findings * 100 + s.high_findings * 50
            + s.medium_findings * 10 + s.low_findings,
            reverse=True,
        )
        memory.review_hotspots = memory.review_hotspots[:30]

    # ---- prompt rendering ----------------------------------------------------

    def render_for_prompt(self, memory: RepoMemory,
                          task_description: str = "") -> str:
        """
        Render memory as compact natural-language text for injection into
        the system prompt.  Target ≈ 500-800 tokens.

        When *task_description* is provided, also includes:
        - Similar past issues ranked by title/summary similarity
        - Few-shot fix examples from validated/published outcomes
        - Hotspot-guided search suggestions based on path_signals
        """
        if memory.run_stats.get("total", 0) == 0:
            return ""

        lines = ["", "## Repository Memory", ""]

        # ---- task-aware: similar issue matching -------------------------------
        similar: list[tuple[IssueOutcome, float]] = []
        if task_description and memory.recent_issues:
            task_lower = task_description.lower()
            for ri in memory.recent_issues:
                ri_text = (ri.title + " " + ri.validation_summary).lower()
                score = difflib.SequenceMatcher(None, task_lower, ri_text).ratio()
                if score > 0.35:
                    similar.append((ri, score))
            similar.sort(key=lambda x: x[1], reverse=True)

        # ---- few-shot examples (from closely matched successful outcomes) -----
        if similar:
            successful = [(ri, s) for ri, s in similar
                          if ri.status in ("validated", "published", "pr_opened")
                          and ri.changed_files]
            if successful:
                lines.append("### Similar Past Fixes (few-shot examples)")
                lines.append(
                    "These issues were similar to the current task and were "
                    "successfully resolved.  Study the approach:"
                )
                for ri, score in successful[:2]:
                    lines.append(
                        f"  - {ri.reference} (similarity: {score:.0%}) [{ri.status}]: "
                        f"{ri.title[:100]}"
                    )
                    if ri.validation_summary:
                        lines.append(f"    Approach: {ri.validation_summary[:200]}")
                    lines.append(
                        f"    Files changed: {', '.join(ri.changed_files[:5])}"
                    )

        # ---- hotspot-guided search suggestions --------------------------------
        hot_paths = self._get_hotspot_paths(memory)
        if hot_paths:
            lines.append("### Search Hotspots")
            lines.append(
                "Previous fixes in this repo most frequently touched these paths. "
                "Start your investigation here:"
            )
            for p, weight in hot_paths[:6]:
                lines.append(f"  - {p} (relevance: {weight})")

        # ---- test commands ----------------------------------------------------
        if memory.detected_test_commands:
            lines.append("Known test commands: " +
                         ", ".join(memory.detected_test_commands[:6]))

        # ---- preferred paths (full detail) ------------------------------------
        if memory.preferred_paths:
            lines.append("Preferred paths (most frequently changed/published):")
            for p in memory.preferred_paths[:8]:
                sig = next((s for s in memory.path_signals if s.path == p), None)
                if sig:
                    lines.append(
                        f"  - {p} (candidates: {sig.candidate_count}, "
                        f"changed: {sig.changed_count}, validated: {sig.successful_validation_count}, "
                        f"published: {sig.published_count})"
                    )
                else:
                    lines.append(f"  - {p}")

        # ---- run stats --------------------------------------------------------
        s = memory.run_stats
        if s["total"] > 0:
            lines.append(
                f"Run history: {s['total']} runs, {s['published']} published, "
                f"{s['real_pr']} PRs opened, {s['successful_validation']} validations passed, "
                f"{s['failed_validation']} failed"
            )

        # ---- recent issues ----------------------------------------------------
        if memory.recent_issues and not task_description:
            # When task_description is provided, similar issues are shown above
            recent = memory.recent_issues[:5]
            lines.append("Recent issue outcomes:")
            for ri in recent:
                files_str = ", ".join(ri.changed_files[:3]) if ri.changed_files else "none"
                lines.append(f"  - {ri.reference} [{ri.status}]: {ri.title[:80]}")
                if ri.changed_files:
                    lines.append(f"    files: {files_str}")

        # ---- validation failure signals ---------------------------------------
        if memory.validation_signals:
            failures = [vs for vs in memory.validation_signals if vs.failure_count > 0]
            if failures:
                lines.append("Known validation failure patterns:")
                for vs in failures[:3]:
                    lines.append(
                        f"  - `{vs.command}` failed {vs.failure_count}x "
                        f"(last exit={vs.last_exit_code})"
                    )

        # ---- review hotspots --------------------------------------------------
        if memory.review_hotspots:
            hotspots = memory.review_hotspots[:5]
            lines.append("Review hotspots (files with most review findings):")
            for hs in hotspots:
                lines.append(
                    f"  - {hs.file_path} (critical: {hs.critical_findings}, "
                    f"high: {hs.high_findings}, reviews: {hs.review_count})"
                )

        return "\n".join(lines)

    # ---- hotspot computation --------------------------------------------------

    def _get_hotspot_paths(self, memory: RepoMemory) -> list[tuple[str, int]]:
        """Return paths ranked by a weighted score for search guidance.

        Higher weight = better candidate for initial investigation.
        Score: validated×3 + changed×2 + candidate×1
        """
        scored = [
            (s.successful_validation_count * 3 + s.changed_count * 2
             + s.candidate_count * 1, s.path)
            for s in memory.path_signals
            if s.path
        ]
        scored.sort(reverse=True)
        return [(path, score) for score, path in scored if score > 0]

    # ---- internal ------------------------------------------------------------

    def _rebuild_preferred_paths(self, memory: RepoMemory) -> None:
        """Sort paths by published (×14) > validated (×10) > changed (×6) > candidate (×1)."""
        scored = [
            (s.published_count * 14 + s.successful_validation_count * 10 +
             s.changed_count * 6 + s.candidate_count * 1, s.path)
            for s in memory.path_signals
        ]
        scored.sort(reverse=True)
        memory.preferred_paths = [p for _, p in scored[:12]]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _normalise_path(p: str) -> str:
    """Strip leading ./ / and backslashes, trim whitespace."""
    p = p.strip().replace("\\", "/")
    while p.startswith("./") or p.startswith("/"):
        p = p.lstrip("./").lstrip("/")
    return p or ""


def _normalise(memory: RepoMemory, repo_full_name: str) -> RepoMemory:
    """Ensure all fields have defaults after loading from disk (forwards compat)."""
    memory.repo_full_name = repo_full_name
    if not memory.first_seen_at:
        memory.first_seen_at = _now_iso()
    if not memory.last_updated_at:
        memory.last_updated_at = _now_iso()
    # Fill missing stats keys
    for key in ("total", "published", "real_pr", "review_required",
                "successful_validation", "failed_validation"):
        memory.run_stats.setdefault(key, 0)
    # Dedupe
    memory.detected_test_commands = list(dict.fromkeys(memory.detected_test_commands))
    memory.preferred_paths = list(dict.fromkeys(memory.preferred_paths))
    return memory


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

memory_service = MemoryService()
