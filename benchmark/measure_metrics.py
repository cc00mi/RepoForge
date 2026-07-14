"""
benchmark/measure_metrics.py

Concrete measurement for the 4 previously-unmeasurable success indicators.

Each metric uses a different data source:
  1. PR Review Coverage      -- scans ~/.repoforge/memory/ for ReviewMemory files
  2. Review Finding Recall   -- reuses existing prediction JSONL + SWE-bench instances
  3. Stale PR Reduction      -- runs StaleManager against realistic synthetic PRs
  4. Time to First Response  -- uses benchmark timing data + local review timing

Usage:
    python -m benchmark.measure_metrics           # all metrics (no LLM)
    python -m benchmark.measure_metrics --live    # include live review timing
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ============================================================================
# Helpers
# ============================================================================

def _pct(v: float) -> str:
    return f"{v * 100:.1f}%" if v else "0.0%"


def _sec(v: float) -> str:
    if v < 60:
        return f"{v:.0f}s"
    if v < 3600:
        return f"{v / 60:.1f}m"
    return f"{v / 3600:.1f}h"


# ============================================================================
# Metric 1: PR Review Coverage
# ============================================================================

def measure_coverage() -> dict:
    """Scan local ReviewMemory files and compute coverage.

    Coverage = reviewed PRs / total unique (repo, pr_number) pairs.
    In production, total comes from GitHub API; offline we count all
    ReviewMemory files found on disk.
    """
    from pipeline.review_memory import load_review_memory

    memory_dir = Path.home() / ".repoforge" / "memory"
    if not memory_dir.exists():
        return {
            "status": "no_data",
            "message": f"No memory directory found at {memory_dir}",
            "reviewed_prs": 0,
            "total_open_prs": "N/A (need GitHub API)",
            "coverage_ratio": 0.0,
            "repos": [],
        }

    # Parse all {owner}__{repo}__pr{N}.json files
    repo_prs: dict[str, list[int]] = {}
    reviewed_count = 0
    total_files = 0

    for f in sorted(memory_dir.glob("*__pr*.json")):
        total_files += 1
        try:
            # Filename: owner__repo__pr42.json
            stem = f.stem  # e.g., "owner__repo__pr42"
            # Split from the right: find the last "__pr"
            idx = stem.rfind("__pr")
            if idx == -1:
                continue
            repo_key = stem[:idx].replace("__", "/")
            pr_num = int(stem[idx + 3:])  # skip "__pr"

            if repo_key not in repo_prs:
                repo_prs[repo_key] = []
            repo_prs[repo_key].append(pr_num)

            # Check if this PR has a meaningful review
            mem = load_review_memory(repo_key, pr_num)
            if mem.review_count > 0 and any(s.total_count > 0 for s in mem.snapshots):
                reviewed_count += 1
        except (ValueError, Exception):
            pass

    repos_detail = []
    for repo, prs in repo_prs.items():
        repos_detail.append({
            "repo": repo,
            "prs_with_memory": sorted(prs),
            "count": len(prs),
        })

    return {
        "status": "ok",
        "memory_files_found": total_files,
        "unique_repos": len(repo_prs),
        "reviewed_prs": reviewed_count,
        "total_open_prs": f"N/A -- query via: gh api repos/OWNER/REPO/pulls?state=open",
        "coverage_ratio": reviewed_count / total_files if total_files else 0.0,
        "note": "total_open_prs requires GitHub API or manual input",
        "repos": repos_detail,
    }


# ============================================================================
# Metric 2: Review Finding Recall
# ============================================================================

def measure_recall() -> dict:
    """Estimate recall by comparing agent patch content against SWE-bench
    problem statements as a proxy for human ground truth.

    For each prediction JSONL entry:
      - Parse the problem_statement for file names and key terms
      - Parse the model_patch for changed files and fix patterns
      - Compute overlap ratio as a recall proxy

    This is a heuristic -- true recall requires structured human reviews.
    """
    import re

    pred_dir = _ROOT / "benchmark_results"
    pred_files = sorted(pred_dir.glob("predictions_*.jsonl"), reverse=True)
    if not pred_files:
        return {"status": "no_data", "message": "No prediction files found"}

    # Load SWE-bench instances for problem statements
    instances: dict[str, dict] = {}
    try:
        from benchmark.swe_bench import load_swebench_lite
        swe_instances = load_swebench_lite()
        for inst in swe_instances:
            instances[inst["instance_id"]] = inst
    except Exception:
        pass  # Can't load SWE-bench; continue with just patch analysis

    results: list[dict] = []
    total_recall = 0.0
    total_precision = 0.0
    count = 0

    for pf in pred_files[:3]:  # Analyze top 3 prediction files
        try:
            for line in pf.read_text(encoding="utf-8").strip().splitlines():
                if not line.strip():
                    continue
                pred = json.loads(line)
                instance_id = pred.get("instance_id", "")
                model_patch = pred.get("model_patch", "")

                if not model_patch:
                    continue

                # Extract changed files from patch
                patch_files = set(re.findall(
                    r"^diff --git a/(.+?) b/", model_patch, re.MULTILINE,
                ))

                # Extract human ground truth signals from problem statement
                inst = instances.get(instance_id, {})
                problem = inst.get("problem_statement", "")

                # Files mentioned in problem statement
                problem_files = set(re.findall(
                    r"`?([\w./-]+\.py)`?", problem,
                ))

                # Key technical terms from problem (words >= 5 chars, non-common)
                stop_words = {"which", "would", "there", "their", "about", "these",
                              "other", "should", "could", "after", "before", "where"}
                problem_terms = set(
                    w.lower() for w in re.findall(r"\b[a-zA-Z]{5,}\b", problem)
                    if w.lower() not in stop_words
                )
                # Terms in patch hunks (context lines + additions)
                patch_text = re.sub(r"^[+-]{3} .+", "", model_patch, flags=re.MULTILINE)
                patch_hunk_text = re.sub(r"^[-+@].*", "", patch_text, flags=re.MULTILINE)
                patch_terms = set(
                    w.lower() for w in re.findall(r"\b[a-zA-Z]{5,}\b", patch_hunk_text)
                    if w.lower() not in stop_words
                )

                # File-level recall: agent touched files mentioned in problem
                file_recall = (
                    len(patch_files & problem_files) / len(problem_files)
                    if problem_files else 0.5  # can't determine
                )

                # Term-level recall: agent patch contains terms from problem
                term_recall = (
                    len(patch_terms & problem_terms) / len(problem_terms)
                    if problem_terms else 0.5
                )

                # Combined proxy recall
                proxy_recall = 0.5 * file_recall + 0.5 * term_recall
                proxy_precision = (
                    len(patch_terms & problem_terms) / len(patch_terms)
                    if patch_terms else 0.0
                )

                results.append({
                    "instance_id": instance_id,
                    "files_in_problem": sorted(problem_files)[:10],
                    "files_in_patch": sorted(patch_files)[:10],
                    "file_recall": round(file_recall, 3),
                    "term_recall": round(term_recall, 3),
                    "proxy_recall": round(proxy_recall, 3),
                    "proxy_precision": round(proxy_precision, 3),
                })
                total_recall += proxy_recall
                total_precision += proxy_precision
                count += 1
        except Exception:
            pass

    if count == 0:
        return {"status": "no_data", "message": "Could not parse any predictions"}

    avg_recall = total_recall / count
    avg_precision = total_precision / count
    f1 = (2 * avg_precision * avg_recall / (avg_precision + avg_recall)
          if (avg_precision + avg_recall) > 0 else 0.0)

    return {
        "status": "ok",
        "method": "problem_statement <-> agent_patch overlap (proxy for human review)",
        "instances_analyzed": count,
        "avg_proxy_recall": round(avg_recall, 3),
        "avg_proxy_precision": round(avg_precision, 3),
        "proxy_f1": round(f1, 3),
        "caveat": "Proxy metric -- true recall requires structured human review comparison",
        "details": results[:10],
    }


# ============================================================================
# Metric 3: Stale PR Reduction
# ============================================================================

def measure_stale_reduction() -> dict:
    """Run StaleManager against realistic synthetic PR data and log scans.

    Simulates 3 scan rounds over 14 days with the stale manager running daily.
    """
    from pipeline.stale_manager import StaleManager, StalePolicy
    from pipeline.metrics import StaleMetricsLogger, StaleScanRecord

    repo = "measurement-test/repo"
    policy = StalePolicy(warn_after_days=7, stale_label_days=14, close_after_days=30)
    manager = StaleManager(policy)

    # Build 20 synthetic PRs with varied update times
    base_date = datetime(2026, 6, 15, tzinfo=timezone.utc)

    pr_templates = [
        # (number, days_inactive_at_start, exempt_labels)
        (1, 2, []),        # active -- no action
        (2, 5, []),        # active -- no action
        (3, 8, []),        # needs warn at scan 1
        (4, 9, []),        # needs warn at scan 1
        (5, 12, []),       # needs stale_label at scan 1
        (6, 14, []),       # needs stale_label at scan 1
        (7, 3, ["keep-open"]),  # exempt -- no action
        (8, 20, ["blocked"]),   # exempt -- no action
        (9, 10, []),       # needs stale_label at scan 1
        (10, 16, []),      # needs stale_label at scan 1
        (11, 6, []),       # active at scan 1, warn at scan 2
        (12, 7, []),       # warn at scan 1
        (13, 25, []),      # needs close at scan 1
        (14, 31, []),      # needs close at scan 1
        (15, 35, []),      # needs close at scan 1
        (16, 4, ["wip"]),  # exempt
        (17, 8, []),       # warn at scan 1
        (18, 1, []),       # active
        (19, 1, []),       # active
        (20, 1, []),       # active
    ]

    # Track which PRs were closed by stale manager in previous scans
    closed_pr_numbers: set[int] = set()
    # Track resolved PRs (author updated after warn)
    resolved_pr_numbers: set[int] = set()

    scans_results = []

    for scan_day in [0, 7, 14]:
        scan_date = base_date + timedelta(days=scan_day)

        open_prs = []
        for num, days_inactive_start, exempt in pr_templates:
            if num in closed_pr_numbers:
                continue  # was closed by stale manager in previous scan

            # Simulate author response: some warned PRs get updated
            effective_days = days_inactive_start
            if num in resolved_pr_numbers:
                effective_days = 2  # recently updated

            pr_date = scan_date - timedelta(days=effective_days)
            open_prs.append({
                "number": num,
                "title": f"PR #{num}: Test feature",
                "user": {"login": f"dev{num % 5 + 1}"},
                "labels": [{"name": lab} for lab in exempt],
                "updated_at": pr_date.isoformat(),
                "repo_full_name": repo,
            })

        actions = manager.scan_prs(open_prs, now=scan_date)

        # Simulate stale manager execution: close PRs that got "close" action
        for a in actions:
            if a.action == "close":
                closed_pr_numbers.add(a.pr_number)

        # Simulate author response to warns: some PRs get updated before next scan
        if scan_day == 0:
            warned = [a.pr_number for a in actions if a.action == "warn"]
            resolved_pr_numbers.update(warned[:2])  # 2 authors respond

        scan_record = StaleScanRecord(
            repo_full_name=repo,
            total_scanned=len(open_prs),
            exempt_count=sum(1 for pr in open_prs if pr.get("labels")),
            stale_count=len(actions),
            warn_count=sum(1 for a in actions if a.action == "warn"),
            label_count=sum(1 for a in actions if a.action == "label_stale"),
            close_count=sum(1 for a in actions if a.action == "close"),
            dry_run=False,
        )
        StaleMetricsLogger.log_scan(scan_record)
        scans_results.append({
            "day": scan_day,
            "date": scan_date.strftime("%Y-%m-%d"),
            "total_scanned": scan_record.total_scanned,
            "stale_count": scan_record.stale_count,
            "warn_count": scan_record.warn_count,
            "label_count": scan_record.label_count,
            "close_count": scan_record.close_count,
        })

    # Compute reduction
    reduction = StaleMetricsLogger.compute_reduction(repo)

    return {
        "status": "ok",
        "method": "Simulated 3 scans over 14 days with 20 synthetic PRs",
        "scan_results": scans_results,
        "first_scan_stale": reduction.first_scan_stale if reduction else 0,
        "latest_scan_stale": reduction.latest_scan_stale if reduction else 0,
        "absolute_reduction": reduction.absolute_reduction if reduction else 0,
        "reduction_pct": reduction.reduction_pct if reduction else 0.0,
        "target": ">= 50%",
        "meets_target": (reduction.reduction_pct >= 50.0) if reduction else False,
    }


# ============================================================================
# Metric 4: Time to First Response (TTR)
# ============================================================================

def measure_ttr() -> dict:
    """Measure TTR from two sources:

    1. Existing benchmark report data (agent run duration from past runs)
    2. Metrics JSONL files on disk
    3. Optionally: live local review timing (with --live flag)

    The TTR recorded here measures from webhook receipt to first agent comment.
    In production, the webhook server records this in ~/.repoforge/metrics/ttr_log.jsonl
    """
    from pipeline.metrics import TTRTracker, _metrics_dir, _read_jsonl

    # Source 1: existing TTR JSONL records
    metrics_dir = _metrics_dir()
    ttr_path = metrics_dir / "ttr_log.jsonl"
    ttr_records = _read_jsonl(ttr_path) if ttr_path.exists() else []

    # Source 2: benchmark report data (agent run durations)
    report_dir = _ROOT / "benchmark_results"
    report_files = sorted(report_dir.glob("report_*.json"), reverse=True)
    benchmark_ttrs: list[float] = []
    if report_files:
        try:
            report = json.loads(report_files[0].read_text(encoding="utf-8"))
            for inst in report.get("instances", []):
                if inst.get("status") == "completed":
                    benchmark_ttrs.append(inst.get("elapsed_seconds", 0))
        except Exception:
            pass

    # Compute TTR stats from recorded JSONL
    ttr_stats = TTRTracker.compute_stats(window_hours=9999)  # all time

    # Combine benchmark elapsed times as TTR proxy
    # (agent run duration ≈ time from webhook to first substantive response)
    combined = list(benchmark_ttrs)
    for r in ttr_records:
        combined.append(float(r.get("ttr_seconds", 0)))

    if not combined:
        return {
            "status": "no_production_data",
            "method": "Run benchmark or deploy webhook server to collect TTR data",
            "ttr_from_metrics_jsonl": ttr_stats,
            "how_to_collect": (
                "1. Deploy: repoforge-pipe serve -> webhook events -> auto-recorded to ttr_log.jsonl\n"
                "2. Benchmark: python -m benchmark.swe_bench -> agent elapsed = TTR proxy\n"
                "3. Local:    python -m benchmark.measure_metrics --live"
            ),
        }

    combined.sort()
    n = len(combined)
    import statistics

    return {
        "status": "ok",
        "samples": n,
        "median_seconds": round(statistics.median(combined), 1),
        "mean_seconds": round(statistics.mean(combined), 1),
        "p95_seconds": round(combined[min(int(n * 0.95), n - 1)], 1),
        "min_seconds": round(combined[0], 1),
        "max_seconds": round(combined[-1], 1),
        "below_5min_count": sum(1 for t in combined if t <= 300),
        "below_5min_pct": round(sum(1 for t in combined if t <= 300) / n * 100, 1),
        "ttr_from_metrics_jsonl": ttr_stats,
        "benchmark_elapsed_samples": len(benchmark_ttrs),
        "target": "<= 300s (5 min)",
        "meets_target": statistics.median(combined) <= 300,
    }


def _measure_live_ttr() -> dict:
    """Run a real local review and time the end-to-end latency.

    Uses the local astropy repo if available.
    """
    astropy_dir = _ROOT / "benchmark_repos" / "astropy__astropy"
    if not (astropy_dir / ".git").exists():
        return {"status": "skipped", "reason": "astropy repo not available"}

    # Find two different commits to diff
    try:
        log = subprocess.run(
            ["git", "-C", str(astropy_dir), "log", "--oneline", "-n", "20"],
            capture_output=True, text=True,
        )
        lines = log.stdout.strip().split("\n")
        if len(lines) < 2:
            return {"status": "skipped", "reason": "not enough commits in repo"}
        head_commit = lines[0].split()[0]
        base_commit = lines[5].split()[0] if len(lines) > 5 else lines[-1].split()[0]
    except Exception:
        return {"status": "skipped", "reason": "git log failed"}

    print(f"\n  Running live review on astropy__astropy...")
    print(f"  Diff: {base_commit[:8]}..{head_commit[:8]}")

    from pipeline.review import run_review
    from config.schema import load_config

    config = load_config()
    t0 = time.time()
    try:
        report = run_review(
            repo_dir=str(astropy_dir),
            base=base_commit,
            head=head_commit,
        )
        elapsed = time.time() - t0
        return {
            "status": "ok",
            "elapsed_seconds": round(elapsed, 1),
            "findings_count": report.total_count,
            "critical": report.critical_count,
            "high": report.high_count,
            "base_commit": base_commit,
            "head_commit": head_commit,
        }
    except Exception as e:
        elapsed = time.time() - t0
        return {
            "status": "failed",
            "elapsed_seconds": round(elapsed, 1),
            "error": str(e)[:200],
        }


# ============================================================================
# Report
# ============================================================================

@dataclass
class MetricsReport:
    coverage: dict = field(default_factory=dict)
    recall: dict = field(default_factory=dict)
    stale: dict = field(default_factory=dict)
    ttr: dict = field(default_factory=dict)
    live_ttr: dict | None = None

    def to_markdown(self) -> str:
        c = self.coverage
        r = self.recall
        s = self.stale
        t = self.ttr

        lines = [
            "# RepoForge -- Measurable Metrics Report",
            "",
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "---",
            "",
            "## 1. PR Review Coverage",
            "",
            f"- **Status:** {c.get('status')}",
            f"- **Reviewed PRs:** {c.get('reviewed_prs', 0)}",
            f"- **Memory files found:** {c.get('memory_files_found', 0)}",
            f"- **Unique repos:** {c.get('unique_repos', 0)}",
            f"- **Coverage ratio:** {_pct(c.get('coverage_ratio', 0))}",
            f"- **Total open PRs:** {c.get('total_open_prs', 'N/A')}",
            f"- **Target:** 100% -- {c.get('note', '')}",
            "",
        ]

        if c.get("repos"):
            lines.append("| Repo | PRs with Memory |")
            lines.append("|------|-----------------|")
            for repo_info in c["repos"]:
                pr_list = ", ".join(str(p) for p in repo_info["prs_with_memory"][:5])
                if len(repo_info["prs_with_memory"]) > 5:
                    pr_list += f" (+{len(repo_info['prs_with_memory']) - 5} more)"
                lines.append(f"| {repo_info['repo']} | {pr_list} |")
            lines.append("")

        lines += [
            "---",
            "",
            "## 2. Review Finding Recall (Proxy)",
            "",
            f"- **Status:** {r.get('status')}",
            f"- **Method:** {r.get('method', '')}",
            f"- **Instances analyzed:** {r.get('instances_analyzed', 0)}",
            f"- **Avg proxy recall:** {_pct(r.get('avg_proxy_recall', 0))}",
            f"- **Avg proxy precision:** {_pct(r.get('avg_proxy_precision', 0))}",
            f"- **Proxy F1:** {r.get('proxy_f1', 0):.3f}",
            f"- **Target:** >= 80% recall vs human",
            f"- **Caveat:** {r.get('caveat', '')}",
            "",
        ]

        if r.get("details"):
            lines.append("| Instance | File Recall | Term Recall | Proxy Recall | Files in Problem |")
            lines.append("|----------|-------------|-------------|--------------|------------------|")
            for d in r["details"]:
                files = ", ".join(d["files_in_problem"][:3])
                if len(d["files_in_problem"]) > 3:
                    files += f" +{len(d['files_in_problem']) - 3}"
                lines.append(
                    f"| {d['instance_id'][:40]} | {_pct(d['file_recall'])} | "
                    f"{_pct(d['term_recall'])} | {_pct(d['proxy_recall'])} | {files} |"
                )
            lines.append("")

        lines += [
            "---",
            "",
            "## 3. Stale PR Reduction",
            "",
            f"- **Status:** {s.get('status')}",
            f"- **Method:** {s.get('method', '')}",
            f"- **First scan stale count:** {s.get('first_scan_stale', 0)}",
            f"- **Latest scan stale count:** {s.get('latest_scan_stale', 0)}",
            f"- **Absolute reduction:** {s.get('absolute_reduction', 0)} PRs",
            f"- **Reduction %:** {s.get('reduction_pct', 0):.1f}%",
            f"- **Target:** >= 50% -- {'MET' if s.get('meets_target') else 'NOT MET'}",
            "",
        ]

        if s.get("scan_results"):
            lines.append("| Day | Date | Total | Stale | Warn | Label | Close |")
            lines.append("|-----|------|-------|-------|------|-------|-------|")
            for scan in s["scan_results"]:
                lines.append(
                    f"| {scan['day']} | {scan['date']} | {scan['total_scanned']} | "
                    f"{scan['stale_count']} | {scan['warn_count']} | "
                    f"{scan['label_count']} | {scan['close_count']} |"
                )
            lines.append("")

        lines += [
            "---",
            "",
            "## 4. Time to First Response",
            "",
            f"- **Status:** {t.get('status')}",
            f"- **Samples:** {t.get('samples', 0)}",
            f"- **Median TTR:** {_sec(t.get('median_seconds', 0))}",
            f"- **Mean TTR:** {_sec(t.get('mean_seconds', 0))}",
            f"- **P95 TTR:** {_sec(t.get('p95_seconds', 0))}",
            f"- **Below 5min:** {t.get('below_5min_count', 0)}/{t.get('samples', 0)} ({t.get('below_5min_pct', 0):.1f}%)",
            f"- **Target:** <= 300s (5 min) -- {'MET' if t.get('meets_target') else 'NOT MET'}",
            "",
        ]

        if self.live_ttr:
            lt = self.live_ttr
            lines += [
                "### Live Review Timing",
                "",
                f"- **Status:** {lt.get('status')}",
                f"- **Elapsed:** {_sec(lt.get('elapsed_seconds', 0))}",
                f"- **Findings:** {lt.get('findings_count', 0)} ({lt.get('critical', 0)} critical, {lt.get('high', 0)} high)",
            ]
            if lt.get("error"):
                lines.append(f"- **Error:** {lt['error']}")
            lines.append("")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "coverage": self.coverage,
            "recall": self.recall,
            "stale": self.stale,
            "ttr": self.ttr,
            "live_ttr": self.live_ttr,
        }


# ============================================================================
# Main
# ============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Measure RepoForge success metrics")
    parser.add_argument("--live", action="store_true",
                        help="Run a live review for TTR measurement (requires LLM API)")
    parser.add_argument("--save", action="store_true",
                        help="Save report to benchmark_results/")
    args = parser.parse_args()

    print("=" * 60)
    print("  RepoForge -- Measurable Metrics")
    print("=" * 60)
    print()

    # 1. Coverage
    print("[1/4] Measuring PR Review Coverage ...")
    coverage = measure_coverage()
    print(f"      Found {coverage.get('memory_files_found', 0)} memory files "
          f"across {coverage.get('unique_repos', 0)} repos")
    print(f"      Reviewed PRs: {coverage.get('reviewed_prs', 0)}, "
          f"Coverage: {_pct(coverage.get('coverage_ratio', 0))}")

    # 2. Recall
    print("[2/4] Measuring Review Finding Recall (proxy) ...")
    recall = measure_recall()
    if recall["status"] == "ok":
        print(f"      Proxy recall: {_pct(recall.get('avg_proxy_recall', 0))} "
              f"across {recall.get('instances_analyzed', 0)} instances")
        print(f"      (method: problem_statement <-> agent_patch overlap)")
    else:
        print(f"      {recall.get('message', recall['status'])}")

    # 3. Stale reduction
    print("[3/4] Measuring Stale PR Reduction ...")
    stale = measure_stale_reduction()
    print(f"      Reduction: {stale.get('first_scan_stale', 0)} -> "
          f"{stale.get('latest_scan_stale', 0)} stale PRs "
          f"({stale.get('reduction_pct', 0):.1f}%)")

    # 4. TTR
    print("[4/4] Measuring Time to First Response ...")
    ttr = measure_ttr()
    if ttr["status"] == "ok":
        print(f"      Median: {_sec(ttr.get('median_seconds', 0))}, "
              f"Below 5min: {ttr.get('below_5min_pct', 0):.1f}% "
              f"({ttr.get('samples', 0)} samples)")
    else:
        print(f"      {ttr.get('how_to_collect', ttr['status'])}")

    # Live TTR (optional)
    live_result = None
    if args.live:
        print()
        print("[*] Running live review timing (requires LLM API call) ...")
        live_result = _measure_live_ttr()
        if live_result["status"] == "ok":
            print(f"      Live TTR: {_sec(live_result['elapsed_seconds'])} "
                  f"({live_result['findings_count']} findings)")
        else:
            print(f"      {live_result['status']}: {live_result.get('reason', live_result.get('error', ''))}")

    # Report
    report = MetricsReport(
        coverage=coverage,
        recall=recall,
        stale=stale,
        ttr=ttr,
        live_ttr=live_result,
    )

    print()
    print(report.to_markdown())

    if args.save:
        output_path = _ROOT / "benchmark_results" / "measurement_report.md"
        output_path.write_text(report.to_markdown(), encoding="utf-8")
        json_path = _ROOT / "benchmark_results" / "measurement_report.json"
        json_path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"\nReport saved: {output_path}")
        print(f"JSON saved:  {json_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
