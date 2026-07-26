"""Read-only historical backtest of Gmail school-sender classification (Step 2/5 of #206).

Fetches an arbitrary past date window of whitelisted school sender/label email and
runs each message through the live Step-1-extended classification contract
(``child``/``task_category``/``prep_complexity``, #215) so prompt/contract changes
can be validated against a full year of real historical school email *now*,
without waiting for new mail. Precedent: ``scripts/gmail_survey.py`` (bounded
sample survey) and ``scripts/traffic_smoke.py`` (standalone live smoke check).

    .\\.venv\\Scripts\\python.exe -m scripts.gmail_school_backtest \\
        --after 2025-09-01 --before 2025-10-01

Hits the REAL Gmail API and REAL ``config/local.json`` — but is strictly
read-only: it never opens the local ``chats``/``messages`` database, never
writes ``analysis_items``/``analysis_trace``, and never advances a cursor.
It also makes one real local-llm-hub call per email in range, so keep
``--max-messages`` bounded for a first pass. Never commit ``--output`` results —
they contain real historical email content; point it at the gitignored
``data/`` directory if you want to keep a copy.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from gmail_readonly import GmailLabel, GmailMailbox, GmailSearch, GmailSender, NormalizedEmail

from src.analysis.classifier import HubClassifier, TracedClassifier
from src.analysis.contract import ContractError, parse_analysis
from src.config import Config, load_config
from src.connector.gmail import build_gmail_read_client
from src.models import StoredMessage

Progress = Callable[[str], None]


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def resolve_whitelist(
    config: Config, *, sender_overrides: list[str], label_overrides: list[str]
) -> tuple[tuple[GmailSender, ...], tuple[GmailLabel, ...]]:
    """Resolve the sender/label whitelist from CLI overrides or the configured one."""
    if sender_overrides or label_overrides:
        senders = tuple(
            GmailSender(address=address, display_name=address) for address in sender_overrides
        )
        labels = tuple(GmailLabel(name=name, display_name=name) for name in label_overrides)
    else:
        senders = tuple(
            GmailSender(address=sender.address, display_name=sender.name)
            for sender in config.gmail.senders
        )
        labels = tuple(
            GmailLabel(name=label.name, display_name=label.display_name)
            for label in config.gmail.labels
        )
    if not senders and not labels:
        raise ValueError(
            "no sender/label whitelist configured; pass --sender/--label or set "
            "gmail.senders/gmail.labels in config/local.json"
        )
    return senders, labels


def _dated_search(search: GmailSearch, *, after: date, before: date) -> GmailSearch:
    """Layer explicit ``after:``/``before:`` operators onto a resolved whitelist search."""
    dated_query = " ".join(
        part
        for part in (search.query, f"after:{after:%Y/%m/%d}", f"before:{before:%Y/%m/%d}")
        if part
    )
    return dataclasses.replace(search, query=dated_query, lookback_days=None)


def _stored_message(email: NormalizedEmail, index: int) -> StoredMessage:
    """A throwaway :class:`StoredMessage` — never persisted, id/chat_id are placeholders."""
    return StoredMessage(
        id=index,
        chat_id=0,
        source_message_id=email.message_id,
        message_timestamp=email.timestamp,
        text=email.text,
        sender_label=email.sender_name or email.sender_address,
        message_type="email",
    )


def run_backtest(
    mailbox: GmailMailbox,
    classifier: TracedClassifier,
    *,
    after: date,
    before: date,
    senders: tuple[GmailSender, ...],
    labels: tuple[GmailLabel, ...],
    max_messages: int = 300,
    progress: Progress = print,
) -> list[dict[str, Any]]:
    """Fetch a historical Gmail window and classify each email.

    Strictly read-only: ``mailbox`` and ``classifier`` are both injected, so this
    function has no import path to ``src.db.store``/``sqlite3`` and no way to
    write local state or advance a cursor.
    """
    if after >= before:
        raise ValueError("--after must be earlier than --before")

    sources = mailbox.resolve_sources(senders=senders, labels=labels)
    display_names = {source.source_id: source.display_name for source in sources}

    fetched: list[tuple[str, NormalizedEmail]] = []
    for source in sources:
        dated = _dated_search(source.search, after=after, before=before)
        fetched.extend((source.source_id, email) for email in mailbox.messages(dated))
    fetched.sort(key=lambda item: item[1].timestamp)

    if len(fetched) > max_messages:
        progress(
            f"⚠️ {len(fetched) - max_messages} email(s) beyond --max-messages "
            f"{max_messages} dropped (kept the earliest)"
        )
        fetched = fetched[:max_messages]

    progress(f"Scope: {len(fetched)} email(s) between {after} and {before}")

    records: list[dict[str, Any]] = []
    for index, (source_id, email) in enumerate(fetched, start=1):
        display_name = display_names.get(source_id, source_id)
        sender = email.sender_address or email.sender_name or "unknown"
        outcome = classifier.classify_traced(
            display_name, [_stored_message(email, index)], None, source="gmail"
        )
        try:
            result = parse_analysis(outcome.raw_output)
        except ContractError as exc:
            reason = "llm_truncated" if outcome.stop_reason == "max_tokens" else str(exc)
            progress(f"⚠️ {email.timestamp} {sender} — classification failed: {reason}")
            continue

        record = {
            "message_id": email.message_id,
            "timestamp": email.timestamp,
            "sender": sender,
            "action_required": result.action_required,
            "priority": result.priority,
            "child": result.child,
            "task_category": result.task_category,
            "prep_complexity": result.prep_complexity,
            "confidence": result.confidence,
            "summary": result.summary,
            "deadline": result.deadline,
        }
        records.append(record)
        progress(
            f"{email.timestamp[:10]} {sender} — child={result.child} "
            f"task={result.task_category} prep={result.prep_complexity} "
            f"action={result.action_required} conf={result.confidence} — "
            f"{result.summary!r}"
        )
    return records


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description=(
            "Read-only historical backtest of Gmail school-sender classification (#216). "
            "Hits the REAL Gmail API and REAL config/local.json — never writes to the "
            "local chats/messages database, never advances a cursor, and makes one real "
            "local-llm-hub call per email in range."
        )
    )
    parser.add_argument("--after", required=True, type=_parse_date, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--before", required=True, type=_parse_date, help="YYYY-MM-DD, exclusive")
    parser.add_argument(
        "--sender",
        action="append",
        default=[],
        help="override the configured sender whitelist (repeatable)",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="override the configured label whitelist (repeatable)",
    )
    parser.add_argument("--max-messages", type=int, default=300)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "append one JSON line per email here; never committed — point it at "
            "the gitignored data/ directory"
        ),
    )
    args = parser.parse_args(argv)

    config = load_config()
    try:
        senders, labels = resolve_whitelist(
            config, sender_overrides=args.sender, label_overrides=args.label
        )
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1

    mailbox = GmailMailbox(build_gmail_read_client(config.gmail))
    try:
        records = run_backtest(
            mailbox,
            HubClassifier(config.hub, config.children),
            after=args.after,
            before=args.before,
            senders=senders,
            labels=labels,
            max_messages=args.max_messages,
        )
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    finally:
        mailbox.close()

    if args.output is not None:
        with args.output.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"Wrote {len(records)} record(s) to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
