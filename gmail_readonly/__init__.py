"""Portable, read-only Gmail OAuth and search component."""

from gmail_readonly.core import (
    GMAIL_READONLY_SCOPE,
    GmailLabel,
    GmailMailbox,
    GmailProfile,
    GmailReadClient,
    GmailReadError,
    GmailSearch,
    GmailSender,
    GmailSource,
    NormalizedEmail,
    masked_email_address,
)
from gmail_readonly.google_client import (
    GoogleGmailReadClient,
    build_google_read_client,
    write_token_atomically,
)

__all__ = [
    "GMAIL_READONLY_SCOPE",
    "GmailLabel",
    "GmailMailbox",
    "GmailProfile",
    "GmailReadClient",
    "GmailReadError",
    "GmailSearch",
    "GmailSender",
    "GmailSource",
    "GoogleGmailReadClient",
    "NormalizedEmail",
    "build_google_read_client",
    "masked_email_address",
    "write_token_atomically",
]
