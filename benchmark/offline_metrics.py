"""
benchmark/offline_metrics.py

Offline system metrics — measures everything that doesn't require LLM API calls.
Generates a comprehensive report aligned with the target metrics table.

Usage:
    python -m benchmark.offline_metrics
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ============================================================================
# Synthetic test datasets
# ============================================================================

# (title, body, expected_classification, expected_priority, expected_effort)
_CLASSIFICATION_DATASET = [
    # --- bugs ---
    ("Fix crash on startup",
     "The application crashes with NullPointerException when starting without "
     "a config file. Steps to reproduce: 1. Remove config.yaml 2. Run app 3. See crash.",
     "bug", "p0", "medium"),
    ("Broken login flow",
     "Users cannot log in after the last deploy. The login button shows an error "
     "page. This is a regression from the previous working version.",
     "bug", "p1", "medium"),
    ("Error when saving file",
     "File save throws an exception intermittently. Hard to reproduce but happens "
     "about 30% of the time.",
     "bug", "p2", "medium"),
    ("Incorrect calculation in tax report",
     "The tax calculation returns wrong values for users in California. This is "
     "a critical data integrity issue affecting financial reports.",
     "bug", "p0", "medium"),
    ("Minor typo in footer text",
     "The footer says 'Copyrigt' instead of 'Copyright'. Just a cosmetic issue.",
     "bug", "p3", "trivial"),

    # --- security ---
    ("XSS vulnerability in comment form",
     "User input in the comment form is not sanitized, allowing script injection. "
     "This is a critical security issue that needs immediate attention.",
     "security", "p0", "medium"),
    ("API key leaked in error logs",
     "The application logs contain the full API key in error messages. This is "
     "a credential leak that must be fixed urgently.",
     "security", "p0", "medium"),

    # --- enhancement ---
    ("Add dark mode support",
     "It would be nice to have a dark theme option. Many users have requested this "
     "feature.",
     "enhancement", "p2", "medium"),
    ("Feature request: export to CSV",
     "Users want to export their data as CSV files. This is a common feature "
     "request among enterprise users.",
     "enhancement", "p2", "medium"),

    # --- docs ---
    ("Typo in README",
     "There is a spelling mistake in the documentation. The word 'instalation' "
     "should be 'installation'.",
     "docs", "p3", "trivial"),
    ("Documentation for API is missing",
     "The /v2/export endpoint is not documented anywhere. Users need this to "
     "integrate with the new export functionality.",
     "docs", "p2", "medium"),

    # --- question ---
    ("How do I configure the server?",
     "I can't figure out how to set the PORT environment variable. The "
     "documentation doesn't explain this clearly.",
     "question", "p2", "medium"),

    # --- performance ---
    ("Memory leak in worker process",
     "The background worker process consumes increasing memory over time. After "
     "24 hours it uses 8GB RAM. This is a critical performance issue.",
     "performance", "p0", "large"),
    ("App is very slow after last deploy",
     "Latency increased 5x after the latest deployment. Response times went from "
     "50ms to 250ms on average.",
     "performance", "p2", "medium"),

    # --- dependencies ---
    ("Bump requests to 2.28",
     "Dependabot has opened an automatic PR to upgrade the requests package from "
     "version 2.25 to 2.28.",
     "dependencies", "p2", "trivial"),

    # --- edge cases ---
    ("Something is wrong",
     "I'm not sure what the problem is but things aren't working right.",
     "bug", "p2", "medium"),  # fallback → bug
    ("",
     "",
     "bug", "p2", "medium"),  # empty → fallback
]


# (new_title, new_body, past_title, past_body, should_match)
_DEDUP_DATASET = [
    # Exact / near-exact duplicates (should match)
    ("Fix crash on startup", "",
     "Fix crash on startup", "",
     True, 0.95),
    ("App crashes on startup", "",
     "Fix crash on startup", "",
     True, 0.50),  # similar title words

    # Rephrased duplicates — different words, same bug (should match)
    ("Incorrect button styling on phone screens",
     "The login button shows wrong color on mobile devices.",
     "Login button shows wrong color on mobile devices",
     "Fixed CSS media query for button color on screen widths under 768px.",
     True, 0.40),
    ("Application fails to initialize without YAML configuration",
     "When starting without a config file, it crashes with NullPointerException.",
     "App crashes on startup when config file is missing",
     "Added default values and null checks in the config loader.",
     True, 0.40),

    # Completely unrelated (should NOT match)
    ("Fix crash on startup", "",
     "Add dark mode support", "",
     False, 0.40),
    ("Memory leak in worker", "",
     "Update README typo", "",
     False, 0.40),
    ("How do I configure the server?", "",
     "Fix button color on mobile", "",
     False, 0.40),
    ("XSS vulnerability in login", "",
     "Bump requests to 2.28", "",
     False, 0.40),
    ("Fix login button color",
     "The login button has wrong CSS color",
     "Add GraphQL API endpoint",
     "New GraphQL endpoint for user data with proper schema design.",
     False, 0.40),
]


# ============================================================================
# Metrics collector
# ============================================================================

@dataclass
class MetricsReport:
    """Comprehensive offline metrics report."""

    # Codebase stats
    total_py_files: int = 0
    total_lines: int = 0
    test_files: int = 0
    test_count: int = 0

    # Triage classification
    triage_accuracy: float = 0.0
    triage_priority_accuracy: float = 0.0
    triage_effort_accuracy: float = 0.0
    triage_samples: int = 0
    triage_errors: list[dict] = field(default_factory=list)

    # Dedup
    dedup_precision: float = 0.0
    dedup_recall: float = 0.0
    dedup_f1: float = 0.0
    dedup_samples: int = 0

    # Repo Memory
    repo_memory_hit_rate: float = 0.0

    # Review coverage (offline synth)
    review_coverage: float = 0.0

    # Finding recall (offline synth)
    finding_recall: float = 0.0
    finding_precision: float = 0.0
    finding_f1: float = 0.0

    # Stale reduction (offline synth)
    stale_reduction_pct: float = 0.0

    # TTR (offline synth)
    ttr_median_seconds: float = 0.0
    ttr_below_5min_pct: float = 0.0

    # Timing
    test_suite_time_seconds: float = 0.0

    def to_table(self) -> str:
        """Render as a markdown-compatible table aligned with target metrics."""
        def _pct(v: float) -> str:
            return f"{v:.1%}" if v else "N/A"
        def _num(v: float, suffix: str = "") -> str:
            return f"{v:,.0f}{suffix}" if v else "N/A"

        return f"""\
