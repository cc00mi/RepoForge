"""
agent/sub_agent.py

Lightweight sub-agent for delegated exploration tasks.

The main agent can spawn ExploreAgent instances to search, read, and
analyse code in parallel.  Each sub-agent runs a short ReAct loop with
*read-only* tools and returns a structured summary.  The main agent
only sees the summary, not the raw tool output — saving context window
space for decision-making and editing.

Design
------
- Reuses ``Agent`` with a restricted ``ToolRegistry`` (read-only tools).
- Limited to ``max_steps=8`` and smaller token budget.
- Multiple sub-agents can run in parallel via ``run_parallel()``.
- Returns ``ExploreResult`` — a structured summary with findings,
  examined files, and confidence.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass, field

from agent.core import Agent, AgentConfig
from agent.task import Task, RunResult
from llm.base import LLMBackend
from tools.base import ToolRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ExploreResult:
    """Structured result from an explore sub-agent run."""

    query: str
    summary: str = ""
    files_examined: list[str] = field(default_factory=list)
    key_findings: list[str] = field(default_factory=list)
    relevant_paths: list[str] = field(default_factory=list)
    confidence: float = 0.5
    steps_taken: int = 0
    success: bool = False
    error: str | None = None

    def to_output(self) -> str:
        """Render as a compact text block for the main agent."""
        lines = [f"## Explore Result: {self.query}", ""]
        if self.summary:
            lines.append(self.summary)
            lines.append("")
        if self.files_examined:
            lines.append("**Files examined:**")
            for f in self.files_examined[:10]:
                lines.append(f"  - {f}")
            lines.append("")
        if self.key_findings:
            lines.append("**Key findings:**")
            for f in self.key_findings:
                lines.append(f"  - {f}")
            lines.append("")
        if self.relevant_paths:
            lines.append("**Relevant paths (start here):**")
            for p in self.relevant_paths[:8]:
                lines.append(f"  - {p}")
            lines.append("")
        if not self.success and self.error:
            lines.append(f"**Error:** {self.error}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sub-agent
# ---------------------------------------------------------------------------


class ExploreAgent:
    """A lightweight, read-only sub-agent for code exploration.

    Does NOT use EventLog (logging would add noise).  Runs synchronously
    within a thread when used via ``run_parallel()``.

    Usage::

        backend = ...        # shared LLM backend
        tools = ExploreAgent.default_tools(base_registry)
        agent = ExploreAgent(backend, tools)
        result = agent.explore("Find where auth logic is implemented",
                               repo_path="/path/to/repo")
        print(result.to_output())
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        max_steps: int = 8,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._max_steps = max_steps

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def explore(self, query: str, repo_path: str) -> ExploreResult:
        """Run a focused exploration task synchronously.

        Args:
            query: What to look for (natural language).
            repo_path: Absolute path to the repository root.

        Returns:
            ExploreResult with findings and examined files.
        """
        task = Task(
            description=self._build_explore_prompt(query),
            repo_path=repo_path,
            max_steps=self._max_steps,
            budget_tokens=30_000,
        )

        config = AgentConfig(
            max_steps=self._max_steps,
            budget_tokens=30_000,
            history_max_messages=16,
            reflection_no_edit_steps=5,
        )

        agent = Agent(self._backend, self._registry, config)

        try:
            result = agent.run(task, _noop_log())
        except Exception as exc:
            logger.debug("Explore sub-agent failed: %s", exc, exc_info=True)
            return ExploreResult(
                query=query,
                summary="",
                error=str(exc),
                success=False,
            )

        return self._parse_result(query, result)

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def default_tools(base_registry: ToolRegistry) -> ToolRegistry:
        """Extract a read-only subset from the main ToolRegistry.

        Only includes tools that do NOT modify the filesystem or run
        arbitrary commands.
        """
        read_only_names = {
            "file_read", "file_view",
            "search_text", "find_files", "find_symbol",
            "git_status", "git_diff",
        }
        ro = ToolRegistry()
        for tool in base_registry.get_all():
            if tool.name in read_only_names:
                ro.register(tool)
        return ro

    @staticmethod
    def run_parallel(
        queries: list[str],
        repo_path: str,
        backend: LLMBackend,
        base_registry: ToolRegistry,
        max_workers: int = 4,
    ) -> list[ExploreResult]:
        """Run multiple exploration queries in parallel.

        Args:
            queries: List of natural-language exploration questions.
            repo_path: Shared repository path.
            backend: LLM backend (shared across sub-agents).
            base_registry: Main ToolRegistry (read-only subset extracted).
            max_workers: Max thread pool size.

        Returns:
            List of ExploreResult, one per query, in the same order.
        """
        tools = ExploreAgent.default_tools(base_registry)
        results: list[ExploreResult | None] = [None] * len(queries)

        def _run_one(idx: int, query: str) -> None:
            agent = ExploreAgent(backend, tools)
            results[idx] = agent.explore(query, repo_path)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max_workers, len(queries))
        ) as executor:
            futures = [
                executor.submit(_run_one, i, q)
                for i, q in enumerate(queries)
            ]
            for f in futures:
                try:
                    f.result(timeout=300)
                except Exception as exc:
                    logger.warning("Explore sub-agent timed out or crashed: %s", exc)

        return [r or ExploreResult(query=q, error="No result") for q, r in zip(queries, results)]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_explore_prompt(query: str) -> str:
        return (
            f"Explore the codebase to answer this question:\n\n"
            f"  {query}\n\n"
            f"Instructions:\n"
            f"1. Search for relevant files using search_text and find_files.\n"
            f"2. Read key files to understand the implementation.\n"
            f"3. Use find_symbol to locate specific functions or classes.\n"
            f"4. After you understand the answer, call FINISH with a concise "
            f"summary of:\n"
            f"   - What you found\n"
            f"   - Which files are relevant\n"
            f"   - Key code locations (file + line or function name)\n\n"
            f"Do NOT edit any files.  This is a read-only exploration task."
        )

    @staticmethod
    def _parse_result(query: str, result: RunResult) -> ExploreResult:
        """Extract structured findings from an Agent RunResult summary."""
        summary = result.summary or ""
        files = list(result.changed_files) if result.changed_files else []

        # Simple heuristic extraction of key findings from summary
        findings: list[str] = []
        for line in summary.split("\n"):
            line = line.strip()
            if line.startswith("- ") or line.startswith("* "):
                findings.append(line[2:])
            elif line.startswith("1. ") or line.startswith("2. "):
                findings.append(line[3:])

        if not findings and summary:
            findings = [summary[:200]]

        return ExploreResult(
            query=query,
            summary=summary[:500] if summary else "",
            files_examined=files,
            key_findings=findings[:8],
            relevant_paths=files[:8],
            confidence=0.7 if result.is_success() else 0.3,
            steps_taken=result.steps_taken,
            success=result.is_success(),
        )


# ---------------------------------------------------------------------------
# Internal: no-op EventLog for sub-agents
# ---------------------------------------------------------------------------

class _NoopEventLog:
    """Minimal EventLog stub — sub-agents don't need persistent logging."""

    def log_task_start(self, task): pass
    def log_action(self, step, action, raw_content=""): pass
    def log_observation(self, step, observation): pass
    def log_reflection(self, step, reason, prompt): pass
    def log_task_complete(self, steps, summary): pass
    def log_task_failed(self, steps, reason): pass
    def get_actions(self): return []
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


def _noop_log() -> _NoopEventLog:
    return _NoopEventLog()
