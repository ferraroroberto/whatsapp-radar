"""Export a radar-marked WhatsApp message to task-os's Inbox (#307).

One public seam, :func:`export_message`, over task-os's ``POST /api/tasks``
(task-os#98's "WhatsApp side"). Mirrors ``src/analysis/summarize.py``'s shape —
same shared :mod:`src._loopback_http` plumbing (a same-host sibling service, per
that module's own docstring), same "typed error the router re-raises as its own
HTTP status" contract — rather than reinventing it.

task-os's ``POST /api/tasks`` does not yet dedupe on ``external_id`` (its own
``TaskCreate`` model has no such field today, task-os#98) — it is sent anyway so
a future task-os release picks it up for free with no radar-side change. Until
then, idempotency is enforced entirely on this side: the router never re-posts a
message whose ``messages.task_exported_at`` is already set (see
:func:`src.db.messages.message_task_export_context`).
"""

from __future__ import annotations

from typing import Any

from src import _loopback_http
from src.config import TaskOsConfig
from src.models import TaskExportContext

# The `actor` task-os stores as a task's `created_by` — distinct from any human
# team member so an exported task is clearly attributable to this integration.
ACTOR = "whatsapp-radar"

# A task list row has limited width; keep the title to one line and fall back to
# the full text in the description.
_TITLE_MAX = 120


class TaskOsError(_loopback_http.LoopbackError):
    """Raised when task-os is unreachable or returns an error response."""


class TaskOsNotConfigured(TaskOsError):
    """task-os export is disabled, or missing its bearer token."""


def tasks_url(base_url: str) -> str:
    """The upstream ``POST /api/tasks`` endpoint for ``base_url``."""
    return f"{base_url.rstrip('/')}/api/tasks"


def build_title(context: TaskExportContext) -> str:
    """A one-line title from the message text, collapsed and length-capped."""
    text = " ".join(context.text.split())
    if len(text) > _TITLE_MAX:
        text = text[: _TITLE_MAX - 1].rstrip() + "…"
    return text


def build_description(context: TaskExportContext) -> str:
    """Sender + chat + timestamp header, then the full message text."""
    sender = context.sender_label or "Unknown sender"
    return (
        f"From WhatsApp: {sender} · {context.chat_name} · {context.message_timestamp}"
        f"\n\n{context.text}"
    )


def build_task_payload(context: TaskExportContext) -> dict[str, Any]:
    """The ``POST /api/tasks`` body for one message (task-os's ``TaskCreate`` shape)."""
    return {
        "title": build_title(context),
        "description": build_description(context),
        # Forward-compatible: silently ignored by task-os's TaskCreate model
        # today (extra fields aren't rejected), honoured once task-os#98 lands.
        "external_id": context.source_message_id,
        "actor": ACTOR,
    }


def export_message(config: TaskOsConfig, context: TaskExportContext) -> dict[str, Any]:
    """POST one message to task-os as an Inbox task; returns the created task.

    Raises :class:`TaskOsNotConfigured` (status 400) when export is disabled or
    the bearer token is unset, :class:`TaskOsError` (status 503) when task-os is
    unreachable, or with task-os's own status when it answers ``>= 400``.
    """
    if not config.enabled or not config.token:
        raise TaskOsNotConfigured(
            "task-os export is not configured (enable it and set WR_TASK_OS_TOKEN)",
            status=400,
        )
    result: dict[str, Any] = _loopback_http.request(
        "POST",
        tasks_url(config.base_url),
        error=TaskOsError,
        service="task-os",
        timeout=config.timeout_s,
        json=build_task_payload(context),
        headers={"Authorization": f"Bearer {config.token}"},
        allow_empty=False,
    )
    return result
