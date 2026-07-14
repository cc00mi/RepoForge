"""
pipeline/security.py

Security advisory + Dependabot alert response.

Flow:
  1. Parse vulnerability (package, severity, CVE, fixed version)
  2. Check if repo uses the affected package
  3. For CRITICAL/HIGH: auto-create bump PR with test suite run
  4. For MEDIUM/LOW: comment with analysis for manual review
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_dependabot_alert(payload: dict) -> dict:
    """Extract key fields from a Dependabot alert webhook payload.

    Returns dict with: package_name, severity, ecosystem, fixed_version,
    cve_id, advisory_summary, affected_path.
    """
    alert = payload.get("alert", {})
    advisory = alert.get("security_advisory", {})
    vuln = alert.get("security_vulnerability", {})

    return {
        "package_name": advisory.get("summary", ""),
        "package_ecosystem": vuln.get("package", {}).get("ecosystem", ""),
        "package_name_raw": vuln.get("package", {}).get("name", ""),
        "severity": advisory.get("severity", "medium"),  # critical|high|medium|low
        "fixed_version": vuln.get("first_patched_version", {}).get("identifier", ""),
        "cve_id": advisory.get("cve_id", ""),
        "advisory_summary": advisory.get("description", "")[:500],
        "ghsa_id": advisory.get("ghsa_id", ""),
        "affected_range": vuln.get("vulnerable_version_range", ""),
    }


# ---------------------------------------------------------------------------
# Package usage check
# ---------------------------------------------------------------------------

def _check_python_dependency(
    repo_dir: str, package_name: str,
) -> bool:
    """Check if a Python package is in requirements files."""
    import re

    patterns = [
        "requirements.txt", "requirements/*.txt", "requirements-*.txt",
        "setup.py", "setup.cfg", "pyproject.toml", "Pipfile", "Pipfile.lock",
        "poetry.lock",
    ]

    name_lower = package_name.lower().replace("-", "[-_]").replace(".", r"\.")
    dep_re = re.compile(rf'^\s*{name_lower}\b', re.IGNORECASE | re.MULTILINE)

    for pattern in patterns:
        for f in Path(repo_dir).rglob(pattern):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                if dep_re.search(content):
                    return True
            except Exception:
                pass

    return False


def check_package_usage(repo_dir: str, package_name: str) -> dict:
    """Check if a vulnerable package is used in the repo.

    Returns dict with: in_use (bool), files (list of str), ecosystem (str).
    """
    in_use = _check_python_dependency(repo_dir, package_name)

    return {
        "in_use": in_use,
        "ecosystem": "python",
    }


# ---------------------------------------------------------------------------
# Dependency bump
# ---------------------------------------------------------------------------

def create_dependency_bump_pr(
    repo_dir: str,
    package_name: str,
    fixed_version: str,
    severity: str,
    cve_id: str = "",
    advisory_summary: str = "",
) -> tuple[bool, str]:
    """Attempt to bump a dependency to the patched version.

    Args:
        repo_dir: path to cloned repo
        package_name: the vulnerable package name
        fixed_version: the patched version identifier
        severity: critical|high|medium|low
        cve_id: CVE identifier if available
        advisory_summary: description of the vulnerability

    Returns:
        (success, message)
    """
    try:
        # Try pip install --upgrade
        proc = subprocess.run(
            ["pip", "install", "--upgrade", f"{package_name}>={fixed_version}"],
            cwd=repo_dir, capture_output=True, text=True, timeout=120,
        )

        if proc.returncode != 0:
            return False, f"Failed to upgrade {package_name}: {proc.stderr[:300]}"

        # Update requirements files
        updated = _update_requirement_files(repo_dir, package_name, fixed_version)

        if not updated:
            return False, f"Could not find {package_name} in any requirements file"

        # Run test suite
        test_result = _run_tests(repo_dir)

        return True, (
            f"Upgraded {package_name} to >={fixed_version}. "
            f"Test result: {test_result}"
        )

    except Exception:
        logger.exception("Dependency bump failed")
        return False, "Exception during dependency bump"


def _update_requirement_files(
    repo_dir: str, package_name: str, fixed_version: str,
) -> bool:
    """Update package version in requirements files. Returns True if any file was changed."""
    import re

    name_pattern = package_name.lower().replace("-", "[-_]").replace(".", r"\.")
    updated = False

    req_patterns = ["requirements.txt", "requirements/*.txt", "requirements-*.txt"]
    for pattern in req_patterns:
        for f in Path(repo_dir).rglob(pattern):
            try:
                content = f.read_text(encoding="utf-8")
                new_content = re.sub(
                    rf'^({name_pattern}\s*[><=!~]+\s*)[\d.*]+',
                    rf'\g<1>{fixed_version}',
                    content,
                    flags=re.MULTILINE | re.IGNORECASE,
                )
                # Also handle unversioned: package_name → package_name>=fixed_version
                new_content = re.sub(
                    rf'^({name_pattern})\s*$',
                    rf'\g<1>=={fixed_version}',
                    new_content,
                    flags=re.MULTILINE | re.IGNORECASE,
                )
                if new_content != content:
                    f.write_text(new_content, encoding="utf-8")
                    updated = True
            except Exception:
                pass

    return updated


def _run_tests(repo_dir: str) -> str:
    """Run the project's test suite and return a summary."""
    try:
        # Try pytest first
        proc = subprocess.run(
            ["python", "-m", "pytest", "-x", "--tb=short", "-q"],
            cwd=repo_dir, capture_output=True, text=True, timeout=300,
        )
        lines = proc.stdout.strip().split("\n")
        # Return last 5 lines (summary)
        return "\n".join(lines[-5:]) if lines else "No output"
    except subprocess.TimeoutExpired:
        return "Test suite timed out"
    except Exception:
        return "Could not run test suite"


# ---------------------------------------------------------------------------
# Impact assessment comment
# ---------------------------------------------------------------------------

def render_security_comment(parsed: dict, in_use: bool) -> str:
    """Generate a markdown comment for the security advisory."""
    if not in_use:
        return (
            f"## Security Advisory: {parsed.get('cve_id', parsed.get('ghsa_id', '?'))}\n\n"
            f"Package `{parsed['package_name_raw']}` ({parsed['severity'].upper()}) "
            f"is **not used** in this repository.\n\n"
            f"No action needed."
        )

    if parsed["severity"] in ("critical", "high"):
        return (
            f"## Security Fix: {parsed.get('cve_id', parsed.get('ghsa_id', '?'))}\n\n"
            f"**Severity:** {parsed['severity'].upper()}\n\n"
            f"**Package:** `{parsed['package_name_raw']}`\n"
            f"**Patched version:** `{parsed['fixed_version']}`\n\n"
            f"**Summary:** {parsed['advisory_summary']}\n\n"
            f"---\n"
            f"A fix PR has been created by RepoForge to bump to the patched version."
        )

    return (
        f"## Security Advisory: {parsed.get('cve_id', parsed.get('ghsa_id', '?'))}\n\n"
        f"**Severity:** {parsed['severity'].upper()} (auto-fix not triggered)\n\n"
        f"**Package:** `{parsed['package_name_raw']}`\n"
        f"**Patched version:** `{parsed['fixed_version']}`\n\n"
        f"**Summary:** {parsed['advisory_summary']}\n\n"
        f"---\n"
        f"This is {parsed['severity']} severity. A maintainer should review and decide on action."
    )
