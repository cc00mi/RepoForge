"""
pipeline/event_registry.py

Unified GitHub event routing matrix.  Replaces ad-hoc if/elif dispatch
with a declarative registry that maps (event_type, action) → handler.

Every handler receives (payload, auth, config) and returns str.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Handler signature: (payload: dict, auth, config) -> str
Handler = Callable[..., str]


@dataclass
class EventRoute:
    """A single event → handler mapping."""
    event_type: str                # "issues", "pull_request", "check_run", ...
    action: str | None = None      # None = all actions (catch-all)
    handler: Handler | None = None
    risk_level: str = "LOW"        # LOW | MED | HIGH
    description: str = ""

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.event_type, self.action)


class EventRouter:
    """Resolve (event_type, action) pairs to handler callables.

    Resolution order:
      1. Exact match:   (event_type, action)
      2. Catch-all:     (event_type, None)
      3. None (not found)
    """

    def __init__(self, routes: list[EventRoute] | None = None):
        self._exact: dict[tuple[str, str], Handler] = {}
        self._catchall: dict[str, Handler] = {}
        self._routes: list[EventRoute] = []
        if routes:
            for r in routes:
                self.register(r)

    def register(self, route: EventRoute) -> None:
        """Add a route."""
        self._routes.append(route)
        if route.action is None:
            self._catchall[route.event_type] = route.handler
        else:
            self._exact[(route.event_type, route.action)] = route.handler

    def resolve(self, event_type: str, action: str) -> Handler | None:
        """Find the handler for (event_type, action).

        Returns None if no handler is registered.
        """
        # 1. Exact match
        key = (event_type, action)
        if key in self._exact:
            return self._exact[key]
        # 2. Catch-all for this event type
        return self._catchall.get(event_type)

    def list_routes(self) -> list[dict]:
        """Return all registered routes as dicts (for dashboard display)."""
        return [
            {
                "event": r.event_type,
                "action": r.action or "*",
                "risk": r.risk_level,
                "description": r.description,
            }
            for r in self._routes
        ]

    def __contains__(self, event_type: str) -> bool:
        return event_type in self._catchall or any(
            et == event_type for et, _ in self._exact
        )


def build_default_router() -> EventRouter:
    """Build the standard EventRouter with all registered handlers.

    Maps (event_type, action) → handler covering the full GitHub event matrix
    from the design document (§4.10).
    """
    from pipeline.handlers import (
        handle_check_run_failed,
        handle_dependabot_alert,
        handle_installation_created,
        handle_issue_comment_created,
        handle_issue_labeled,
        handle_issues_closed,
        handle_issues_opened,
        handle_pr_closed,
        handle_pr_labeled,
        handle_pull_request_opened,
        handle_pull_request_review_submitted,
        handle_pull_request_synchronize,
        handle_push_tag,
    )

    return EventRouter([
        # ── Issues ──
        EventRoute("issues", "opened", handle_issues_opened, "MED",
                   "Triage + auto-fix for new issues"),
        EventRoute("issues", "labeled", handle_issue_labeled, "LOW",
                   "Check for agent-fix label"),
        EventRoute("issues", "closed", handle_issues_closed, "LOW",
                   "Update RepoMemory outcome status"),
        EventRoute("issue_comment", "created", handle_issue_comment_created, "LOW",
                   "Check for /agent-fix or /agent-review slash commands"),

        # ── Pull Requests ──
        EventRoute("pull_request", "opened", handle_pull_request_opened, "MED",
                   "Full PR review + first-time contributor welcome"),
        EventRoute("pull_request", "synchronize", handle_pull_request_synchronize, "LOW",
                   "Incremental PR review"),
        EventRoute("pull_request", "closed", handle_pr_closed, "LOW",
                   "Update RepoMemory on PR merge"),
        EventRoute("pull_request", "labeled", handle_pr_labeled, "MED",
                   "Auto-merge eligibility check"),

        # ── PR Reviews ──
        EventRoute("pull_request_review", "submitted",
                   handle_pull_request_review_submitted, "HIGH",
                   "Auto-address maintainer review comments (CHANGES_REQUESTED only)"),

        # ── CI ──
        EventRoute("check_run", None, handle_check_run_failed, "HIGH",
                   "CI failure analysis + fix"),

        # ── Releases ──
        EventRoute("push", None, handle_push_tag, "LOW",
                   "Release notes generation on version tag push"),

        # ── Security ──
        EventRoute("dependabot_alert", "created", handle_dependabot_alert, "HIGH",
                   "Security fix PR for dependency alerts"),

        # ── App Lifecycle ──
        EventRoute("installation", "created", handle_installation_created, "LOW",
                   "Welcome + register repos on app install"),
    ])
