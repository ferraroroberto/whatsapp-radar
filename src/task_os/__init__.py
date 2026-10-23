"""Export a radar-marked WhatsApp message to task-os's Inbox.

Read-through, one-way cross-repo dependency: this package POSTs one message to
task-os's ``POST /api/tasks`` (the "WhatsApp side" of task-os's task creation) when the operator
taps Send to Task-OS in the Chats overlay. The one seam is
:func:`src.task_os.client.export_message`; task-os just receives — nothing here
reads task-os state back.
"""

from src.task_os.client import (
    TaskOsError,
    TaskOsNotConfigured,
    export_message,
)

__all__ = [
    "TaskOsError",
    "TaskOsNotConfigured",
    "export_message",
]
