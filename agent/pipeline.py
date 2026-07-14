"""
agent/pipeline.py

Four-stage pipeline engine that replaces the single monolithic ReAct loop
with staged execution and context reset at each boundary.

Stages:
  Stage 0: UNDERSTAND — explore repo, identify candidate files & test commands
  Stage 1: PLAN       — design a concrete change plan, assess feasibility
  Stage 2: IMPLEMENT  — make the actual edits, run tests, iterate
  Stage 3: VERIFY     — final validation, produce PR narrative

Each stage runs its own Agent instance with a dedicated system prompt and
constrained tool set.  Only the structured JSON output flows between stages
— conversation history is reset to zero tokens at each boundary.

Feasibility gate between PLAN and IMPLEMENT: if the LLM decides the task
cannot be solved autonomously, the pipeline stops early.
"""

from __future__ import annotations

import json
import logging
import subprocess

# ---------------------------------------------------------------------------
# Per-stage token budgets (design doc §4.2)
# ---------------------------------------------------------------------------
_STAGE_TOKEN_BUDGETS: dict[str, int] = {
    "understand": 6_000,
    "plan": 4_000,
    "implement": 15_000,
    "verify": 3_000,
}
import time
from dataclasses import dataclass, field

from agent.core import Agent, AgentConfig
from agent.structured_output import (
    UnderstandOutput, PlanOutput, ImplementOutput, VerifyOutput,
    extract_and_validate,
)
from agent.task import Task, RunResult, RunStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage prompts — tightly focused, each ~1000-2000 tokens
# ---------------------------------------------------------------------------

_UNDERSTAND_SYSTEM = """\
You are an expert code explorer. Your job is to understand a GitHub issue
and identify the relevant parts of a codebase — NOT to make any changes.

## Available tools (THE ONLY TOOLS YOU CAN CALL)
- find_files  — search for files by glob pattern
- file_read   — read file contents
- file_view   — view a range of lines in a file
- finish      — complete the stage with your JSON findings

YOU CANNOT CALL: shell, file_edit, file_write, test, or any other tool.
If you try to call a tool not listed above, it will FAIL with an error.
Use ONLY the tools in the "Available tools" section of each message.

## Workflow
1. Read the issue description carefully
2. Explore the repository structure (find_files)
3. Read files that are likely relevant to the issue (file_read, file_view)
4. Identify test commands that exist in the project
5. Call finish with your findings — do NOT keep exploring past step 6-7

## Rules
- ONLY use: find_files, file_read, file_view, finish
- Do NOT call shell — it is not available in this stage
- Do NOT edit, write, or delete any files
- Do NOT run tests (that comes later)
- Be thorough but efficient — explore for 5-7 steps, then finish

## How to finish
Call the `finish` function with your JSON summary. Example:
finish(message='{"problem_summary": "...", "candidate_files": [...], ...}')

Your JSON message must be a valid JSON object with these fields:
{
  "problem_summary": "<2-3 sentence summary of the bug/feature>",
  "candidate_files": ["full/path/to/file1.py", "full/path/to/file2.py"],
  "test_commands": ["pytest tests/something", "python -m pytest"],
  "repo_structure_summary": "<brief overview of relevant directories>"
}

IMPORTANT: Do NOT output bare function calls like "file_view(...)".
Do NOT output XML tags like "<function_result>".
Only output valid JSON when calling finish or when forced to output text.
"""

_UNDERSTAND_TASK = """\
Explore the repository to understand this issue and identify relevant files.

## Issue
{description}

## Repository
{repo_path}

## Instructions
1. Start with find_files to understand the repo structure
2. Use file_view or file_read to examine likely candidate files
3. Identify how to run tests (look at CI configs, Makefile, setup.cfg, pyproject.toml)
4. When you have a clear picture, call finish with your findings in JSON format
"""


