"""
pipeline/metrics.py

Metrics collection, storage, and computation for the 4 previously-unmeasurable
success indicators from the design document (§6):

  1. PR Review Coverage      — reviewed PRs / total open PRs
  2. Review Finding Recall   — agent vs human review overlap
  3. Stale PR Reduction      — before/after stale counts over time
  4. Time to First Response  — webhook receipt → first comment delta

All storage uses append-only JSONL (matching agent/event_log.py pattern).
Computations read from disk on demand (matching pipeline/dashboard.py pattern).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _metrics_dir() -> Path:
    """Return the metrics storage directory (~/.repoforge/metrics/)."""
    base = os.environ.get("REPOFORGE_HOME", os.path.expanduser("~/.repoforge"))
    return Path(base) / "metrics"


def _ensure_metrics_dir() -> Path:
    d = _metrics_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_jsonl(dir_path: Path, filename: str, record: dict) -> None:
    """Append a single JSON record as one line to a JSONL file."""
    dir_path.mkdir(parents=True, exist_ok=True)
    fpath = dir_path / filename
    try:
        with open(fpath, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.exception("Failed to write metrics record to %s", fpath)


def _read_jsonl(path: Path) -> list[dict]:
    """Read all records from a JSONL file. Returns [] if file missing."""
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        logger.exception("Failed to read metrics from %s", path)
    return records


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ======================================================================
# Subsystem 1: PR Review Coverage
# ======================================================================

@dataclass
class CoverageSnapshot:
    """Coverage ratio for a single repo at a point in time."""
    repo_full_name: str = ""
    total_open_prs: int = 0
    reviewed_prs: int = 0
    coverage_ratio: float = 0.0
    unreviewed_prs: list[int] = field(default_factory=list)
    computed_at: str = ""


def compute_coverage(
    repo_full_name: str,
    open_pr_numbers: list[int],
) -> CoverageSnapshot:
    """Cross-reference GitHub open PRs against local ReviewMemory files.

    A PR is considered "reviewed" if its ReviewMemory file exists, has
    review_count > 0, and at least one snapshot has total_count > 0.
    """
    from pipeline.review_memory import load_review_memory

    reviewed: list[int] = []
    unreviewed: list[int] = []

    for n in open_pr_numbers:
        try:
            mem = load_review_memory(repo_full_name, n)
            if mem.review_count > 0 and any(s.total_count > 0 for s in mem.snapshots):
                reviewed.append(n)
            else:
                unreviewed.append(n)
        except Exception:
            unreviewed.append(n)

    total = len(open_pr_numbers)
    ratio = len(reviewed) / total if total > 0 else 0.0

    return CoverageSnapshot(
        repo_full_name=repo_full_name,
        total_open_prs=total,
        reviewed_prs=len(reviewed),
        coverage_ratio=round(ratio, 4),
        unreviewed_prs=unreviewed,
        computed_at=_now_iso(),
    )


# ======================================================================
# Subsystem 2: Review Finding Recall
# ======================================================================

@dataclass
class AgentFindingsRecord:
    pr_number: int = 0
    repo_full_name: str = ""
    recorded_at: str = ""
    head_sha: str = ""
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    total_count: int = 0
    findings: list[dict] = field(default_factory=list)


@dataclass
class HumanFindingsRecord:
    pr_number: int = 0
    repo_full_name: str = ""
    recorded_at: str = ""
    reviewer: str = ""
    review_state: str = ""
    critical_count: int = 0
    high_count: int = 0
    total_comments: int = 0
    findings: list[dict] = field(default_factory=list)


@dataclass
class RecallResult:
    pr_number: int = 0
    repo_full_name: str = ""
    agent_total: int = 0
    human_total: int = 0
    true_positives: int = 0
    false_negatives: int = 0
    false_positives: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    matched_pairs: list[dict] = field(default_factory=list)
    unmatched_agent: list[dict] = field(default_factory=list)
    unmatched_human: list[dict] = field(default_factory=list)


def _tokenize(text: str) -> set[str]:
    """Extract significant lowercase words from text, dropping short tokens."""
    import re
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
    return set(words)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _match_findings(
    agent_list: list[dict],
    human_list: list[dict],
    threshold: float = 0.12,
) -> tuple[int, int, int, list[dict]]:
    """Match agent findings against human (ground-truth) findings.

    Uses a composite similarity score:
      - Jaccard on message tokens (excluding file paths)
      - Line number proximity bonus (±5 lines)
      - Severity agreement bonus

    Returns: (true_positives, false_negatives, false_positives, matched_pairs)
    """
    import re
    matched_human: set[int] = set()
    matched_agent: set[int] = set()
    pairs: list[dict] = []

    for hi, hf in enumerate(human_list):
        h_file = (hf.get("file_path") or hf.get("file") or "").replace("\\", "/")
        h_line = int(hf.get("line") or 0)
        h_sev = hf.get("severity", "")
        h_msg = hf.get("message", "")
        # Strip file path tokens from message before tokenizing
        h_msg_clean = _clean_message_for_tokenize(h_msg)
        h_tokens = _tokenize(h_msg_clean)

        best_score = -1.0
        best_ai = -1
        for ai, af in enumerate(agent_list):
            if ai in matched_agent:
                continue
            a_file = (af.get("file_path") or af.get("file") or "").replace("\\", "/")
            a_line = int(af.get("line") or 0)
            a_sev = af.get("severity", "")
            a_msg = af.get("message", "")
            a_msg_clean = _clean_message_for_tokenize(a_msg)

            # File match: exact path or same filename stem
            file_match = bool(
                h_file and a_file
                and (h_file == a_file or Path(h_file).name == Path(a_file).name)
            )
            if not file_match:
                continue

            # --- Composite score ---
            score = 0.0

            # 1. Jaccard similarity on message tokens (weight: 0.5)
            msg_jaccard = _jaccard(h_tokens, _tokenize(a_msg_clean))
            score += 0.5 * msg_jaccard

            # 2. Line proximity bonus (weight: 0.3)
            if h_line > 0 and a_line > 0:
                line_diff = abs(h_line - a_line)
                if line_diff == 0:
                    score += 0.30
                elif line_diff <= 3:
                    score += 0.20
                elif line_diff <= 8:
                    score += 0.10
                elif line_diff <= 15:
                    score += 0.05
            elif h_line == 0 or a_line == 0:
                score += 0.05  # one side has no line info — neutral bonus

            # 3. Severity agreement bonus (weight: 0.2)
            if h_sev and a_sev:
                sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "SUGGESTION": 4}
                sev_diff = abs(sev_order.get(h_sev, 2) - sev_order.get(a_sev, 2))
                if sev_diff == 0:
                    score += 0.20
                elif sev_diff == 1:
                    score += 0.10

            if score > best_score:
                best_score = score
                best_ai = ai

        if best_score >= threshold and best_ai >= 0:
            matched_human.add(hi)
            matched_agent.add(best_ai)
            pairs.append({
                "human_finding": hf,
                "agent_finding": agent_list[best_ai],
                "score": round(best_score, 3),
            })

    tp = len(matched_human)
    fn = len(human_list) - tp
    fp = len(agent_list) - len(matched_agent)

    unmatched_agent = [af for ai, af in enumerate(agent_list) if ai not in matched_agent]
    unmatched_human = [hf for hi, hf in enumerate(human_list) if hi not in matched_human]

    return tp, fn, fp, pairs, unmatched_agent, unmatched_human


def _clean_message_for_tokenize(msg: str) -> str:
    """Remove file paths and line numbers from message before tokenization.

    This prevents file names like 'test_review_target.py' from diluting the
    Jaccard similarity when they're part of the message text.
    """
    import re
    # Remove common file path patterns
    msg = re.sub(r"`?[\w./\-]+\.py`?(?:\s*:\s*\d+)?", "", msg)
    # Remove markdown formatting
    msg = re.sub(r"\*{1,3}|\_{1,3}|`", "", msg)
    return msg


def _extract_human_findings(
    review_body: str,
    inline_comments: list[dict],
) -> list[dict]:
    """Extract structured findings from a human review.

    Parses review body text and inline comments for file references,
    severity signals, and actionable items.  Uses markdown section headings
    to infer severity context.
    """
    import re
    findings: list[dict] = []

    # Parse inline comments (already have structured file/line data)
    for c in inline_comments:
        msg = c.get("body", "")
        if not msg or len(msg) < 10:
            continue
        findings.append({
            "file_path": c.get("path", ""),
            "line": c.get("line") or c.get("original_line", 0),
            "message": msg[:300],
            "severity": _infer_severity(msg),
        })

    if not review_body:
        return findings

    # ---- Split review body into sections -----------------------------------
    # Match markdown headings: ### HEADING or ## HEADING
    section_re = re.compile(r"^(#{2,4})\s+(.+)$", re.MULTILINE)
    sections = list(section_re.finditer(review_body))

    # Build list of (heading_text, content_start, content_end) sections
    parsed_sections: list[tuple[str, int, int]] = []
    for i, m in enumerate(sections):
        heading = m.group(2).strip()
        content_start = m.end()
        content_end = sections[i + 1].start() if i + 1 < len(sections) else len(review_body)
        parsed_sections.append((heading, content_start, content_end))

    # If no markdown headings, treat entire body as one section
    if not parsed_sections:
        parsed_sections = [("", 0, len(review_body))]

    # ---- Extract findings per section --------------------------------------
    file_line_re = re.compile(
        r"(?:in|at|file)\s+`?([\w./\-]+)`?(?:\s*:\s*|#L?)(\d+)",
        re.IGNORECASE,
    )

    for heading, sec_start, sec_end in parsed_sections:
        section_text = review_body[sec_start:sec_end]
        heading_severity = _infer_severity(heading) if heading else ""

        # Try file:line references first
        for m in file_line_re.finditer(section_text):
            file_path = m.group(1)
            line_num = int(m.group(2))
            # Extract the paragraph around this reference, NOT including the file:line
            para_start = max(0, m.start() - 60)
            para_end = min(len(section_text), m.end() + 200)
            para = section_text[para_start:para_end]

            # Clean message: remove the file:line reference itself and leading markup
            msg = re.sub(
                r"\*?\*?.*?(?:in|at|file)\s+`?[\w./\-]+`?\s*:?\s*\d+\*?\*?\s*[-:]*\s*",
                "", para, count=1, flags=re.IGNORECASE,
            ).strip()
            # Strip markdown formatting
            msg = re.sub(r"\*{1,3}|\_{1,3}", "", msg).strip()
            if len(msg) < 8:
                msg = para[:200]  # fallback

            findings.append({
                "file_path": file_path,
                "line": line_num,
                "message": msg[:300],
                "severity": _infer_severity(
                    heading + " " + section_text[m.start():m.end() + 150],
                    heading_context=heading,
                ),
            })

        # Bullet / numbered items in this section
        bullet_re = re.compile(r"(?:^|\n)\s*(?:[-*]|\d+[.)])\s+(.+)")
        bullets = list(bullet_re.finditer(section_text))

        # If no file:line refs found, use bullets as standalone findings
        if not any(
            m.start() >= sec_start and m.start() < sec_end
            for m in file_line_re.finditer(review_body)
            if sec_start <= m.start() < sec_end
        ):
            for bm in bullets:
                msg = bm.group(1).strip()
                if len(msg) < 15:
                    continue
                # Try to find file:line in the bullet text
                fl_match = file_line_re.search(msg)
                fp = fl_match.group(1) if fl_match else ""
                ln = int(fl_match.group(2)) if fl_match else 0
                if fl_match:
                    msg_clean = re.sub(
                        r"\*?\*?.*?(?:in|at|file)\s+`?[\w./\-]+`?\s*:?\s*\d+\*?\*?\s*[-:]*\s*",
                        "", msg, count=1, flags=re.IGNORECASE,
                    ).strip()
                    msg_clean = re.sub(r"\*{1,3}|\_{1,3}", "", msg_clean).strip()
                    if len(msg_clean) > 8:
                        msg = msg_clean
                findings.append({
                    "file_path": fp,
                    "line": ln,
                    "message": msg[:300],
                    "severity": _infer_severity(heading + " " + msg, heading_context=heading),
                })

    # ---- Global fallback if nothing found ----------------------------------
    if not findings:
        bullet_re = re.compile(r"(?:^|\n)\s*(?:[-*]|\d+[.)])\s+(.+)")
        for bm in bullet_re.finditer(review_body):
            msg = bm.group(1).strip()
            if len(msg) > 15:
                findings.append({
                    "file_path": "",
                    "line": 0,
                    "message": msg[:300],
                    "severity": _infer_severity(msg),
                })

    return findings


_SEVERITY_PATTERNS: list[tuple[str, list[str]]] = [
    ("CRITICAL", [
        "critical", "security", "vulnerability", "crash", "data loss",
        "urgent", "blocker", "emergency", "injection", "secret",
        "credential", "leak", "exposed", "rce", "traversal",
        "overflow", "exploit", "bypass", "xss", "csrf", "hardcoded",
        "api key", "access token", "password", "authentication bypass",
        "privilege escalation", "remote code execution",
    ]),
    ("HIGH", [
        "bug", "broken", "wrong", "incorrect", "error", "fail",
        "memory leak", "race condition", "deadlock", "zero division",
        "null pointer", "type error", "logic error", "regression",
        "infinite loop", "resource leak", "dead code",
    ]),
    ("MEDIUM", [
        "refactor", "improve", "performance", "style", "naming",
        "duplicate", "unused", "cleanup", "simplify", "complex",
        "readability", "maintainability", "consistency",
        "missing type", "annotation", "missing doc", "test coverage",
        "o(n", "linear search",
    ]),
    ("LOW", [
        "nit", "typo", "whitespace", "spacing", "cosmetic",
        "comment", "doc", "minor", "formatting", "pep",
        "linting", "indentation", "trailing", "blank line",
    ]),
]


def _infer_severity(text: str, heading_context: str = "") -> str:
    """Infer severity from text using scoring + optional heading context.

    Uses a scoring approach: each keyword match adds to the severity score.
    Heading context (from the nearest markdown heading) gets bonus weight.
    Returns the severity with the highest score.
    """
    t = text.lower()
    scores: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}

    # Score from the text itself
    for severity, keywords in _SEVERITY_PATTERNS:
        for kw in keywords:
            if kw in t:
                scores[severity] += 1

    # Bonus from heading context (e.g., "### CRITICAL Issues")
    if heading_context:
        h = heading_context.lower()
        for sev in ["critical", "high", "medium", "low"]:
            if sev in h:
                scores[sev.upper()] += 3  # Heading is a strong signal

    # Pick highest score; break ties toward higher severity
    best_severity = "MEDIUM"
    best_score = -1
    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
        if scores[sev] > best_score:
            best_score = scores[sev]
            best_severity = sev

    return best_severity


def _extract_surrounding_sentence(text: str, pos: int, radius: int = 120) -> str:
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    snippet = text[start:end].strip()
    # Try to break at sentence boundaries
    for sep in (". ", "\n"):
        if sep in snippet:
            parts = snippet.split(sep)
            if len(parts) >= 2:
                return (parts[-2] + ".").strip()
    return snippet[:200]


class FindingStore:
    """Append-only JSONL storage for structured review findings.

    Agent findings are recorded when the agent completes a PR review.
    Human findings are recorded when a maintainer submits a review.
    Recall/precision/F1 are computed offline by cross-referencing both.
    """

    @staticmethod
    def record_agent_findings(record: AgentFindingsRecord) -> None:
        d = _ensure_metrics_dir()
        FindingStore._write(d, "agent_findings.jsonl", record)

    @staticmethod
    def record_human_findings(record: HumanFindingsRecord) -> None:
        d = _ensure_metrics_dir()
        FindingStore._write(d, "human_findings.jsonl", record)

    @staticmethod
    def compute_recall(repo_full_name: str, pr_number: int) -> RecallResult | None:
        """Compute recall for a specific PR. Returns None if no paired data."""
        d = _metrics_dir()
        agent_records = _read_jsonl(d / "agent_findings.jsonl")
        human_records = _read_jsonl(d / "human_findings.jsonl")

        # Filter for this PR
        agent_findings = [
            r.get("findings", []) for r in agent_records
            if r.get("repo_full_name") == repo_full_name
            and r.get("pr_number") == pr_number
        ]
        human_findings = [
            r.get("findings", []) for r in human_records
            if r.get("repo_full_name") == repo_full_name
            and r.get("pr_number") == pr_number
        ]

        if not agent_findings or not human_findings:
            return None

        # Use latest record for each side
        agent_list = agent_findings[-1]
        human_list = human_findings[-1]

        tp, fn, fp, pairs, unmatched_agent, unmatched_human = _match_findings(
            agent_list, human_list,
        )

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        return RecallResult(
            pr_number=pr_number,
            repo_full_name=repo_full_name,
            agent_total=len(agent_list),
            human_total=len(human_list),
            true_positives=tp,
            false_negatives=fn,
            false_positives=fp,
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1=round(f1, 4),
            matched_pairs=pairs,
            unmatched_agent=unmatched_agent,
            unmatched_human=unmatched_human,
        )

    @staticmethod
    def aggregate_recall() -> dict:
        """Aggregate recall across all PRs with paired data. Returns summary dict."""
        d = _metrics_dir()
        agent_records = _read_jsonl(d / "agent_findings.jsonl")
        human_records = _read_jsonl(d / "human_findings.jsonl")

        # Find PRs with both agent and human findings
        human_prs = {(r.get("repo_full_name"), r.get("pr_number"))
                     for r in human_records}
        agent_prs = {(r.get("repo_full_name"), r.get("pr_number"))
                     for r in agent_records}
        paired_prs = human_prs & agent_prs

        total_tp = 0
        total_fn = 0
        total_fp = 0
        results: list[dict] = []

        for repo, pr in paired_prs:
            result = FindingStore.compute_recall(repo, pr)
            if result:
                total_tp += result.true_positives
                total_fn += result.false_negatives
                total_fp += result.false_positives
                results.append({
                    "pr_number": pr,
                    "repo": repo,
                    "recall": result.recall,
                    "precision": result.precision,
                    "f1": result.f1,
                })

        if not results:
            return {
                "paired_prs": 0,
                "avg_recall": 0.0,
                "avg_precision": 0.0,
                "avg_f1": 0.0,
                "status": "insufficient_data",
                "details": [],
            }

        avg_recall = sum(r["recall"] for r in results) / len(results)
        avg_precision = sum(r["precision"] for r in results) / len(results)
        agg_f1 = (2 * avg_precision * avg_recall / (avg_precision + avg_recall)
                  if (avg_precision + avg_recall) > 0 else 0.0)

        return {
            "paired_prs": len(results),
            "avg_recall": round(avg_recall, 4),
            "avg_precision": round(avg_precision, 4),
            "avg_f1": round(agg_f1, 4),
            "total_tp": total_tp,
            "total_fn": total_fn,
            "total_fp": total_fp,
            "status": "ok" if len(results) >= 3 else "insufficient_data",
            "details": results,
        }

    @staticmethod
    def _write(d: Path, filename: str, record) -> None:
        data = record.__dict__.copy() if hasattr(record, "__dict__") else dict(record)
        _append_jsonl(d, filename, data)


# ======================================================================
# Subsystem 3: Stale PR Reduction
# ======================================================================

@dataclass
class StaleScanRecord:
    timestamp: str = ""
    repo_full_name: str = ""
    total_scanned: int = 0
    exempt_count: int = 0
    stale_count: int = 0
    warn_count: int = 0
    label_count: int = 0
    close_count: int = 0
    dry_run: bool = True


@dataclass
class StaleReductionResult:
    repo_full_name: str = ""
    first_scan_stale: int = 0
    latest_scan_stale: int = 0
    absolute_reduction: int = 0
    reduction_pct: float = 0.0
    total_scans: int = 0
    scan_period_days: float = 0.0


class StaleMetricsLogger:
    """Record stale scan results and compute reduction over time."""

    @staticmethod
    def log_scan(record: StaleScanRecord) -> None:
        record.timestamp = record.timestamp or _now_iso()
        d = _ensure_metrics_dir()
        _append_jsonl(d, "stale_scans.jsonl", record.__dict__)

    @staticmethod
    def compute_reduction(repo_full_name: str) -> StaleReductionResult | None:
        d = _metrics_dir()
        records = _read_jsonl(d / "stale_scans.jsonl")

        repo_scans = [
            r for r in records
            if r.get("repo_full_name") == repo_full_name
        ]
        if len(repo_scans) < 2:
            return None

        repo_scans.sort(key=lambda r: r.get("timestamp", ""))
        first = repo_scans[0]
        last = repo_scans[-1]
        first_stale = int(first.get("stale_count", 0))
        last_stale = int(last.get("stale_count", 0))
        reduction = first_stale - last_stale
        pct = (reduction / first_stale * 100) if first_stale > 0 else 0.0

        # Compute scan period
        try:
            t0 = datetime.fromisoformat(first.get("timestamp", ""))
            t1 = datetime.fromisoformat(last.get("timestamp", ""))
            period_days = (t1 - t0).total_seconds() / 86400
        except (ValueError, TypeError):
            period_days = 0.0

        return StaleReductionResult(
            repo_full_name=repo_full_name,
            first_scan_stale=first_stale,
            latest_scan_stale=last_stale,
            absolute_reduction=reduction,
            reduction_pct=round(pct, 1),
            total_scans=len(repo_scans),
            scan_period_days=round(period_days, 1),
        )

    @staticmethod
    def list_scans(repo_full_name: str) -> list[dict]:
        d = _metrics_dir()
        records = _read_jsonl(d / "stale_scans.jsonl")
        return [r for r in records if r.get("repo_full_name") == repo_full_name]


# ======================================================================
# Subsystem 4: Time to First Response (TTR)
# ======================================================================

@dataclass
class TTRRecord:
    delivery_id: str = ""
    event_type: str = ""
    repo_full_name: str = ""
    issue_or_pr_number: int = 0
    received_at: str = ""
    first_response_at: str = ""
    ttr_seconds: float = 0.0
    response_type: str = ""


class TTRTracker:
    """Track webhook-receipt → first-response latency.

    Uses an in-memory dictionary keyed by (repo_full_name, issue_or_pr_number)
    to match responses back to receipts. Flushes completed records to JSONL.
    """

    _pending: dict[tuple[str, int], dict] = {}
    _lock = threading.Lock()

    @classmethod
    def record_receipt(
        cls,
        repo_full_name: str,
        issue_or_pr: int,
        event_type: str,
    ) -> None:
        """Called from webhook handler when a valid event is received."""
        key = (repo_full_name, issue_or_pr)
        with cls._lock:
            cls._pending[key] = {
                "received_at": _now_iso(),
                "event_type": event_type,
            }

    @classmethod
    def record_response(
        cls,
        repo_full_name: str,
        issue_or_pr: int,
        response_type: str = "comment",
    ) -> None:
        """Called after posting the first comment/review on an issue or PR."""
        key = (repo_full_name, issue_or_pr)
        response_at = _now_iso()

        with cls._lock:
            receipt = cls._pending.pop(key, None)

        if receipt is None:
            return  # no matching receipt (e.g., comment from non-webhook path)

        try:
            t0 = datetime.fromisoformat(receipt["received_at"])
            t1 = datetime.fromisoformat(response_at)
            ttr = (t1 - t0).total_seconds()
        except (ValueError, TypeError):
            ttr = 0.0

        record = TTRRecord(
            event_type=receipt.get("event_type", ""),
            repo_full_name=repo_full_name,
            issue_or_pr_number=issue_or_pr,
            received_at=receipt["received_at"],
            first_response_at=response_at,
            ttr_seconds=round(ttr, 2),
            response_type=response_type,
        )

        d = _ensure_metrics_dir()
        _append_jsonl(d, "ttr_log.jsonl", record.__dict__)

    @classmethod
    def compute_stats(cls, window_hours: float = 24) -> dict:
        """Compute TTR statistics for recent entries.

        Returns dict with: count, median_seconds, mean_seconds, p95_seconds,
        min_seconds, max_seconds, below_5min_count, below_5min_pct.
        """
        import statistics

        d = _metrics_dir()
        records = _read_jsonl(d / "ttr_log.jsonl")

        cutoff = datetime.now(timezone.utc)
        filtered = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r.get("received_at", ""))
                if (cutoff - ts).total_seconds() <= window_hours * 3600:
                    filtered.append(float(r.get("ttr_seconds", 0)))
            except (ValueError, TypeError):
                pass

        if not filtered:
            return {
                "count": 0, "median_seconds": 0, "mean_seconds": 0,
                "p95_seconds": 0, "min_seconds": 0, "max_seconds": 0,
                "below_5min_count": 0, "below_5min_pct": 0,
                "status": "no_data",
            }

        filtered.sort()
        n = len(filtered)
        p95_idx = int(n * 0.95)
        below_5 = sum(1 for t in filtered if t <= 300)

        return {
            "count": n,
            "median_seconds": round(statistics.median(filtered), 1),
            "mean_seconds": round(statistics.mean(filtered), 1),
            "p95_seconds": round(filtered[min(p95_idx, n - 1)], 1),
            "min_seconds": round(filtered[0], 1),
            "max_seconds": round(filtered[-1], 1),
            "below_5min_count": below_5,
            "below_5min_pct": round(below_5 / n * 100, 1),
            "status": "ok",
        }