| 指标                          | 当前实测           | Phase 1-3 目标   | Phase 4-10 目标      |
| ----------------------------- | ------------------ | ---------------- | -------------------- |
| SWE-bench resolved rate       | 需 API 实测        | ≥ 5%             | ≥ 10%                |
| Avg steps / instance          | 需 API 实测        | ≤ 15 (complete)  | ≤ 12                 |
| Tokens / instance             | 需 API 实测        | ≤ 150k           | ≤ 100k               |
| Triage classification acc.    | {_pct(self.triage_accuracy)}             | —                | ≥ 70%                |
| Triage priority acc.          | {_pct(self.triage_priority_accuracy)}             | —                | —                    |
| Triage effort acc.            | {_pct(self.triage_effort_accuracy)}             | —                | —                    |
| Dedup precision               | {_pct(self.dedup_precision)}             | —                | ≥ 90% (不误关)       |
| Dedup recall                  | {_pct(self.dedup_recall)}             | —                | —                    |
| Dedup F1                      | {self.dedup_f1:.3f}               | —                | —                    |
| Repo Memory hit rate          | {_pct(self.repo_memory_hit_rate)}             | ≥ 30%            | ≥ 60%                |
| PR review coverage (offline)  | {_pct(self.review_coverage)}             | —                | 100% PR 被 review    |
| Review finding recall (offln) | {_pct(self.finding_recall)}             | —                | ≥ 80% vs human       |
| Review finding precision      | {_pct(self.finding_precision)}             | —                | —                    |
| Review finding F1 (offline)   | {self.finding_f1:.3f}               | —                | —                    |
| Stale PR reduction (offline)  | {_pct(self.stale_reduction_pct / 100 if self.stale_reduction_pct else 0)}             | —                | ≥ 50% stale PR 减少  |
| TTR median (offline synth)    | {self.ttr_median_seconds:.0f}s              | —                | ≤ 300s (5 min)       |
| TTR below 5min rate (offline) | {_pct(self.ttr_below_5min_pct / 100 if self.ttr_below_5min_pct else 0)}             | —                | —                    |
| Test count                    | {self.test_count}                  | —                | —                    |
| Codebase lines                | {_num(self.total_lines)}           | —                | —                    |
| Python files                  | {self.total_py_files}              | —                | —                    |
| Test suite time               | {self.test_suite_time_seconds:.0f}s               | —                | —                    |
"""

    def to_dict(self) -> dict:
        return {
            "triage_accuracy": self.triage_accuracy,
            "triage_priority_accuracy": self.triage_priority_accuracy,
            "triage_effort_accuracy": self.triage_effort_accuracy,
            "triage_samples": self.triage_samples,
            "dedup_precision": self.dedup_precision,
            "dedup_recall": self.dedup_recall,
            "dedup_f1": self.dedup_f1,
            "dedup_samples": self.dedup_samples,
            "repo_memory_hit_rate": self.repo_memory_hit_rate,
            "review_coverage": self.review_coverage,
            "finding_recall": self.finding_recall,
            "finding_precision": self.finding_precision,
            "finding_f1": self.finding_f1,
            "stale_reduction_pct": self.stale_reduction_pct,
            "ttr_median_seconds": self.ttr_median_seconds,
            "ttr_below_5min_pct": self.ttr_below_5min_pct,
            "total_py_files": self.total_py_files,
            "total_lines": self.total_lines,
            "test_count": self.test_count,
            "test_suite_time_seconds": self.test_suite_time_seconds,
        }


# ============================================================================
# Measurement functions
# ============================================================================

def _measure_codebase() -> tuple[int, int, int, int]:
    """Count Python files, total lines, test files, test functions."""
    root = _ROOT
    py_files = list(root.rglob("*.py"))
    # Exclude __pycache__, .pytest_cache, benchmark_repos, etc.
    py_files = [f for f in py_files
                if "__pycache__" not in str(f)
                and ".pytest_cache" not in str(f)
                and "benchmark_repos" not in str(f)
                and "benchmark_results" not in str(f)]

    total_lines = 0
    for f in py_files:
        try:
            total_lines += len(f.read_text(encoding="utf-8", errors="ignore").splitlines())
        except Exception:
            pass

    test_files = [f for f in py_files if f.name.startswith("test_") or "tests" in str(f.parent).split("\\")[-1]]

    test_count = 0
    for f in test_files:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
            test_count += content.count("def test_")
        except Exception:
            pass

    return len(py_files), total_lines, len(test_files), test_count


def _measure_triage() -> tuple[float, float, float, int, list[dict]]:
    """Measure classification accuracy on synthetic dataset."""
    from pipeline.triage import TriageEngine

    engine = TriageEngine()
    correct_type = 0
    correct_priority = 0
    correct_effort = 0
    errors = []
    total = len(_CLASSIFICATION_DATASET)

    for title, body, exp_type, exp_priority, exp_effort in _CLASSIFICATION_DATASET:
        result = engine.classify(title, body)
        type_ok = result.classification == exp_type
        pri_ok = result.priority == exp_priority
        eff_ok = result.effort == exp_effort
        if type_ok:
            correct_type += 1
        if pri_ok:
            correct_priority += 1
        if eff_ok:
            correct_effort += 1
        if not type_ok:
            errors.append({
                "title": title[:60],
                "expected": exp_type,
                "got": result.classification,
                "confidence": result.confidence,
            })
    return (
        correct_type / total,
        correct_priority / total,
        correct_effort / total,
        total,
        errors,
    )


def _measure_dedup() -> tuple[float, float, float, int]:
    """Measure dedup precision and recall on synthetic dataset."""
    from pipeline.triage import TriageEngine

    engine = TriageEngine()
    tp = fp = fn = tn = 0

    for new_title, new_body, past_title, past_body, should_match, threshold in _DEDUP_DATASET:
        recent = [{
            "reference": "owner/repo#1",
            "title": past_title,
            "validation_summary": past_body,
        }]
        dups = engine.check_duplicates(new_title, new_body, recent)
        found = len(dups) > 0

        if should_match and found:
            tp += 1
        elif should_match and not found:
            fn += 1
        elif not should_match and found:
            fp += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return precision, recall, f1, len(_DEDUP_DATASET)


def _measure_repo_memory_hit_rate() -> float:
    """Measure RepoMemory hit rate on synthetic issue matching.

    Simulates: given an issue description, can RepoMemory find relevant
    past fixes? Uses render_for_prompt() with task_description to check
    if similar past issues are retrieved.
    """
    from memory.repo_memory import (
        RepoMemory, MemoryService,
        IssueOutcome, PathSignal,
    )

    mem = RepoMemory(repo_full_name="benchmark/test")
    svc = MemoryService()

    # Seed memory with some past outcomes
    mem.recent_issues = [
        IssueOutcome(
            reference="owner/repo#1",
            title="Fix crash on startup when config is missing",
            status="validated",
            validation_summary="Added null check in ConfigLoader.load() and default values.",
            changed_files=["src/config.py", "src/loader.py"],
        ),
        IssueOutcome(
            reference="owner/repo#2",
            title="Add dark mode support to settings",
            status="published",
            validation_summary="Added CSS variables and theme toggle in settings.",
            changed_files=["src/theme.css", "src/settings.py", "src/toggle.js"],
        ),
        IssueOutcome(
            reference="owner/repo#3",
            title="Fix login button color on mobile devices",
            status="validated",
            validation_summary="Updated CSS media query for screen widths under 768px.",
            changed_files=["src/login.css"],
        ),
    ]

    # run_stats must have total > 0 for render_for_prompt to produce output
    mem.run_stats = {"total": 3, "published": 1, "real_pr": 0,
                     "review_required": 0, "successful_validation": 2,
                     "failed_validation": 0}

    # Hotspot path signals
    mem.path_signals = [
        PathSignal(path="src/config.py", successful_validation_count=3,
                    changed_count=5, candidate_count=2),
        PathSignal(path="src/login.css", successful_validation_count=1,
                    changed_count=2, candidate_count=0),
        PathSignal(path="src/theme.css", successful_validation_count=2,
                    changed_count=3, candidate_count=1),
    ]

    # Test: does render_for_prompt surface relevant past fixes?
    queries = [
        ("config crash at startup", True),   # should match issue #1
        ("mobile button color is wrong", True),  # should match issue #3
        ("add csv export feature", False),    # no match expected
        ("dark theme support", True),         # should match issue #2
    ]

    hits = 0
    for task_desc, should_hit in queries:
        rendered = svc.render_for_prompt(mem, task_description=task_desc)
        has_similar = "Similar Past Fixes" in rendered
        if has_similar == should_hit:
            hits += 1

    return hits / len(queries) if queries else 0.0


def _measure_coverage_offline() -> float:
    """Measure PR review coverage using synthetic ReviewMemory data.

    Creates ReviewMemory files for some PRs, then calls compute_coverage
    to verify the ratio is computed correctly.
    """
    from pipeline.metrics import compute_coverage
    from pipeline.review_memory import (
        ReviewMemory, ReviewSnapshot, save_review_memory,
    )

    repo = "benchmark/test-coverage"
    reviewed_prs = [1, 3, 5]
    unreviewed_prs = [2, 4]

    # Write ReviewMemory for "reviewed" PRs
    for pr_num in reviewed_prs:
        mem = ReviewMemory(pr_number=pr_num, repo_full_name=repo, review_count=1)
        mem.snapshots.append(ReviewSnapshot(
            head_sha="abc123",
            critical_count=0,
            high_count=2,
            medium_count=3,
            low_count=1,
            total_count=6,
            summary="Test review snapshot for PR #{}".format(pr_num),
        ))
        try:
            save_review_memory(mem)
        except Exception:
            pass

    all_prs = reviewed_prs + unreviewed_prs
    snapshot = compute_coverage(repo, all_prs)
    return snapshot.coverage_ratio


def _measure_recall_offline() -> tuple[float, float, float]:
    """Measure review finding recall on synthetic paired data.

    Creates agent and human findings for the same PR, then runs
    compute_recall to verify matching accuracy.
    """
    from pipeline.metrics import (
        AgentFindingsRecord, HumanFindingsRecord, FindingStore,
    )

    repo = "benchmark/test-recall"
    pr = 42

    # Synthetic agent findings
    FindingStore.record_agent_findings(AgentFindingsRecord(
        pr_number=pr, repo_full_name=repo, head_sha="abc",
        critical_count=0, high_count=3, medium_count=2, total_count=5,
        findings=[
            {"severity": "HIGH", "file_path": "src/auth.py", "line": 42,
             "message": "Missing null check on user input"},
            {"severity": "HIGH", "file_path": "src/auth.py", "line": 78,
             "message": "Hardcoded secret key should use env var"},
            {"severity": "HIGH", "file_path": "src/api.py", "line": 15,
             "message": "No rate limiting on login endpoint"},
            {"severity": "MEDIUM", "file_path": "src/utils.py", "line": 200,
             "message": "Use f-string instead of format()"},
            {"severity": "MEDIUM", "file_path": "tests/test_auth.py", "line": 10,
             "message": "Missing test for edge case"},
        ],
    ))

    # Synthetic human findings (3 match agent, 2 are unique)
    FindingStore.record_human_findings(HumanFindingsRecord(
        pr_number=pr, repo_full_name=repo, reviewer="maintainer1",
        review_state="changes_requested", critical_count=0, high_count=3,
        total_comments=5,
        findings=[
            {"severity": "HIGH", "file_path": "src/auth.py", "line": 42,
             "message": "Missing null check for user input parameter"},
            {"severity": "HIGH", "file_path": "src/auth.py", "line": 78,
             "message": "Secret key must come from environment variable not hardcoded"},
            {"severity": "MEDIUM", "file_path": "src/utils.py", "line": 200,
             "message": "Prefer f-strings for readability"},
            {"severity": "HIGH", "file_path": "src/db.py", "line": 55,
             "message": "SQL injection risk in raw query"},
            {"severity": "MEDIUM", "file_path": "README.md", "line": 0,
             "message": "Missing deployment instructions"},
        ],
    ))

    result = FindingStore.compute_recall(repo, pr)
    if result is None:
        return 0.0, 0.0, 0.0
    return result.recall, result.precision, result.f1


def _measure_stale_offline() -> float:
    """Measure stale PR reduction on two synthetic scan rounds.

    Simulates a before/after scan and verifies the reduction computation.
    """
    from pipeline.metrics import StaleMetricsLogger, StaleScanRecord

    repo = "benchmark/test-stale"

    # Scan 1: 12 stale PRs
    StaleMetricsLogger.log_scan(StaleScanRecord(
        repo_full_name=repo, total_scanned=25, exempt_count=3,
        stale_count=12, warn_count=5, label_count=4, close_count=3,
        dry_run=False,
        timestamp="2026-07-01T00:00:00+00:00",
    ))

    # Scan 2: 7 stale PRs (reduction from deployment of stale manager)
    StaleMetricsLogger.log_scan(StaleScanRecord(
        repo_full_name=repo, total_scanned=22, exempt_count=2,
        stale_count=7, warn_count=3, label_count=2, close_count=2,
        dry_run=False,
        timestamp="2026-07-08T00:00:00+00:00",
    ))

    result = StaleMetricsLogger.compute_reduction(repo)
    if result is None:
        return 0.0
    return result.reduction_pct


def _measure_ttr_offline() -> tuple[float, float]:
    """Measure TTR on synthetic receipt/response pairs.

    Simulates several webhook receipt → first comment cycles and
    verifies median/percentile computation.
    """
    from pipeline.metrics import TTRTracker
    import time as _time

    # Simulate: receipt → wait → response
    sims = [
        ("pull_request", "benchmark/test-ttr", 1, 45),     # 45s
        ("pull_request", "benchmark/test-ttr", 2, 180),    # 3min
        ("issues", "benchmark/test-ttr", 10, 12),          # 12s
        ("pull_request", "benchmark/test-ttr", 3, 310),    # 5min 10s
        ("issues", "benchmark/test-ttr", 11, 240),         # 4min
    ]

    for event_type, repo, num, delay in sims:
        TTRTracker.record_receipt(repo, num, event_type)
        # Simulate delay by manipulating the pending dict's timestamp
        key = (repo, num)
        if key in TTRTracker._pending:
            from datetime import datetime, timezone, timedelta
            fake_received = datetime.now(timezone.utc) - timedelta(seconds=delay)
            TTRTracker._pending[key]["received_at"] = fake_received.isoformat()
        TTRTracker.record_response(repo, num, "comment")

    stats = TTRTracker.compute_stats(window_hours=1)
    return stats.get("median_seconds", 0), stats.get("below_5min_pct", 0)


# ============================================================================
# Main
# ============================================================================

def run_all() -> MetricsReport:
    """Run all offline metrics and return a report."""
    report = MetricsReport()

    # 1. Codebase stats
    print("Measuring codebase stats ...")
    report.total_py_files, report.total_lines, report.test_files, report.test_count = \
        _measure_codebase()

    # 2. Triage classification
    print("Measuring triage classification accuracy ...")
    (report.triage_accuracy,
     report.triage_priority_accuracy,
     report.triage_effort_accuracy,
     report.triage_samples,
     report.triage_errors) = _measure_triage()

    # 3. Dedup metrics
    print("Measuring dedup precision/recall ...")
    (report.dedup_precision,
     report.dedup_recall,
     report.dedup_f1,
     report.dedup_samples) = _measure_dedup()

    # 4. Repo Memory hit rate
    print("Measuring Repo Memory hit rate ...")
    report.repo_memory_hit_rate = _measure_repo_memory_hit_rate()

    # 5. PR review coverage (offline synth)
    print("Measuring PR review coverage (synthetic) ...")
    report.review_coverage = _measure_coverage_offline()

    # 6. Finding recall (offline synth)
    print("Measuring review finding recall (synthetic) ...")
    (report.finding_recall,
     report.finding_precision,
     report.finding_f1) = _measure_recall_offline()

    # 7. Stale PR reduction (offline synth)
    print("Measuring stale PR reduction (synthetic) ...")
    report.stale_reduction_pct = _measure_stale_offline()

    # 8. TTR (offline synth)
    print("Measuring TTR response latency (synthetic) ...")
    (report.ttr_median_seconds,
     report.ttr_below_5min_pct) = _measure_ttr_offline()

    # 9. Test suite timing
    print("Running test suite (timing) ...")
    t0 = time.time()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=120,
    )
    report.test_suite_time_seconds = time.time() - t0

    return report


def main():
    print("=" * 60)
    print("  RepoForge — Offline System Metrics")
    print("=" * 60)
    print()

    report = run_all()

    print()
    print(report.to_table())

    # Classification errors detail
    if report.triage_errors:
        print()
        print("Classification errors:")
        for e in report.triage_errors:
            print(f"  '{e['title']}' → expected {e['expected']}, got {e['got']} "
                  f"(confidence: {e['confidence']:.2f})")

    # Save JSON report
    output_path = _ROOT / "benchmark_results" / "offline_metrics.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nReport saved: {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
