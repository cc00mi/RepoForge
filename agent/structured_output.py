"""
agent/structured_output.py

Extract and validate structured JSON from LLM responses.
Used at the end of each pipeline stage to produce machine-readable output.

If the JSON is malformed, a repair prompt is sent to the LLM (one retry).
If repair also fails, falls back to treating the raw text as a plain summary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Stage output schemas (simple dataclasses, no external deps)
# ---------------------------------------------------------------------------

@dataclass
class UnderstandOutput:
    """Stage 0: UNDERSTAND output."""
    problem_summary: str = ""
    candidate_files: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    repo_structure_summary: str = ""


@dataclass
class PlanOutput:
    """Stage 1: PLAN output."""
    goal: str = ""
    target_files: list[dict] = field(default_factory=list)   # [{path, reason}]
    proposed_changes: list[dict] = field(default_factory=list)  # [{title, details, files}]
    risks: list[str] = field(default_factory=list)
    validation_notes: list[str] = field(default_factory=list)


@dataclass
class ImplementOutput:
    """Stage 2: IMPLEMENT output."""
    summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    test_results: str = ""
    status: str = "needs_iteration"   # "success" | "needs_iteration" | "failed"


@dataclass
class VerifyOutput:
    """Stage 3: VERIFY output."""
    verdict: str = "fail"             # "pass" | "fail"
    summary: str = ""
    pr_narrative: str = ""


# ---------------------------------------------------------------------------
# Text sanitization — strip common LLM hallucination patterns
# ---------------------------------------------------------------------------

# Patterns that DeepSeek sometimes produces instead of JSON on forced-text output
_BARE_FUNCALL_RE = re.compile(
    r'\b(file_view|file_read|find_files|shell|file_edit|file_write|'
    r'git_diff|test|pytest|search_text|find_symbol)\s*\([^)]*\)',
    re.IGNORECASE,
)
_XML_TAG_RE = re.compile(
    r'<(function_result|｜｜DSML｜｜tool_calls|function_calls)[^>]*>(.*?)</\1>',
    re.DOTALL | re.IGNORECASE,
)
_XML_SELF_CLOSING_RE = re.compile(r'<(function_result|｜｜DSML｜｜tool_calls|function_calls)[^>]*/>', re.IGNORECASE)
_LEADING_NOISE_RE = re.compile(
    r'^((I (need|want|should|will|can|must|have|think|believe)|'
    r'Let me|Now|First|Next|Finally|Action|Thought|Observation)'
    r'\s*.+?[.:]\s*)',
    re.IGNORECASE,
)


def _sanitize_llm_output(raw: str) -> str:
    """
    Strip known hallucination patterns from LLM text output before attempting
    JSON extraction. Returns cleaned text that is more likely to contain
    parseable JSON.
    """
    text = raw.strip()

    # 1. Extract content from XML wrapper tags (DeepSeek sometimes wraps output)
    #    <function_result>{"key": "val"}</function_result> → {"key": "val"}
    def _extract_xml(m):
        return m.group(2) if m.lastindex and m.group(2) else ''
    text = _XML_TAG_RE.sub(_extract_xml, text)
    text = _XML_SELF_CLOSING_RE.sub('', text)

    # 2. Remove bare function calls (hallucinated tool invocations)
    #    But be careful: don't strip content inside JSON strings
    #    Only strip them if they appear OUTSIDE of {...}
    json_start = text.find('{')
    json_end = text.rfind('}')
    if json_start >= 0 and json_end > json_start:
        # JSON braces found — only clean text outside braces
        prefix = text[:json_start]
        suffix = text[json_end + 1:]
        prefix = _BARE_FUNCALL_RE.sub('', prefix)
        suffix = _BARE_FUNCALL_RE.sub('', suffix)
        text = prefix + text[json_start:json_end + 1] + suffix
    else:
        # No JSON braces — the whole text might be a hallucinated function call
        # Strip function calls and check if anything useful remains
        text = _BARE_FUNCALL_RE.sub('', text)

    # 3. Strip leading noise ("I need to find the...", "Let me...")
    text = _LEADING_NOISE_RE.sub('', text)

    # 4. Normalize whitespace
    text = text.strip()

    return text


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json(raw: str) -> dict:
    """
    Try to extract a JSON object from LLM output.
    Handles ```json fences, raw braces, and leading/trailing noise.
    Also sanitizes common LLM hallucination patterns before extraction.
    """
    raw = _sanitize_llm_output(raw)

    # 1. Try ```json ... ``` fences (greedy — find outermost block)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        return json.loads(m.group(1))

    # 2. Try from first { to last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError("No valid JSON found", raw, 0)


# ---------------------------------------------------------------------------
# Schema-based filling
# ---------------------------------------------------------------------------

def _str(v: Any) -> str:
    return str(v) if v else ""


def _list_str(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        return [v] if v.strip() else []
    return []


def _list_dict(v: Any) -> list[dict]:
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


def fill_understand(data: dict) -> UnderstandOutput:
    return UnderstandOutput(
        problem_summary=_str(data.get("problem_summary", data.get("summary", ""))),
        candidate_files=_list_str(data.get("candidate_files", data.get("files", []))),
        test_commands=_list_str(data.get("test_commands", data.get("tests", []))),
        repo_structure_summary=_str(data.get("repo_structure_summary", "")),
    )


def fill_plan(data: dict) -> PlanOutput:
    return PlanOutput(
        goal=_str(data.get("goal", "")),
        target_files=_list_dict(data.get("target_files", data.get("targets", []))),
        proposed_changes=_list_dict(data.get("proposed_changes", data.get("changes", []))),
        risks=_list_str(data.get("risks", [])),
        validation_notes=_list_str(data.get("validation_notes", data.get("validation", []))),
    )


def fill_implement(data: dict) -> ImplementOutput:
    return ImplementOutput(
        summary=_str(data.get("summary", "")),
        changed_files=_list_str(data.get("changed_files", data.get("files", []))),
        test_results=_str(data.get("test_results", data.get("tests", ""))),
        status=_str(data.get("status", "needs_iteration")),
    )


def fill_verify(data: dict) -> VerifyOutput:
    return VerifyOutput(
        verdict=_str(data.get("verdict", "fail")),
        summary=_str(data.get("summary", "")),
        pr_narrative=_str(data.get("pr_narrative", data.get("narrative", ""))),
    )


# ---------------------------------------------------------------------------
# High-level extract + validate
# ---------------------------------------------------------------------------

T = type["UnderstandOutput"] | type["PlanOutput"] | type["ImplementOutput"] | type["VerifyOutput"]

_FILLERS = {
    "understand": fill_understand,
    "plan": fill_plan,
    "implement": fill_implement,
    "verify": fill_verify,
}


def extract_and_validate(
    raw: str,
    stage: str,           # "understand" | "plan" | "implement" | "verify"
    repair_fn: Callable[[str], str] | None = None,
):
    """
    Extract JSON from LLM output and fill the correct dataclass.

    Args:
        raw:       Raw LLM response text
        stage:     Stage name (determines output schema)
        repair_fn: Optional repair callback: repair_fn(raw_response) -> repaired_response
                   Called once on parse failure.

    Returns:
        (dataclass instance, parse_success_bool)
    """
    fill = _FILLERS.get(stage)
    if fill is None:
        raise ValueError(f"Unknown stage: {stage}")

    def _try(raw_text: str):
        data = extract_json(raw_text)
        return fill(data)

    try:
        return _try(raw), True
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        if repair_fn:
            try:
                repaired = repair_fn(raw)
                return _try(repaired), True
            except Exception:
                pass

    # Fallback: create a minimal output from the raw text
    fallback_data = {"summary": raw[:500]}
    try:
        return fill({"problem_summary": raw[:500], "summary": raw[:500], "goal": raw[:500]}), False
    except Exception:
        return fill({}), False
