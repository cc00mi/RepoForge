"""
pipeline/triage.py

Issue triage pipeline: classify, deduplicate, solvability gate.

Flow:
  1. Classify → type + priority + effort + labels
  2. Dedup → check against recent_issues in RepoMemory
  3. Solvability gate → auto_fix | needs_triage | escalate
  4. Generate triage comment

No external NLP dependencies. Dedup uses difflib.SequenceMatcher (stdlib).
"""

from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class IssueTriage:
    """Result of triage classification."""
    classification: str = "bug"     # bug | enhancement | question | docs | security | performance | dependencies
    priority: str = "p2"           # p0 | p1 | p2 | p3
    effort: str = "medium"         # trivial | small | medium | large
    labels: list[str] = field(default_factory=list)
    confidence: float = 0.5
    summary: str = ""              # one-line LLM or heuristic summary
    root_cause: str = ""           # suspected root cause
    suggested_approach: str = ""   # how to fix it


@dataclass
class DupCandidate:
    """A potential duplicate issue."""
    reference: str                 # "owner/repo#NNN" or "#NNN"
    title: str
    score: float                   # 0.0–1.0 similarity
    source: str                    # "memory" | "github"


# ---------------------------------------------------------------------------
# Keyword-based classification rules
# ---------------------------------------------------------------------------

_TYPE_RULES = [
    # (label, keywords, classification)
    # NOTE: security is first — "error" in "error logs" should not trump security
    ("security", ["security", "vulnerability", "exploit", "injection",
                   "xss", "csrf", "auth bypass", "cve", "leaked key",
                   "leaked token", "leaked credential", "credential leak",
                   "data leak", "api key", "auth token", "secret key"], "security"),
    ("bug", ["bug", "error", "crash", "broken", "fail", "exception",
              "incorrect", "wrong", "regression", "defect"], "bug"),
    ("enhancement", ["feature request", "enhancement", "suggestion",
                      "would be nice", "add support for", "should have"], "enhancement"),
    ("documentation", ["doc", "documentation", "readme", "typo", "spelling",
                        "document", "explain"], "docs"),
    ("question", ["question", "how do i", "how to", "how can i",
                   "what is", "why does", "help"], "question"),
    ("performance", ["performance", "slow", "latency", "timeout",
                      "memory leak", "optimize", "throughput"], "performance"),
    ("dependencies", ["dependency", "dependabot", "bump", "upgrade",
                       "version", "package"], "dependencies"),
]

_PRIORITY_PATTERNS = [
    (r"crash|data loss|security|critical|urgent|p0", "p0"),
    (r"broken|regression|blocking|stuck|can.t work", "p1"),
    (r"should|would be nice|minor|cosmetic|nit", "p3"),
]

_EFFORT_PATTERNS = [
    (r"typo|one.?(line|word)|rename|spelling|trivial", "trivial"),
    (r"minor|simple|small|easy|straightforward", "small"),
    (r"major|complex|refactor|restructure|rewrite|architecture", "large"),
]


# ---------------------------------------------------------------------------
# Triage engine
# ---------------------------------------------------------------------------

