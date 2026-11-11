"""The delivery status vocabulary, declared once.

One word says how a notification (or a whole run's delivery) ended. A single send
ends in ``sent``, ``skipped`` or ``failed``; a run's ``notification_status`` adds the
words for "nothing to send" and "the run stopped before it could deliver".
Every reader asks :func:`run_delivery_ok` instead of spelling the failing words
itself, so a new failing word is added here and nowhere else.
"""

from __future__ import annotations

#: One send went out.
DELIVERY_SENT = "sent"
#: No notifier is configured, so nothing was attempted.
DELIVERY_SKIPPED = "skipped"
#: The notifier was misconfigured or the send raised.
DELIVERY_FAILED = "failed"
#: The run had nothing actionable to deliver.
DELIVERY_NONE = "none"
#: A dry run withholds the digest rather than sending it.
DELIVERY_DRY_RUN = "dry_run"
#: The run stopped because every enabled *source* was unreachable.
DELIVERY_OFFLINE = "offline"
#: The run stopped because Stage 2 could not reach the hub. Distinct from
#: ``offline`` so the audit trail names which end of the pipeline failed.
DELIVERY_CLASSIFIER_OFFLINE = "classifier_offline"

#: What one send can end in; the only words a ``notifications`` row carries.
SEND_STATUSES: frozenset[str] = frozenset({DELIVERY_SENT, DELIVERY_SKIPPED, DELIVERY_FAILED})

DELIVERY_STATUSES: frozenset[str] = SEND_STATUSES | {
    DELIVERY_NONE,
    DELIVERY_DRY_RUN,
    DELIVERY_OFFLINE,
    DELIVERY_CLASSIFIER_OFFLINE,
}

#: The words that make a run's delivery count as not ok.
FAILING_STATUSES: frozenset[str] = frozenset(
    {DELIVERY_FAILED, DELIVERY_OFFLINE, DELIVERY_CLASSIFIER_OFFLINE}
)


def run_delivery_ok(status: str) -> bool:
    """True unless ``status`` says delivery failed or the run stopped before it could."""
    return status not in FAILING_STATUSES


def withheld_status(*, dry_run: bool, deliver: bool, has_actionable: bool) -> str | None:
    """How a run that sends no digest ends, or ``None`` when its digest is due.

    A dry run, or a run whose ``deliver`` is off (the ``process`` verb's own
    ``--dry-run``), withholds the digest; a run with nothing actionable has none.
    """
    if dry_run:
        return DELIVERY_DRY_RUN
    if not deliver:
        return DELIVERY_DRY_RUN if has_actionable else DELIVERY_NONE
    return None if has_actionable else DELIVERY_NONE


def require_send_status(status: str) -> str:
    """Return ``status`` when a single send can end in it, else raise ``ValueError``."""
    if status not in SEND_STATUSES:
        raise ValueError(f"{status!r} is not a send status")
    return status
