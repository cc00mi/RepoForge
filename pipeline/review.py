"""
pipeline/review.py

专用 PR 代码审查模块。

功能：
- 拉取 PR diff（GitHub API 或本地 git）
- 运行 agent 做结构化代码审查
- 解析审查结果为分级 findings
- 通过 GitHub PR Review API 提交正式 review（APPROVE / REQUEST_CHANGES / COMMENT）

用法：
    # 对真实 PR 做 review
    python -m pipeline.review --repo owner/repo --pr 42

    # 对本地分支做 review（不推送到 GitHub）
    python -m pipeline.review --local --base main --head feature-branch

    # CLI 快捷入口
    repoforge-pipe review --repo owner/repo --pr 42
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    CRITICAL = "CRITICAL"  # security hole, data loss, must-fix
    HIGH = "HIGH"          # bug, broken functionality
    MEDIUM = "MEDIUM"      # code smell, performance issue
    LOW = "LOW"            # style nit, minor improvement
    SUGGESTION = "SUGGESTION"  # optional enhancement idea


@dataclass
class ReviewFinding:
    """一条审查发现。"""
    severity: Severity
    file_path: str
    line: int
    message: str
    suggestion: str = ""

    def to_markdown(self) -> str:
        icon = {
            Severity.CRITICAL: "[!]",
            Severity.HIGH: "[X]",
            Severity.MEDIUM: "[~]",
            Severity.LOW: "[-]",
            Severity.SUGGESTION: "[i]",
        }.get(self.severity, "[*]")
        lines = [
            f"### {icon} [{self.severity.value}] `{self.file_path}:{self.line}`",
            f"**Issue:** {self.message}",
        ]
        if self.suggestion:
            lines.append(f"**Suggestion:** {self.suggestion}")
        return "\n".join(lines)


@dataclass
class ReviewReport:
    """一次完整审查的汇总。"""
    findings: list[ReviewFinding] = field(default_factory=list)
    summary: str = ""
    stats: dict = field(default_factory=dict)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.HIGH)

    @property
    def total_count(self) -> int:
        return len(self.findings)

    def is_clean(self) -> bool:
        return self.total_count == 0

    def needs_changes(self) -> bool:
        """有 CRITICAL 或 HIGH 需要强制修改。"""
        return self.critical_count > 0 or self.high_count > 0

    def to_markdown(self) -> str:
        """生成完整的审查报告 Markdown。"""
        if self.is_clean():
            return (
                "## Code Review: All Clear\n\n"
                "No issues found. The code looks good.\n\n"
                f"{self.summary}"
            )

        lines = [
            "## Automated Code Review",
            "",
            "| Severity | Count |",
            "|----------|-------|",
            f"| CRITICAL | {self.critical_count} |",
            f"| HIGH     | {self.high_count} |",
            f"| MEDIUM   | {sum(1 for f in self.findings if f.severity == Severity.MEDIUM)} |",
            f"| LOW      | {sum(1 for f in self.findings if f.severity == Severity.LOW)} |",
            f"| SUGGESTION | {sum(1 for f in self.findings if f.severity == Severity.SUGGESTION)} |",
            "",
        ]
        if self.summary:
            lines.append(f"**Summary:** {self.summary}")
            lines.append("")

        # 按严重度分组
        for sev in [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM,
                     Severity.LOW, Severity.SUGGESTION]:
            sev_findings = [f for f in self.findings if f.severity == sev]
            if sev_findings:
                lines.append(f"---")
                for f in sev_findings:
                    lines.append(f.to_markdown())
                    lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Review system prompt
# ---------------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = """You are an expert code reviewer. Your task is to review code changes
in a pull request and produce structured findings.

## Rules
- Only READ files. Do NOT edit, write, or delete anything.
- Analyze the DIFF carefully. Look for:
  1. **Security** — injection, XSS, auth bypass, exposed secrets, unsafe deserialization
  2. **Correctness** — logic errors, off-by-one, null handling, edge cases, race conditions
  3. **Performance** — N+1 queries, unnecessary allocations, blocking I/O, missing caching
  4. **Robustness** — missing error handling, unvalidated input, missing tests
  5. **Style** — naming, DRY violations, overly complex functions, dead code
- For each issue: reference the exact file path and line number.
- Be specific. "This could be better" is not a review.

## Output Format
When you finish your review, call FINISH with a summary in this format:

