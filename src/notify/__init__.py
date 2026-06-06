"""Notification boundary."""

from .base import Notifier, NotifierError
from .delivery import deliver_digest
from .factory import build_notifier
from .telegram import TelegramNotifier

__all__ = [
    "Notifier",
    "NotifierError",
    "TelegramNotifier",
    "build_notifier",
    "deliver_digest",
]
