"""
context/compressor.py

Semantic context compression: when conversation history exceeds the token
budget, have the LLM generate a structured summary of the oldest messages
before discarding them.  The summary preserves *what happened* (files changed,
test results, key findings) rather than the raw transcript.

Design
------
Compression happens inside ``Agent._build_messages()``, **before**
``TokenBudget.trim_history()``.  The compressor:

1. Identifies messages that would be dropped by the token budget.
2. Calls the LLM with a summarisation prompt over those messages.
3. Replaces the dropped segment with a single ``[Context Summary]``
   user message that preserves semantics at ~5% of the original token cost.
4. Lets ``trim_history()`` run as normal on the already-compressed list
   (it may still need to trim if the summary + recent messages exceed budget).
"""

from __future__ import annotations

import logging

from llm.base import LLMBackend, LLMMessage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compression threshold constants
# ---------------------------------------------------------------------------

COMPRESS_TRIGGER_MESSAGES = 12
COMPRESS_MIN_OLD_MESSAGES = 6

# ---------------------------------------------------------------------------
# Summarisation prompt
# ---------------------------------------------------------------------------

_COMPRESSION_SYSTEM = """\
You are a context compressor. Summarise the conversation segment below into a
compact, structured note that preserves all information relevant to the
ongoing coding task.

Output ONLY a JSON object with these keys:
- "summary": 1-2 sentence overview of what happened in this segment.
- "files_examined": list of file paths the agent read or searched.
- "files_modified": list of file paths the agent edited or created (with a
  brief note of what changed).
- "test_results": any test output, failures, or successes observed.
- "key_findings": critical discoveries (root causes, error messages, design
  decisions) that future steps MUST know.
- "current_state": what the agent was doing when this segment ended.

Be concise. Prefer lists over prose. Every token counts.
"""

_COMPRESSION_USER = """\
Compress the following conversation segment.  This segment will be DELETED
from the agent's context — only your summary will survive.  Make sure the
agent can continue working without losing critical information.

=== SEGMENT START ===
{segment}
=== SEGMENT END ===

Return ONLY the JSON object (no markdown fences, no preamble)."""


# ---------------------------------------------------------------------------
# Compressor
# ---------------------------------------------------------------------------

class ContextCompressor:
    """Generate semantic summaries of old conversation segments."""

    def __init__(self, backend: LLMBackend) -> None:
        self._backend = backend
        self._compression_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compress(
        self,
        messages: list[dict],
        backend: LLMBackend | None = None,
        task_description: str = "",
    ) -> list[dict]:
        """Compress the *oldest* portion of the message list.

        Returns a new list where messages in the compression window (indices
        1 .. N) have been replaced by a single summary message.  Index 0
        (task description) is never compressed.

        If the message list is too short to benefit from compression, it is
        returned unchanged.
        """
        if len(messages) < COMPRESS_TRIGGER_MESSAGES:
            return messages

        # Determine the compression boundary: messages [1:boundary] get
        # summarised; messages [boundary:] are kept verbatim.
        keep_recent = max(4, len(messages) // 3)
        boundary = len(messages) - keep_recent
        if boundary < COMPRESS_MIN_OLD_MESSAGES:
            return messages  # not enough old material worth compressing

        segment = messages[1:boundary]
        recent = messages[boundary:]

        # Build the segment text for the LLM
        segment_text = self._format_segment(segment, task_description)

        try:
            be = backend or self._backend
            summary = self._generate_summary(be, segment_text)
        except Exception:
            logger.debug("Compression LLM call failed, keeping original messages",
                         exc_info=True)
            return messages

        if not summary:
            return messages

        self._compression_count += 1
        compressed_msg = {
            "role": "user",
            "content": self._format_summary_message(summary, boundary - 1),
        }

        logger.info(
            "Context compressed: %d messages → 1 summary (%d chars → %d chars), "
            "total compressions: %d",
            len(segment), len(segment_text), len(compressed_msg["content"]),
            self._compression_count,
        )

        return [messages[0], compressed_msg] + recent

    @property
    def compression_count(self) -> int:
        return self._compression_count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _format_segment(messages: list[dict], task_description: str) -> str:
        """Format the old segment as a text block for the LLM."""
        parts: list[str] = []
        if task_description:
            parts.append(f"Task: {task_description}")
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            # Truncate very long individual messages
            if len(content) > 2000:
                content = content[:2000] + f"\n... [{len(content) - 2000} more chars]"
            parts.append(f"[{role}]: {content}")
        return "\n\n".join(parts)

    @staticmethod
    def _format_summary_message(summary: dict, dropped_count: int) -> str:
        """Render the structured summary into a user message."""
        lines = [
            f"[Context Summary — {dropped_count} earlier messages compressed]",
            "",
        ]
        if summary.get("summary"):
            lines.append(f"Overview: {summary['summary']}")
            lines.append("")

        files_examined = summary.get("files_examined", [])
        if files_examined:
            lines.append("Files examined:")
            for f in files_examined:
                lines.append(f"  - {f}")
            lines.append("")

        files_modified = summary.get("files_modified", [])
        if files_modified:
            lines.append("Files modified:")
            for f in files_modified:
                lines.append(f"  - {f}")
            lines.append("")

        test_results = summary.get("test_results")
        if test_results and test_results.strip():
            lines.append(f"Test results: {test_results}")
            lines.append("")

        key_findings = summary.get("key_findings", [])
        if key_findings:
            lines.append("Key findings:")
            for f in key_findings:
                lines.append(f"  - {f}")
            lines.append("")

        current_state = summary.get("current_state")
        if current_state and current_state.strip():
            lines.append(f"Current state: {current_state}")

        return "\n".join(lines)

    def _generate_summary(
        self,
        backend: LLMBackend,
        segment_text: str,
    ) -> dict | None:
        """Call the LLM to summarise the old segment.

        Returns the parsed JSON dict, or None on failure.
        """
        # Use complete_text() — a dedicated path for raw text output,
        # no tool schemas, no Action parsing overhead.
        messages = [
            LLMMessage(role="system", content=_COMPRESSION_SYSTEM),
            LLMMessage(
                role="user",
                content=_COMPRESSION_USER.format(segment=segment_text),
            ),
        ]
        raw = backend.complete_text(messages).strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            # Remove opening fence
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            # Remove closing fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()

        import json
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Compression summary parse failed: %s", raw[:200])
            return None