```
REVIEW REPORT
CRITICAL/NONE  (number of critical issues found)
HIGH/NONE  (number of high issues found)
MEDIUM/NONE  (number of medium issues found)
LOW/NONE  (number of low issues found)

FINDINGS:
SEVERITY: <level>
FILE: <path>
LINE: <number>
MESSAGE: <description of the issue>
SUGGESTION: <how to fix it>
---END---

OVERALL: <1-3 sentence summary of the review>
```

If no issues found at all, write: "OVERALL: No issues found. Code looks good."

## Important
- If the diff is empty or tiny, say so and finish quickly.
- Do not make up issues. Only flag real problems you actually see in the code.
- Focus on substance over style unless style issues are significant.
"""

REVIEW_TASK_TEMPLATE = """Review the following pull request.

## PR: {title}
{body}

## Changed Files
{file_list}

## Diff Preview (first 6000 chars of the unified diff)
```
{diff_preview}
```

## Instructions
1. The full diff is in `.agent_review_diff.txt` — read it for complete context.
2. Review each changed file for: security issues, bugs, performance problems, robustness gaps, and style violations.
3. Be efficient — read only the files that look suspicious in the diff, not every file.
4. Aim to finish in 10 steps or fewer. Quality over quantity.
5. Call FINISH with your findings in the structured format from the system prompt.