_PLAN_SYSTEM = """\
You are an expert software architect. Your job is to design a change plan
for fixing a bug or implementing a feature — NOT to write code.

## Workflow
1. Review the exploration summary from the previous stage
2. Read the candidate files identified (use file_read)
3. Design a minimal, safe change plan
4. Assess feasibility — can this be fixed autonomously?
5. When done, DO NOT make more tool calls — instead output your plan

## Rules
- ONLY use read-only tools: file_read, file_view, find_files
- Do NOT edit, write, or delete any files
- Be concrete: specify exact files, not generic descriptions
- Be honest about risks and unknowns
- CRITICAL: When reading candidate files, use the EXACT paths provided in the
  exploration summary. Do NOT shorten them or make them relative.

## How to finish
When you have designed the plan, call the `finish` function with your
JSON plan as the `message` parameter.

## Output Format
```json
{
  "goal": "<one-sentence goal>",
  "target_files": [
    {"path": "src/foo.py", "reason": "contains the buggy logic at line 42"}
  ],
  "proposed_changes": [
    {"title": "Fix null check in foo()", "details": "Add guard for None input", "files": ["src/foo.py"]}
  ],
  "risks": ["Changing foo() may break callers that expect the old behavior"],
  "validation_notes": ["Run pytest tests/test_foo.py to verify"]
}
```

## Feasibility Check
Before finishing, ask yourself:
- Do I understand the root cause?
- Are the files I need to change clearly identified?
- Is this fix achievable with the tools available (file_edit, shell)?
- Is the scope small enough for a single autonomous pass?

Set risks based on your assessment:
- If the task CANNOT be solved autonomously at all:
  risks: ["INFEASIBLE: <reason>"]  → pipeline aborts
- If the task is DOABLE but the scope is LARGE (3+ files, multi-module):
  risks: ["NARROW: <reason>"]  → pipeline limits to first target file only
- If the task is straightforward with 1-2 files:
  risks: [] or list specific concerns like "may break callers"
"""

_PLAN_TASK = """\
Design a change plan based on the exploration results.

## Repository
{repo_path}

## Exploration Summary
{understand_output}

## Repository Memory
{memory_text}

## Instructions
1. Read the key candidate files to understand the current code — use the
   EXACT file paths from the exploration summary (they already include the
   repo root prefix)
2. Design the minimal set of changes needed
3. Decide if this is feasible to implement autonomously
4. Call finish with your plan in JSON format
"""


_IMPLEMENT_SYSTEM = """\
You are an expert software engineer. Your job is to implement the change plan
by editing files, running tests, and iterating until they pass.

## Workflow
1. Read the plan from the previous stage
2. Read the target files to understand existing code
3. Make the edits using file_edit or file_write
4. Run tests to verify
5. If tests fail, fix and re-run (max 3 iterations)
6. When done, DO NOT make more tool calls — instead output your results

## Rules
- Follow the plan — don't make unrelated changes
- Make minimal, precise edits
- Always run tests after editing
- If stuck after 3 iterations, report status "needs_iteration"

## How to finish
When you have implemented and tested the changes, call the `finish` function
with your JSON results as the `message` parameter.

## Output Format
```json
{
  "summary": "<what was changed and why>",
  "changed_files": ["path/to/changed.py"],
  "test_results": "<test output summary>",
  "status": "success" | "needs_iteration" | "failed"
}
```
"""

_IMPLEMENT_TASK = """\
Implement the change plan.

## Repository
{repo_path}

## Plan
{plan_output}

## Repository Memory
{memory_text}

## Instructions
1. Read the target files first (use full paths starting from the repo root)
2. Make the edits
3. Run tests to verify
4. If tests fail, analyze and fix — up to 3 attempts
5. When tests pass (or you've exhausted attempts), call finish with JSON results
"""


