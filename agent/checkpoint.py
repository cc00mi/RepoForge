"""
agent/checkpoint.py

Checkpoint / resume for long-running agent tasks.

Every N steps the agent's full run state — conversation history, code
diff, current step, token usage — is saved to a JSON checkpoint file.
If the process is interrupted (crash, OOM, pre-emption), the run can
be resumed from the last checkpoint.

Usage::

    # In Agent.run():
    ck = CheckpointManager("./checkpoints")
    ck.save(Checkpoint.from_agent(task, history, step, tokens, repo_path))

    # CLI resume:
    cp = CheckpointManager.load("checkpoints/my_task_ckpt.json")
    agent.run(task, log, resume_from=cp)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent.task import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Checkpoint data object
# ---------------------------------------------------------------------------


@dataclass
class Checkpoint:
    """A snapshot of the agent's run state at a specific step."""

    task_id: str
    step: int
    task_description: str
    repo_path: str
    history_dicts: list[dict]           # ConversationHistory.to_dicts()
    total_tokens: int = 0
    steps_without_edit: int = 0
    patch: str | None = None            # git diff at checkpoint time
    changed_files: list[str] = field(default_factory=list)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_agent(
        cls,
        task: Task,
        history_dicts: list[dict],
        step: int,
        total_tokens: int,
        repo_path: str,
        steps_without_edit: int = 0,
        patch: str | None = None,
        changed_files: list[str] | None = None,
    ) -> "Checkpoint":
        return cls(
            task_id=task.task_id,
            step=step,
            task_description=task.description,
            repo_path=repo_path,
            history_dicts=history_dicts,
            total_tokens=total_tokens,
            steps_without_edit=steps_without_edit,
            patch=patch,
            changed_files=changed_files or [],
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "step": self.step,
            "task_description": self.task_description,
            "repo_path": self.repo_path,
            "history_dicts": self.history_dicts,
            "total_tokens": self.total_tokens,
            "steps_without_edit": self.steps_without_edit,
            "patch": self.patch,
            "changed_files": self.changed_files,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Checkpoint":
        return cls(
            task_id=data["task_id"],
            step=data["step"],
            task_description=data["task_description"],
            repo_path=data["repo_path"],
            history_dicts=data["history_dicts"],
            total_tokens=data.get("total_tokens", 0),
            steps_without_edit=data.get("steps_without_edit", 0),
            patch=data.get("patch"),
            changed_files=data.get("changed_files", []),
            timestamp=data.get("timestamp", ""),
        )

    def build_task(self) -> Task:
        """Reconstruct a Task from this checkpoint.

        The step is NOT incremented here — the caller decides the starting step.
        """
        return Task(
            description=self.task_description,
            repo_path=self.repo_path,
            task_id=self.task_id,
        )


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------


class CheckpointManager:
    """Save and load agent run checkpoints to/from JSON files."""

    def __init__(self, directory: str = "./checkpoints") -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self, checkpoint: Checkpoint) -> Path:
        """Persist a checkpoint to disk.

        Returns the file path of the saved checkpoint.
        """
        filename = f"{checkpoint.task_id}_step{checkpoint.step:04d}.json"
        path = self._dir / filename
        payload = json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2)
        path.write_text(payload, encoding="utf-8")
        logger.info("Checkpoint saved: %s (step %d)", filename, checkpoint.step)
        return path

    @staticmethod
    def load(path: str | Path) -> Checkpoint:
        """Load a checkpoint from a JSON file."""
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        return Checkpoint.from_dict(data)

    def list_checkpoints(self, task_id: str | None = None) -> list[Path]:
        """List checkpoint files, optionally filtered by task_id prefix.

        Returns paths sorted by modification time (newest first).
        """
        pattern = f"{task_id}_step*.json" if task_id else "*_step*.json"
        paths = sorted(
            self._dir.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return paths

    def latest(self, task_id: str | None = None) -> Checkpoint | None:
        """Load the most recent checkpoint, optionally for a specific task."""
        paths = self.list_checkpoints(task_id=task_id)
        if not paths:
            return None
        return self.load(paths[0])

    def cleanup(self, task_id: str, keep_latest: int = 3) -> int:
        """Remove old checkpoints for a task, keeping the N most recent.

        Returns the number of files removed.
        """
        paths = self.list_checkpoints(task_id=task_id)
        removed = 0
        for p in paths[keep_latest:]:
            p.unlink()
            removed += 1
        if removed:
            logger.debug("Cleaned up %d old checkpoints for %s", removed, task_id)
        return removed

    @property
    def directory(self) -> Path:
        return self._dir