Remember: READ ONLY. Do not edit any files."""


# ---------------------------------------------------------------------------
# Diff / file helpers
# ---------------------------------------------------------------------------

def get_pr_diff_github(repo_full_name: str, pr_number: int, token: str) -> str:
    """通过 GitHub API 获取 PR 的 unified diff。"""
    import requests

    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.text


def get_pr_files_github(repo_full_name: str, pr_number: int, token: str) -> list[dict]:
    """获取 PR 变更文件列表。"""
    import requests

    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/files"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()


def get_local_diff(repo_dir: str | Path, base: str, head: str) -> str:
    """获取本地仓库两个分支/commit 间的 diff。"""
    repo_dir = str(repo_dir)
    # 先确认仓库存在
    if not (Path(repo_dir) / ".git").exists():
        return ""

    proc = subprocess.run(
        ["git", "diff", f"{base}..{head}"],
        cwd=repo_dir, capture_output=True, text=False, timeout=60,
    )
    if proc.returncode != 0:
        return ""
    # 用 UTF-8 解码（Windows 上 git diff 不一定是 GBK）
    raw = proc.stdout
    for encoding in ["utf-8", "gbk", "latin-1"]:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

_FINDING_RE = re.compile(
    r"SEVERITY:\s*(\w+)\s*\n"
    r"FILE:\s*(.+?)\s*\n"
    r"LINE:\s*(\d+)\s*\n"
    r"MESSAGE:\s*(.+?)\s*\n"
    r"SUGGESTION:\s*(.*?)(?:\n---END---|\nSEVERITY:|\nOVERALL:|\Z)",
    re.DOTALL | re.IGNORECASE,
)

_OVERALL_RE = re.compile(r"OVERALL:\s*(.+?)(?:\n\Z|\Z)", re.DOTALL)


def _parse_severity(label: str) -> Severity:
    label_upper = label.strip().upper()
    for s in Severity:
        if s.value == label_upper:
            return s
    return Severity.LOW


def parse_review_output(text: str) -> ReviewReport:
    """从 agent 输出中解析结构化 ReviewReport。

    支持两种格式：
    1. 结构化格式（SEVERITY: / FILE: / LINE: / MESSAGE: / SUGGESTION:）
    2. Markdown 自由格式回退解析
    """
    findings: list[ReviewFinding] = []

    # 先尝试结构化解析
    for m in _FINDING_RE.finditer(text):
        sev_str = m.group(1).strip()
        if sev_str.upper() == "NONE":
            continue
        try:
            severity = _parse_severity(sev_str)
        except Exception:
            severity = Severity.MEDIUM

        file_path = m.group(2).strip()
        try:
            line = int(m.group(3).strip())
        except ValueError:
            line = 0
        message = m.group(4).strip()
        suggestion = m.group(5).strip()

        findings.append(ReviewFinding(
            severity=severity,
            file_path=file_path,
            line=line,
            message=message,
            suggestion=suggestion,
        ))

    # 如果结构化解析无结果，尝试 markdown 自由格式解析
    if not findings:
        findings = _parse_markdown_findings(text)

    # 解析 OVERALL
    overall_match = _OVERALL_RE.search(text)
    summary = overall_match.group(1).strip() if overall_match else ""
    if not summary and not findings:
        summary = text.strip()[:1000]

    stats = {
        "total": len(findings),
        "critical": sum(1 for f in findings if f.severity == Severity.CRITICAL),
        "high": sum(1 for f in findings if f.severity == Severity.HIGH),
        "medium": sum(1 for f in findings if f.severity == Severity.MEDIUM),
        "low": sum(1 for f in findings if f.severity == Severity.LOW),
        "suggestion": sum(1 for f in findings if f.severity == Severity.SUGGESTION),
    }

    return ReviewReport(findings=findings, summary=summary, stats=stats)


_MD_FINDING_RE = re.compile(
    r"(?:###\s*|####\s*|(?:Bug|Issue|Problem)\s*\d*:?\s*)"
    r"(?:\[!\]|\[X\]|\[~\]|\[-\]|\[i\]|\[\\*\])?\s*"
    r"(?:\[(CRITICAL|HIGH|MEDIUM|LOW|SUGGESTION)\]\s*)?"
    r".*?`([^`]+):(\d+)`[^\n]*\n"
    r".*?\*\*Issue:\*\*\s*(.+?)(?:\n|$)"
    r"(?:\s*\*\*Suggestion:\*\*\s*(.+?)(?:\n###|\n####|\n---|\n\Z|$))?",
    re.DOTALL | re.IGNORECASE,
)

_SIMPLE_BUG_RE = re.compile(
    r"(?:###|####)\s+(?:Bug|Issue|Problem)\s*\d*:?\s*(.+?)\n"
    r".*?\*\*File:\*\*\s*(?:`)?([^`\n]+)(?:`)?[^\n]*\n"
    r".*?\*\*(?:Issue|Problem|Description):\*\*\s*(.+?)(?:\n|$)"
    r"(?:\s*\*\*(?:Suggestion|Fix|Recommendation):\*\*\s*(.+?))?"
    r"(?:\n###|\n####|\nBug|\nIssue|\n---|\n\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _parse_markdown_findings(text: str) -> list[ReviewFinding]:
    """从 markdown 格式的 review 输出中提取 findings。"""
    findings: list[ReviewFinding] = []

    # 方法1: 解析表格行（| # | Issue | File | Severity |）
    table_re = re.compile(
        r"\|\s*\d+\s*\|\s*(.+?)\s*\|\s*(?:`)?([^`|\n]+?)(?:`)?\s*\|\s*(\w+)\s*\|",
        re.IGNORECASE,
    )
    for m in table_re.finditer(text):
        message = m.group(1).strip()
        file_path = m.group(2).strip()
        sev_str = m.group(3).strip()
        severity = _parse_severity(sev_str)
        findings.append(ReviewFinding(
            severity=severity, file_path=file_path, line=0,
            message=message, suggestion="",
        ))

    if findings:
        return findings

    # 方法2: 按行解析 "Bug N: description / File: path / Severity: level" 格式
    for m in _SIMPLE_BUG_RE.finditer(text):
        title = m.group(1).strip()
        file_path = m.group(2).strip()
        message = m.group(3).strip()
        suggestion = (m.group(4) or "").strip()
        # 从标题推断 severity
        sev = Severity.MEDIUM
        title_lower = title.lower()
        if "security" in title_lower or "critical" in title_lower:
            sev = Severity.HIGH
        elif "performance" in title_lower:
            sev = Severity.LOW
        findings.append(ReviewFinding(
            severity=sev, file_path=file_path, line=0,
            message=f"{title}: {message}", suggestion=suggestion,
        ))

    if findings:
        return findings

    # 方法3: 尝试按 heading 分段，每个 heading 可能是一个 finding
    sections = re.split(r"\n(?=###?\s+)", text)
    for section in sections:
        heading_match = re.match(r"###?\s*(.+?)(?:\n|$)", section)
        if not heading_match:
            continue
        heading = heading_match.group(1).strip()
        # 跳过非 finding 的 heading
        if any(skip in heading.lower() for skip in
               ["summary", "review", "overview", "code review",
                "all clear", "no issues", "instructions"]):
            continue

        # 提取 file:line 引用
        file_match = re.search(r"(?:\*\*File:\*\*\s*`?([^`\n]+)`?|`([^`]+):(\d+)`\s*[-\*]\s*)", section)
        sev_match = re.search(r"(?:\*\*Severity:\*\*\s*(\w+)|severity.*?(\w+))", section, re.IGNORECASE)

        file_path = ""
        line = 0
        if file_match:
            if file_match.group(1):
                file_path = file_match.group(1).strip()
            if file_match.group(2):
                file_path = file_match.group(2).strip()
            if file_match.group(3):
                try:
                    line = int(file_match.group(3))
                except ValueError:
                    pass

        severity = Severity.MEDIUM
        if sev_match:
            sev_str = sev_match.group(1) or sev_match.group(2) or ""
            severity = _parse_severity(sev_str)

        # 取 section 的第一段非 heading 文本作为 message
        body_lines = section.strip().split("\n")[1:]
        # 跳过空行和格式化行
        body = " ".join(
            l.strip() for l in body_lines
            if l.strip() and not l.strip().startswith("```")
        )[:300]

        if file_path or (body and len(body) > 20):
            findings.append(ReviewFinding(
                severity=severity, file_path=file_path, line=line,
                message=heading + (f" — {body}" if body and body != heading else ""),
                suggestion="",
            ))

    return findings


# ---------------------------------------------------------------------------
# Main review runner
# ---------------------------------------------------------------------------

def run_review(
    *,
    repo_full_name: str | None = None,
    pr_number: int | None = None,
    repo_dir: str | Path | None = None,
    base: str = "main",
    head: str = "HEAD",
    token: str | None = None,
    backend=None,
    config=None,
) -> ReviewReport:
    """
    运行代码审查 agent。

    支持两种模式：
    1. GitHub PR 模式——通过 API 拉 diff
    2. 本地模式——直接对比两个分支

    Returns:
        ReviewReport 包含所有 findings
    """
    from agent.core import Agent, AgentConfig
    from agent.event_log import EventLog
    from agent.task import Task
    from entry.cli import _build_registry

    if config is None:
        from config.schema import load_config
        config = load_config()
    if backend is None:
        from llm.router import create_backend_from_config
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "model": config.llm.model,
            "api_key": config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })

    # 获取 diff 和文件列表
    if repo_full_name and pr_number and token:
        # GitHub PR 模式
        diff_text = get_pr_diff_github(repo_full_name, pr_number, token)
        files = get_pr_files_github(repo_full_name, pr_number, token)
        file_names = [f["filename"] for f in files[:50]]
        pr_title = f"PR #{pr_number} in {repo_full_name}"
        pr_body = ""
    elif repo_dir:
        # 本地模式
        repo_dir = str(repo_dir)
        diff_text = get_local_diff(repo_dir, base, head)
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{base}..{head}"],
            cwd=repo_dir, capture_output=True, text=False, timeout=30,
        )
        raw = proc.stdout
        file_list_text = raw.decode("utf-8", errors="replace")
        file_names = [
            f.strip() for f in file_list_text.strip().split("\n") if f.strip()
        ][:50]
        pr_title = f"Local diff: {base}..{head}"
        pr_body = ""
    else:
        raise ValueError("Either (repo_full_name, pr_number, token) or repo_dir required")

    # 构建任务
    diff_preview_text = diff_text[:6000] if diff_text else "(no diff available)"
    file_list_str = "\n".join(f"  - {fn}" for fn in file_names)
    task_desc = REVIEW_TASK_TEMPLATE.format(
        title=pr_title,
        body=pr_body,
        file_list=file_list_str if file_list_str else "(no files changed)",
        diff_preview=diff_preview_text,
    )

    # 写 diff 到临时文件，供 agent 读取
    if repo_dir:
        diff_path = Path(repo_dir) / ".agent_review_diff.txt"
    else:
        diff_path = Path("./pipeline_repos/.agent_review_diff.txt")
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_text = diff_text or ""
    diff_path.write_text(diff_text[:30000], encoding="utf-8")

    task_desc += f"\n\nThe full diff is at `.agent_review_diff.txt` ({len(diff_text)} chars). Read this file first."

    registry = _build_registry(config)
    # Review 需要足够步数读完文件并输出 findings
    review_max_steps = max(25, min(config.agent.max_steps, 35))
    agent_cfg = AgentConfig(
        max_steps=review_max_steps,
        budget_tokens=config.agent.budget_tokens,
        stream=True,
    )
    agent = Agent(backend, registry, agent_cfg)

    task = Task(
        description=task_desc,
        repo_path=str(repo_dir) if repo_dir else "./pipeline_repos",
        max_steps=agent_cfg.max_steps,
        budget_tokens=agent_cfg.budget_tokens,
    )

    # 运行 agent
    log_dir = os.path.join(config.agent.log_dir, "review")
    with EventLog.create(task, log_dir=log_dir) as log:
        result = agent.run(task, log)

    # 解析输出
    raw_output = result.summary or ""
    report = parse_review_output(raw_output)

    # 补充分回退——如果解析不出结构化 findings，把整段输出当 summary
    if report.is_clean() and raw_output.strip():
        report.summary = raw_output.strip()

    report.stats["steps_taken"] = result.steps_taken
    report.stats["tokens_used"] = result.total_tokens

    # 清理临时文件
    try:
        diff_path.unlink()
    except Exception:
        pass

    return report


# ---------------------------------------------------------------------------
# GitHub Review API
# ---------------------------------------------------------------------------

def submit_github_review(
    repo_full_name: str,
    pr_number: int,
    report: ReviewReport,
    token: str,
) -> str:
    """
    向 GitHub 提交正式的 PR Review。

    Returns:
        Review URL 或错误描述
    """
    import requests

    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 决定 review event 类型
    if report.needs_changes():
        event = "REQUEST_CHANGES"
    elif report.is_clean():
        event = "APPROVE"
    else:
        event = "COMMENT"

    # 构建 inline comments
    comments: list[dict] = []
    for f in report.findings:
        if f.line > 0 and f.file_path:
            comments.append({
                "path": f.file_path,
                "line": f.line,
                "side": "RIGHT",
                "body": (
                    f"**{f.severity.value}:** {f.message}\n\n"
                    + (f"*Suggestion:* {f.suggestion}" if f.suggestion else "")
                ),
            })

    body_text = report.to_markdown()
    body_text += f"\n\n---\n*Automated review by Repoforge*"

    payload = {
        "body": body_text,
        "event": event,
    }
    if comments:
        payload["comments"] = comments[:50]  # GitHub 限制单次 review 最多约 50 条

    resp = requests.post(url, headers=headers, json=payload, timeout=60)

    if resp.status_code >= 400:
        error = resp.json() if resp.text else {}
        msg = error.get("message", resp.text)
        logger.error("Failed to submit review: %s", msg)
        return f"Error: {msg}"

    data = resp.json()
    review_url = data.get("html_url", "unknown")
    logger.info("Review submitted: %s (event=%s)", review_url, event)
    return review_url


# ---------------------------------------------------------------------------
# Standalone entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="PR Auto-Review")
    p.add_argument("--repo", help="owner/repo")
    p.add_argument("--pr", type=int, help="PR number")
    p.add_argument("--local", action="store_true", help="Local mode (use --base and --head)")
    p.add_argument("--base", default="main", help="Base branch (local mode)")
    p.add_argument("--head", default="HEAD", help="Head branch (local mode)")
    p.add_argument("--repo-dir", default=".", help="Local repo directory")
    p.add_argument("--submit", action="store_true", help="Submit review to GitHub")

    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")

    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GITHUB_APP_PRIVATE_KEY", "")

    if args.local:
        report = run_review(
            repo_dir=args.repo_dir, base=args.base, head=args.head,
        )
    elif args.repo and args.pr and token:
        # 用 token 做 API 调用获取 diff
        diff = get_pr_diff_github(args.repo, args.pr, token)
        files = get_pr_files_github(args.repo, args.pr, token)
        # 把 diff 写到临时目录然后跑本地模式
        tmp_dir = Path("./pipeline_repos/_review_pr")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        diff_path = tmp_dir / ".agent_review_diff.txt"
        diff_path.write_text(diff[:30000], encoding="utf-8")

        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog
        from agent.task import Task
        from config.schema import load_config
        from entry.cli import _build_registry
        from llm.router import create_backend_from_config

        config = load_config()
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "model": config.llm.model,
            "api_key": config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })

        report = run_review(
            repo_full_name=args.repo,
            pr_number=args.pr,
            repo_dir=str(tmp_dir),
            token=token,
            backend=backend, config=config,
        )

        if args.submit and token:
            review_url = submit_github_review(args.repo, args.pr, report, token)
            print(f"\nReview submitted: {review_url}")
    else:
        p.error("Use --local OR (--repo + --pr with GITHUB_TOKEN/GITHUB_APP_PRIVATE_KEY)")

    # 打印报告
    print(f"\n{report.to_markdown()}")
    print(f"\nStats: {json.dumps(report.stats, indent=2)}")