_VERIFY_SYSTEM = """\
You are a code reviewer. Your job is to verify the changes made and produce
a final summary.

## Workflow
1. Review the implementation results
2. Run `git diff` to see the actual changes
3. Run any remaining tests
4. When done, DO NOT make more tool calls — instead output your verdict

## How to finish
When you have verified the changes, call the `finish` function with your
JSON results as the `message` parameter.

## Output Format
```json
{
  "verdict": "pass" | "fail",
  "summary": "<1-2 paragraph summary of what was done and whether it works>",
  "pr_narrative": "<a PR description suitable for submitting to the repository>"
}
```
"""

_VERIFY_TASK = """\
Verify the implementation and produce a final report.

## Repository
{repo_path}

## Implementation Summary
{implement_output}

## Instructions
1. Run git diff to see exact changes
2. Verify the changes match the original issue
3. Produce a PR narrative
4. Call finish with JSON results
"""


# ---------------------------------------------------------------------------
# Repair prompts — called when JSON extraction fails
# ---------------------------------------------------------------------------

_REPAIR_PROMPTS = {
    "understand": """\
Your previous response was not valid JSON. Output ONLY a JSON object like:
{{
  "problem_summary": "<2-3 sentence summary>",
  "candidate_files": ["path/to/file1.py"],
  "test_commands": ["pytest tests/something"],
  "repo_structure_summary": "<brief overview>"
}}

No markdown, no ```json fences, no commentary. ONLY the JSON object.

Previous response: {raw}
""",
    "plan": """\
Your previous response was not valid JSON. Output ONLY a JSON object like:
{{
  "goal": "<one-sentence goal>",
  "target_files": [{{"path": "...", "reason": "..."}}],
  "proposed_changes": [{{"title": "...", "details": "...", "files": ["..."]}}],
  "risks": ["..."],
  "validation_notes": ["..."]
}}

No markdown, no ```json fences, no commentary. ONLY the JSON object.

Previous response: {raw}
""",
    "implement": """\
Your previous response was not valid JSON. Output ONLY a JSON object like:
{{
  "summary": "<what was changed>",
  "changed_files": ["path/to/file.py"],
  "test_results": "<test output>",
  "status": "success"
}}

No markdown, no ```json fences, no commentary. ONLY the JSON object.

Previous response: {raw}
""",
    "verify": """\
Your previous response was not valid JSON. Output ONLY a JSON object like:
{{
  "verdict": "pass",
  "summary": "<what was done>",
  "pr_narrative": "<PR description>"
}}

No markdown, no ```json fences, no commentary. ONLY the JSON object.

Previous response: {raw}
""",
}