class TriageEngine:
    """Classify, deduplicate, and gate issues before agent dispatch."""

    def classify(self, title: str, body: str) -> IssueTriage:
        """Classify an issue with keyword heuristics.

        Returns an IssueTriage with classification, priority, effort, and
        suggested labels. Confidence is set to 0.9 for strong matches,
        0.6 for ambiguous ones.
        """
        text = (title + " " + (body or "")).lower()

        # 1. Type classification
        classification = "bug"
        labels: list[str] = []
        best_confidence = 0.0

        classified = False
        for label, keywords, cls_type in _TYPE_RULES:
            if any(kw in text for kw in keywords):
                labels.append(label)
                if not classified:
                    classification = cls_type
                    classified = True
                # Shorter, more specific keywords → higher confidence
                matches = sum(1 for kw in keywords if kw in text)
                confidence = min(0.95, 0.5 + matches * 0.15)
                best_confidence = max(best_confidence, confidence)

        # Fallback: if nothing matched, mark as bug (most common)
        if not labels:
            labels = ["bug", "needs-triage"]
            best_confidence = 0.3

        # 2. Priority inference
        priority = "p2"
        for pattern, pri in _PRIORITY_PATTERNS:
            if re.search(pattern, text):
                priority = pri
                break

        # 3. Effort estimation
        effort = "medium"
        for pattern, eff in _EFFORT_PATTERNS:
            if re.search(pattern, text):
                effort = eff
                break

        # 4. Generate brief summary
        summary = self._summarize(title, body)

        return IssueTriage(
            classification=classification,
            priority=priority,
            effort=effort,
            labels=labels,
            confidence=best_confidence,
            summary=summary,
        )

    def check_duplicates(
        self,
        title: str,
        body: str,
        recent_issues: list,  # list of IssueOutcome or dicts with reference, title
    ) -> list[DupCandidate]:
        """Check new issue against recent issues for duplicates.

        Uses two complementary similarity measures and takes the max:
        1. difflib.SequenceMatcher on titles — catches literal/near-literal dupes
        2. TF cosine similarity on title + first 500 chars of body —
           catches rephrased or semantically similar duplicates

        Returns candidates sorted by score descending.
        """
        new_text = self._dedup_text(title, body)
        new_tokens = self._tokenize(new_text)

        # TF vector for the new issue (no IDF — shared words are the signal for dedup)
        new_tf = self._tf_vector(new_tokens)

        candidates: list[DupCandidate] = []
        title_lower = title.lower().strip()

        for ri in recent_issues:
            past_title = self._get_attr(ri, 'title')
            ref = self._get_attr(ri, 'reference')
            if not ref or not past_title:
                continue

            # 1. Sequence-based similarity (title only)
            seq_score = difflib.SequenceMatcher(
                None, title_lower, past_title.lower().strip()
            ).ratio()

            # 2. TF cosine similarity (title + body) — catches rephrased duplicates
            past_body = self._get_attr(ri, 'validation_summary')
            past_text = self._dedup_text(past_title, past_body)
            past_tokens = self._tokenize(past_text)
            past_tf = self._tf_vector(past_tokens)
            cosine_score = self._cosine_similarity(new_tf, past_tf)

            # Max captures both literal dupes and semantic dupes
            score = max(seq_score, cosine_score)

            if score > 0.40:
                candidates.append(DupCandidate(
                    reference=ref, title=past_title,
                    score=round(score, 3), source="memory",
                ))

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    # ---- similarity helpers --------------------------------------------------

    @staticmethod
    def _dedup_text(title: str, body: str) -> str:
        """Combine title and first 500 chars of body into one dedup text."""
        body = (body or "").strip()
        return f"{title} {body[:500]}".strip()

    @staticmethod
    def _tokenize(text: str) -> Counter:
        """Tokenize text into a bag-of-words Counter (stdlib only, >=3 char tokens).

        Captures both ASCII words and CJK character bigrams for cross-lingual
        fuzzy matching at the n-gram level.
        """
        text = text.lower()
        # ASCII words (>=3 chars) — handles English, code identifiers, etc.
        words = re.findall(r'[a-z0-9_]{3,}', text)
        # CJK character bigrams — captures semantic units without a segmenter
        cjk = re.findall(r'[一-鿿]', text)
        for i in range(len(cjk) - 1):
            words.append(cjk[i] + cjk[i + 1])
        return Counter(words)

    @staticmethod
    def _tf_vector(tokens: Counter) -> dict[str, float]:
        """Convert a term-frequency Counter to a normalized TF vector.

        No IDF weighting — shared words are the signal for duplicate detection.
        """
        total = sum(tokens.values()) or 1
        return {word: count / total for word, count in tokens.items()}

    @staticmethod
    def _cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
        """Cosine similarity between two TF-IDF vectors."""
        if not a or not b:
            return 0.0
        keys = set(a.keys()) & set(b.keys())
        dot = sum(a[k] * b[k] for k in keys)
        mag_a = math.sqrt(sum(v ** 2 for v in a.values()))
        mag_b = math.sqrt(sum(v ** 2 for v in b.values()))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    @staticmethod
    def _get_attr(obj, name: str) -> str:
        """Get attribute from IssueOutcome or dict."""
        if isinstance(obj, dict):
            return obj.get(name, '')
        return getattr(obj, name, '')

    def assess_solvability(self, title: str, body: str) -> tuple[str, str]:
        """Determine if the agent can reasonably fix this issue.

        Returns:
            (decision, reason) where decision is:
              - "auto_fix" — agent can likely handle this
              - "needs_triage" — unclear, needs human input first
              - "escalate" — requires human expertise
        """
        text = (title + " " + (body or "")).lower()
        reasons: list[str] = []

        # Check for clear problem description (min body length or structured)
        body_len = len(body or "")
        if body_len < 60:
            reasons.append("Issue body is very short (<60 chars)")

        # Check for reproduction steps
        has_repro = bool(re.search(
            r"(steps? to (reproduce|replicate|trigger)|reproduction|"
            r"how to reproduce|to reproduce)",
            text,
        ))
        if not has_repro and body_len > 100:
            # For longer bodies, check if there's any structured content
            has_structure = bool(re.search(
                r"(expected.*behavior|actual.*behavior|steps?|"
                r"###?\s|```|environment|version)",
                text,
            ))
            if not has_structure:
                reasons.append("No clear reproduction steps or structured description")

        # Check for external dependencies
        has_external = bool(re.search(
            r"(api key|credentials?|third.party|external service|"
            r"need access to|permission|authentication)",
            text,
        ))
        if has_external:
            reasons.append("Involves external service or credentials")

        # Check for design decisions
        has_design = bool(re.search(
            r"(architecture|design decision|api contract|breaking change|"
            r"deprecation|roadmap|planning)",
            text,
        ))
        if has_design:
            reasons.append("Requires architecture/design decision")

        # Check if it's clearly actionable
        has_actionable = bool(re.search(
            r"(fix|change|add|remove|update|rename|replace|move|convert)",
            text,
        ))
        if not has_actionable and body_len < 100:
            reasons.append("No clear actionable request found")

        # Decision logic
        if not reasons:
            return "auto_fix", "Issue has sufficient detail for agent to attempt a fix"

        # Escalate if multiple serious blocks
        serious = sum(1 for r in reasons if "external" in r or "design" in r)
        if serious >= 2:
            return "escalate", "; ".join(reasons)

        if len(reasons) >= 2:
            return "needs_triage", "; ".join(reasons)

        return "needs_triage", reasons[0] if reasons else "Unclear description"

    def generate_triage_comment(
        self,
        triage: IssueTriage,
        duplicates: list[DupCandidate] | None = None,
        decision: str = "auto_fix",
        reason: str = "",
    ) -> str:
        """Render a triage analysis comment in markdown.

        Args:
            triage: classification result
            duplicates: list of potential duplicates
            decision: auto_fix | needs_triage | escalate
            reason: explanation for the decision
        """
        lines = [
            "## Automated Triage by RepoForge",
            "",
            f"**Classification:** {triage.classification} | "
            f"**Priority:** {triage.priority} | "
            f"**Est. Effort:** {triage.effort} | "
            f"**Confidence:** {triage.confidence:.0%}",
            "",
        ]

        if triage.summary:
            lines.append("### Summary")
            lines.append(triage.summary)
            lines.append("")

        if triage.root_cause:
            lines.append("### Possible Root Cause")
            lines.append(triage.root_cause)
            lines.append("")

        if triage.suggested_approach:
            lines.append("### Suggested Approach")
            lines.append(triage.suggested_approach)
            lines.append("")

        # Duplicates section
        if duplicates:
            high_dups = [d for d in duplicates if d.score > 0.75]
            if high_dups:
                lines.append("### Potential Duplicates")
                for d in high_dups[:3]:
                    if d.score > 0.90:
                        lines.append(f"- **{d.reference}** (similarity: {d.score:.0%}) — likely duplicate")
                    else:
                        lines.append(f"- {d.reference} (similarity: {d.score:.0%}) — possibly related")
                lines.append("")

        # Decision
        lines.append("### Decision")
        icon = {"auto_fix": "Agent will attempt to fix this issue.",
                "needs_triage": "Agent analysis provided — human review recommended before fixing.",
                "escalate": "This issue requires human expertise — agent will not attempt a fix.",
                }.get(decision, reason)
        lines.append(f"**{decision.upper()}:** {icon}")
        if reason and decision != "auto_fix":
            lines.append(f"\n*Reason:* {reason}")

        lines.append("")
        lines.append("---")
        lines.append("*This is an automated analysis. A maintainer will review shortly.*")

        return "\n".join(lines)

    # ---- internal ------------------------------------------------------------

    @staticmethod
    def _summarize(title: str, body: str) -> str:
        """Extract a one-line summary from the issue."""
        # If body has a clear first line, use it
        if body:
            first_line = body.strip().split("\n")[0].strip()
            if len(first_line) > 20 and len(first_line) < 200:
                return first_line[:200]
        return title[:200]