_REPAIR_PROMPT_FALLBACK = """\
Your previous response was not valid JSON. Output ONLY the correct JSON object.
No markdown, no ```json fences, no commentary, no function calls.

Previous response: {raw}
"""


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Full result of a pipeline run."""
    task_id: str
    status: RunStatus
    summary: str
    steps_taken: int = 0
    total_tokens: int = 0
    patch: str | None = None
    changed_files: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    # Stage outputs (for debugging / downstream use)
    understand: UnderstandOutput | None = None
    plan: PlanOutput | None = None
    implement: ImplementOutput | None = None
    verify: VerifyOutput | None = None

    # Stop reason
    stopped_early: bool = False
    stop_reason: str = ""


# ---------------------------------------------------------------------------
# Pipeline engine
# ---------------------------------------------------------------------------

class PipelineEngine:
    """Orchestrates the four-stage pipeline."""

    def __init__(self, backend, registry, config):
        self._backend = backend
        self._registry = registry
        self._config = config

    def run(self, task: Task, log, repo_memory_text: str = "") -> PipelineResult:
        """
        Execute the full pipeline and return PipelineResult.
        On failure, falls back to the standard single-stage Agent.run().
        """
        t0 = time.time()
        total_steps = 0
        total_tokens = 0

        try:
            # ── Stage 0: UNDERSTAND ──────────────────────────────────
            understand, steps, tokens = self._run_stage(
                task=task,
                stage_name="understand",
                system_prompt=self._build_system(_UNDERSTAND_SYSTEM, task.repo_path),
                task_template=_UNDERSTAND_TASK,
                task_kwargs={"description": task.description, "repo_path": task.repo_path},
                max_steps=8,
                tools_allowlist={"find_files", "file_read", "file_view"},
                memory_text="",
                log=log,
            )
            total_steps += steps
            total_tokens += tokens
            # Normalize candidate_files: prepend repo_path to relative paths
            _repo_root = task.repo_path
            _normalized = []
            for _f in understand.candidate_files:
                if not _f.startswith(_repo_root):
                    _f = _f.replace("\\", "/")
                    _normalized.append(f"{_repo_root}/{_f}")
                else:
                    _normalized.append(_f)
            understand.candidate_files = _normalized

            logger.info("Stage UNDERSTAND: %d files, %d tests",
                        len(understand.candidate_files), len(understand.test_commands))

            # Update Repo Memory: bump candidate paths
            if understand.candidate_files:
                try:
                    from memory.repo_memory import memory_service
                    m = memory_service.load(_repo_name_from_path(task.repo_path))
                    memory_service.bump_candidate_paths(m, understand.candidate_files)
                    memory_service.save(m)
                except Exception:
                    pass

            # ── Stage 1: PLAN ────────────────────────────────────────
            plan_text = json.dumps({
                "problem_summary": understand.problem_summary,
                "candidate_files": understand.candidate_files,
                "test_commands": understand.test_commands,
                "repo_structure_summary": understand.repo_structure_summary,
            }, indent=2, ensure_ascii=False)

            plan, steps, tokens = self._run_stage(
                task=task,
                stage_name="plan",
                system_prompt=self._build_system(_PLAN_SYSTEM, task.repo_path),
                task_template=_PLAN_TASK,
                task_kwargs={"understand_output": plan_text, "memory_text": repo_memory_text or "(none)",
                             "repo_path": task.repo_path},
                max_steps=5,
                tools_allowlist={"file_read", "file_view", "find_files"},
                memory_text=repo_memory_text,
                log=log,
            )
            total_steps += steps
            total_tokens += tokens
            logger.info("Stage PLAN: %d targets, %d risks",
                        len(plan.target_files), len(plan.risks))

            # Feasibility gate
            # INFEASIBLE → abort pipeline; NARROW → limit to first target file
            if any("INFEASIBLE" in str(r).upper() for r in plan.risks):
                return PipelineResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=f"Infeasible: {plan.risks}",
                    steps_taken=total_steps,
                    total_tokens=total_tokens,
                    elapsed_seconds=time.time() - t0,
                    understand=understand, plan=plan,
                    stopped_early=True,
                    stop_reason="feasibility_gate:infeasible",
                )
            if any("NARROW" in str(r).upper() for r in plan.risks):
                logger.info("NARROW scope: limiting to first target file (was %d)", len(plan.target_files))
                if plan.target_files:
                    plan.target_files = plan.target_files[:1]
                    if plan.proposed_changes:
                        plan.proposed_changes = [c for c in plan.proposed_changes
                                                  if any(tf["path"] == plan.target_files[0]["path"]
                                                         for tf in plan.target_files)]

            # ── Stage 2: IMPLEMENT ───────────────────────────────────
            plan_json = json.dumps({
                "goal": plan.goal,
                "target_files": plan.target_files,
                "proposed_changes": plan.proposed_changes,
                "risks": plan.risks,
                "validation_notes": plan.validation_notes,
            }, indent=2, ensure_ascii=False)

            impl, steps, tokens = self._run_stage(
                task=task,
                stage_name="implement",
                system_prompt=self._build_system(_IMPLEMENT_SYSTEM, task.repo_path),
                task_template=_IMPLEMENT_TASK,
                task_kwargs={"plan_output": plan_json, "memory_text": repo_memory_text or "(none)",
                             "repo_path": task.repo_path},
                max_steps=12,
                tools_allowlist={"file_read", "file_view", "file_edit", "file_write",
                                 "shell", "test", "git_diff"},
                memory_text=repo_memory_text,
                log=log,
            )
            total_steps += steps
            total_tokens += tokens
            logger.info("Stage IMPLEMENT: status=%s, files=%d",
                        impl.status, len(impl.changed_files))

            # ── Stage 3: VERIFY ──────────────────────────────────────
            impl_json = json.dumps({
                "summary": impl.summary,
                "changed_files": impl.changed_files,
                "test_results": impl.test_results,
                "status": impl.status,
            }, indent=2, ensure_ascii=False)

            verify, steps, tokens = self._run_stage(
                task=task,
                stage_name="verify",
                system_prompt=self._build_system(_VERIFY_SYSTEM, task.repo_path),
                task_template=_VERIFY_TASK,
                task_kwargs={"implement_output": impl_json, "repo_path": task.repo_path},
                max_steps=3,
                tools_allowlist={"file_read", "shell", "git_diff"},
                memory_text="",
                log=log,
            )
            total_steps += steps
            total_tokens += tokens

            # ── Gather final result ──────────────────────────────────
            elapsed = time.time() - t0
            patch = self._get_patch(task.repo_path)
            changed = impl.changed_files or self._get_changed_files(task.repo_path)

            final_status = RunStatus.SUCCESS if verify.verdict == "pass" else RunStatus.GAVE_UP

            return PipelineResult(
                task_id=task.task_id,
                status=final_status,
                summary=verify.summary or impl.summary,
                steps_taken=total_steps,
                total_tokens=total_tokens,
                patch=patch,
                changed_files=changed,
                elapsed_seconds=elapsed,
                understand=understand,
                plan=plan,
                implement=impl,
                verify=verify,
            )

        except Exception as exc:
            logger.exception("Pipeline failed, falling back to standard agent")
            # Fallback: run standard single-stage agent
            try:
                agent_cfg = AgentConfig(
                    max_steps=self._config.agent.max_steps,
                    budget_tokens=self._config.agent.budget_tokens,
                    stream=False,
                )
                agent = Agent(self._backend, self._registry, agent_cfg)
                # Run in a fresh log context
                from agent.event_log import EventLog
                import os as _os
                fallback_log_dir = _os.path.join(self._config.agent.log_dir, "pipeline_fallback")
                with EventLog.create(task, log_dir=fallback_log_dir) as fallback_log:
                    result = agent.run(task, fallback_log, repo_memory_text=repo_memory_text)
                elapsed = time.time() - t0
                return PipelineResult(
                    task_id=task.task_id,
                    status=result.status,
                    summary=result.summary,
                    steps_taken=result.steps_taken,
                    total_tokens=result.total_tokens,
                    patch=result.patch,
                    changed_files=result.changed_files,
                    elapsed_seconds=elapsed,
                    stop_reason=f"pipeline_error_fallback: {exc}",
                )
            except Exception:
                elapsed = time.time() - t0
                return PipelineResult(
                    task_id=task.task_id,
                    status=RunStatus.FAILED,
                    summary=str(exc),
                    steps_taken=total_steps,
                    total_tokens=total_tokens,
                    elapsed_seconds=elapsed,
                    stop_reason=str(exc),
                )

    # ------------------------------------------------------------------
    # Internal: run a single stage
    # ------------------------------------------------------------------

    def _run_stage(
        self,
        *,
        task: Task,
        stage_name: str,
        system_prompt: str,
        task_template: str,
        task_kwargs: dict,
        max_steps: int,
        tools_allowlist: set[str],
        memory_text: str,
        log,
    ):
        """Run one pipeline stage and return (output_dataclass, steps, tokens)."""
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog
        import os as _os

        stage_task_desc = task_template.format(**task_kwargs)

        stage_task = Task(
            description=stage_task_desc,
            repo_path=task.repo_path,
            issue_url=task.issue_url,
            max_steps=max_steps,
            budget_tokens=task.budget_tokens,
        )

        # Build a filtered registry (only allowed tools for this stage)
        filtered = self._build_filtered_registry(tools_allowlist)

        agent_cfg = AgentConfig(
            max_steps=max_steps,
            budget_tokens=task.budget_tokens,
            stream=False,
            reflection_no_edit_steps=999,  # no reflection during pipeline stages
        )
        agent = _PipelineStageAgent(self._backend, filtered, agent_cfg, system_prompt,
                                     token_budget_limit=_STAGE_TOKEN_BUDGETS.get(stage_name, 0))

        stage_log_dir = _os.path.join(self._config.agent.log_dir, "pipeline", stage_name)
        with EventLog.create(stage_task, log_dir=stage_log_dir) as stage_log:
            result = agent.run(stage_task, stage_log, repo_memory_text=memory_text)

        # Extract structured output from the raw LLM response
        raw_output = result.summary or ""

        # Define repair function
        _stage = stage_name  # capture for closure
        def _repair(raw_text: str) -> str:
            try:
                from llm.base import LLMMessage
                prompt_template = _REPAIR_PROMPTS.get(_stage, _REPAIR_PROMPT_FALLBACK)
                prompt = prompt_template.format(raw=raw_text[:3000])
                msgs = [
                    LLMMessage(role="system", content="You are a JSON formatter. Return only valid JSON."),
                    LLMMessage(role="user", content=prompt),
                ]
                resp = self._backend.complete(msgs, [])
                return resp.content if hasattr(resp, "content") else str(resp)
            except Exception:
                return raw_text

        output, _parsed_ok = extract_and_validate(raw_output, stage_name, repair_fn=_repair)
        return output, result.steps_taken, result.total_tokens

    def _build_filtered_registry(self, allowed: set[str]):
        """Return a tool registry containing only allowed tools, plus finish."""
        from tools.base import ToolRegistry
        filtered = ToolRegistry()
        filtered.register(_FinishTool())
        for tool in self._registry.get_all():
            if tool.name in allowed:
                filtered.register(tool)
        return filtered

    def _build_system(self, template: str, repo_path: str) -> str:
        """Fill the system prompt template with repository info."""
        return template.replace("{repo_path}", repo_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_patch(self, repo_path: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True, text=True, timeout=10, cwd=repo_path,
            )
            diff = proc.stdout.strip()
            return diff if diff else None
        except Exception:
            return None

    def _get_changed_files(self, repo_path: str) -> list[str]:
        try:
            proc = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                capture_output=True, text=True, timeout=10, cwd=repo_path,
            )
            return [l.strip() for l in proc.stdout.strip().split("\n") if l.strip()]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Stage-specific Agent subclass — uses a fixed system prompt per stage
# ---------------------------------------------------------------------------

class _FinishTool:
    """Pseudo-tool that tells the agent it can call 'finish' to end the stage.
    Detected in _PipelineStageAgent.run() and converted to ActionType.FINISH."""

    @property
    def name(self) -> str:
        return "finish"

    @property
    def description(self) -> str:
        return (
            "MUST be called to complete this stage and pass results to the next "
            "stage. Call with a JSON message containing your findings. "
            "You MUST call this function once you have enough information — "
            "do NOT keep exploring after you understand the issue."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "JSON object with your findings. Must follow the format "
                        "specified in the system prompt."
                    ),
                },
            },
            "required": ["message"],
        }

    def to_llm_schema(self):
        from llm.base import LLMToolSchema
        return LLMToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters_schema,
        )

    def execute(self, params):
        from tools.base import ToolResult
        return ToolResult(success=True, output="Stage complete.")


class _PipelineStageAgent(Agent):
    """Agent variant that uses a fixed system prompt instead of the default
    build_system_prompt() flow, skips reflection, and passes the stage
    task description directly without wrapping it in build_task_prompt()."""

    def __init__(self, backend, registry, config, fixed_system_prompt: str,
                 token_budget_limit: int = 0):
        super().__init__(backend, registry, config)
        self._stage_system_prompt = fixed_system_prompt
        self._token_budget_limit = token_budget_limit

    def run(self, task, log, repo_memory_text: str = ""):
        """Override: use raw task description (stage template is self-contained)."""
        from context.history import ConversationHistory
        from context.token_budget import TokenBudget
        from context.repo_map import RepoMap
        from llm.base import LLMMessage
        from agent.task import ActionType, RunResult, RunStatus

        self._current_repo_path = task.repo_path
        self._repo_memory_text = repo_memory_text
        cache_key = task.repo_path
        if getattr(self, "_repo_map_cache_key", None) != cache_key:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache
            self._repo_map_cache_key = cache_key
        log.log_task_start(task)

        history = ConversationHistory(max_messages=self._cfg.history_max_messages)
        # Stage task templates are self-contained — no wrapping needed
        history.add(LLMMessage(role="user", content=task.description))
        token_budget = TokenBudget(total=self._cfg.budget_tokens)
        repo_map = RepoMap(task.repo_path)

        total_tokens = 0

        for step in range(1, task.max_steps + 1):
            messages = self._build_messages(history, token_budget, repo_map)

            # On the final step, send NO tools — force the model to output text.
            # Inject a JSON-forcing nudge so the model outputs structured data
            # instead of unstructured reasoning or hallucinated tool calls.
            if step < task.max_steps:
                tools = self._registry.get_schemas()
            else:
                tools = []
                final_nudge = (
                    "## FINAL STEP — NO TOOLS AVAILABLE\n\n"
                    "You have ZERO tools. You CANNOT call any functions. "
                    "Your ONLY option is to output raw text.\n\n"
                    "CRITICAL — output ONLY a JSON object, nothing else:\n"
                    '{ "field1": "value1", "field2": [...] }\n\n'
                    "WHAT YOU MUST NOT OUTPUT:\n"
                    '- BARE FUNCTION CALLS: file_view(path="...")  ← REJECTED\n'
                    '- XML TAGS: <function_result>...</function_result>  ← REJECTED\n'
                    '- MARKDOWN FENCES: ```json {{ }} ```  ← REJECTED\n'
                    '- REASONING TEXT: "I need to..." or "Let me..."  ← REJECTED\n\n'
                    "If you gathered information: output the JSON with your findings.\n"
                    "If not: output JSON with empty fields and explain in problem_summary.\n\n"
                    "OUTPUT THE JSON OBJECT NOW:"
                )
                messages.append(LLMMessage(role="user", content=final_nudge))

            try:
                response = self._call_with_retry(messages, tools)
            except Exception as exc:
                log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
                return RunResult(
                    task_id=task.task_id, status=RunStatus.FAILED,
                    summary=f"LLM call failed: {exc}",
                    steps_taken=step, total_tokens=total_tokens, error=str(exc),
                )

            total_tokens += response.total_tokens
            action = response.action

            # Token budget enforcement: force finish when exceeded
            if self._token_budget_limit > 0 and total_tokens >= self._token_budget_limit:
                budget_msg = (
                    f"TOKEN BUDGET EXCEEDED ({total_tokens}/{self._token_budget_limit}). "
                    f"Call `finish` IMMEDIATELY with your JSON findings. "
                    f"Do NOT explore further."
                )
                history.add(LLMMessage(role="user", content=budget_msg))
                # On next iteration, strip tools to force raw JSON output
                self._token_budget_limit = 0  # only inject once

            log.log_action(step=step, action=action, raw_content=response.raw_content)

            if self._is_looping(log):
                reason = f"Loop detected"
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id, status=RunStatus.GAVE_UP,
                    summary=reason, steps_taken=step, total_tokens=total_tokens,
                )

            if action.action_type == ActionType.FINISH:
                summary = action.message or "Task complete."
                log.log_task_complete(steps=step, summary=summary)
                return RunResult(
                    task_id=task.task_id, status=RunStatus.SUCCESS,
                    summary=summary, steps_taken=step, total_tokens=total_tokens,
                )

            if action.action_type == ActionType.GIVE_UP:
                reason = action.message or "Agent gave up."
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id, status=RunStatus.GAVE_UP,
                    summary=reason, steps_taken=step, total_tokens=total_tokens,
                )

            if action.action_type == ActionType.TOOL_CALL and action.tool_call:
                tc = action.tool_call

                # Intercept 'finish' tool call — treat as stage completion
                if tc.name == "finish":
                    summary = tc.params.get("message", "Stage complete.")
                    log.log_task_complete(steps=step, summary=summary)
                    return RunResult(
                        task_id=task.task_id, status=RunStatus.SUCCESS,
                        summary=summary, steps_taken=step, total_tokens=total_tokens,
                    )

                result = self._registry.execute_tool(tc.name, tc.params)
                observation = result.to_observation(tc.name)

                log.log_observation(step=step, observation=observation)

                history.add(LLMMessage(
                    role="assistant",
                    content=self._format_action_for_history(action),
                ))
                history.add(LLMMessage(
                    role="user",
                    content=self._format_observation_for_history(observation),
                ))

                # If the model tried a disallowed tool, inject corrective guidance
                if observation.error and "Unknown tool" in observation.error:
                    allowed = [t.name for t in self._registry.get_all()]
                    correction = (
                        f"ERROR: The tool '{tc.name}' is NOT available in this stage. "
                        f"You can ONLY use: {', '.join(allowed)}. "
                        f"Do NOT try to call '{tc.name}' again — it will fail. "
                        f"Use one of the available tools instead, or call `finish` "
                        f"if you have enough information."
                    )
                    history.add(LLMMessage(role="user", content=correction))

                # Reminder: when close to step limit, prompt the model to finish
                steps_remaining = task.max_steps - step
                if 1 <= steps_remaining <= 3:
                    reminder = (
                        f"URGENT: Only {steps_remaining} step(s) remain. "
                        f"Call `finish` NOW with your JSON findings. "
                        f"Example: finish(message='{{\"field\": \"value\"}}')\n"
                        f"Do NOT explore further. On the FINAL step, "
                        f"you will have NO tools and must output raw JSON."
                    )
                    history.add(LLMMessage(role="user", content=reminder))

        reason = f"Reached max_steps limit ({task.max_steps})"
        log.log_task_failed(steps=task.max_steps, reason=reason)
        return RunResult(
            task_id=task.task_id, status=RunStatus.MAX_STEPS,
            summary=reason, steps_taken=task.max_steps, total_tokens=total_tokens,
        )

    def _build_messages(self, history, token_budget, repo_map):
        """Override: use the stage-specific system prompt."""
        from agent.prompt import _format_tool_descriptions
        from llm.base import LLMMessage

        if not hasattr(self, "_repo_map_cache"):
            self._repo_map_cache = repo_map.build(
                budget=token_budget.default_plan().repo_map
            )

        schemas = self._registry.get_schemas()
        tool_desc = _format_tool_descriptions(schemas)
        system_content = self._stage_system_prompt + f"\n\n## Available tools\n{tool_desc}"

        trimmed = token_budget.trim_history(
            history.to_dicts(), token_budget.default_plan().history,
        )

        messages = [LLMMessage(role="system", content=system_content)]
        for d in trimmed:
            messages.append(LLMMessage(role=d["role"], content=d["content"]))
        return messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo_name_from_path(repo_path: str) -> str:
    """Best-effort extract owner/repo from local path."""
    import os as _os
    parts = _os.path.normpath(repo_path).split(_os.sep)
    # Try to find a pattern like ".../owner__repo" from memory or ".../repo"
    # For SWE-bench: repos_dir/astropy__astropy → astropy/astropy
    name = parts[-1]
    if "__" in name:
        return name.replace("__", "/", 1)
    return name
