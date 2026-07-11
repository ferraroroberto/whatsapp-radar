"""Operator-run Gmail taxonomy discovery through the local LLM hub.

Only configured whitelist searches are used. The command reports aggregate
count/date scope before retrieving bounded message content or making its single
LLM call. Model output is validated and privacy-checked before atomically
replacing the editable Gmail classification assets.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gmail_readonly import (
    GmailLabel,
    GmailMailbox,
    GmailSender,
    GmailSource,
    NormalizedEmail,
)

from src.analysis.keywords import load_keyword_rules, normalize, roots_file_path
from src.config import Config
from src.connector.gmail import build_gmail_read_client

Progress = Callable[[str], None]
_BUCKET_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_EMAIL_ADDRESS = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_URL = re.compile(r"https?://", re.IGNORECASE)
_SYSTEM_PROMPT = """You design a generic deterministic email-triage taxonomy.
Return one JSON object with a "buckets" array. Each bucket has "name",
"description", and "roots". Names are lowercase generic categories; roots are
short high-recall phrases. Never reproduce names, organizations, addresses,
domains, message identifiers, URLs, subjects, or sentences from the sample.
Do not include markdown or prose outside the JSON object."""


@dataclass(frozen=True)
class SurveyScope:
    """Aggregate whitelist scope shown before analysis."""

    whitelist_entries: int
    message_count: int
    earliest: str | None
    latest: str | None


@dataclass(frozen=True)
class SurveyProposal:
    """Validated generic assets ready for atomic persistence."""

    taxonomy_markdown: str
    keyword_rules: str


def run_gmail_survey(
    config: Config,
    *,
    days: int = 60,
    max_messages: int = 100,
    progress: Progress = print,
) -> SurveyScope:
    """Survey configured Gmail sources and replace the editable rule assets."""
    if days < 1:
        raise ValueError("days must be at least 1")
    if max_messages < 1:
        raise ValueError("max_messages must be at least 1")

    mailbox = GmailMailbox(build_gmail_read_client(config.gmail))
    try:
        sources = mailbox.resolve_sources(
            senders=tuple(
                GmailSender(sender.address, sender.name)
                for sender in config.gmail.senders
            ),
            labels=tuple(
                GmailLabel(label.name, label.display_name)
                for label in config.gmail.labels
            ),
            lookback_days=days,
        )
        metadata = [item for source in sources for item in mailbox.metadata(source.search)]
        timestamps = sorted(item.timestamp for item in metadata)
        scope = SurveyScope(
            whitelist_entries=len(sources),
            message_count=len(metadata),
            earliest=timestamps[0] if timestamps else None,
            latest=timestamps[-1] if timestamps else None,
        )
        progress(
            f"Scope: {scope.whitelist_entries} whitelist entr"
            f"{'y' if scope.whitelist_entries == 1 else 'ies'}, "
            f"{scope.message_count} email(s), last {days} days"
        )
        progress(
            f"Date range: {scope.earliest or 'none'} -> {scope.latest or 'none'}"
        )
        if not metadata:
            raise ValueError("no Gmail messages matched the configured whitelist and window")

        samples = _sample_messages(mailbox, sources, max_messages)
        progress(
            f"Analyzing {len(samples)} bounded sample(s) through local-llm-hub"
        )
        raw = _call_hub(config, samples)
        proposal = parse_survey_proposal(
            raw,
            forbidden_fragments=_forbidden_fragments(config, samples),
        )
        write_survey_assets(proposal)
        progress(
            "Updated src/analysis/prompts/gmail_classification_taxonomy.md "
            "and gmail_keyword_roots.txt"
        )
        return scope
    finally:
        mailbox.close()


def _sample_messages(
    mailbox: GmailMailbox,
    sources: tuple[GmailSource, ...],
    max_messages: int,
) -> list[NormalizedEmail]:
    """Retrieve a deterministic, bounded allocation across whitelist entries."""
    per_source = max(1, max_messages // len(sources))
    samples = [
        item
        for source in sources
        for item in mailbox.messages(source.search, limit=per_source)
    ]
    return sorted(samples, key=lambda item: (item.timestamp, item.message_id))[-max_messages:]


def _call_hub(config: Config, samples: list[NormalizedEmail]) -> str:
    """Make exactly one Anthropic-shape call through the configured local hub."""
    from anthropic import Anthropic

    payload = [
        {
            "sample_id": f"email-{index}",
            "sent_at": email.timestamp,
            "subject": email.subject,
            "body": email.body_text,
        }
        for index, email in enumerate(samples, start=1)
    ]
    try:
        client = Anthropic(api_key="local-dummy", base_url=config.hub.base_url)
        response = client.messages.create(
            model=config.hub.model,
            max_tokens=config.hub.max_tokens,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Infer generic actionable email buckets and candidate keyword "
                        "roots from these private local samples:\n"
                        + json.dumps(payload, ensure_ascii=False)
                    )[: config.hub.max_prompt_chars],
                }
            ],
        )
    except Exception as exc:
        raise RuntimeError("local LLM hub survey call failed") from exc
    return "".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", "") == "text"
    )


def parse_survey_proposal(
    raw: str,
    *,
    forbidden_fragments: set[str] | None = None,
) -> SurveyProposal:
    """Validate structured generic output and render the two plain-text assets."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Gmail survey returned no JSON object")
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("Gmail survey returned malformed JSON") from exc
    buckets = payload.get("buckets") if isinstance(payload, dict) else None
    if not isinstance(buckets, list) or not 1 <= len(buckets) <= 12:
        raise ValueError("Gmail survey must return between 1 and 12 buckets")

    rendered: list[tuple[str, str, tuple[str, ...]]] = []
    seen_names: set[str] = set()
    seen_roots: set[str] = set()
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise ValueError("Gmail survey bucket must be an object")
        name = str(bucket.get("name") or "").strip().lower()
        description = str(bucket.get("description") or "").strip()
        roots = bucket.get("roots")
        if not _BUCKET_NAME.fullmatch(name) or name in seen_names:
            raise ValueError("Gmail survey returned an invalid or duplicate bucket name")
        if not description or len(description) > 240 or "\n" in description:
            raise ValueError("Gmail survey returned an invalid bucket description")
        if not isinstance(roots, list) or not 1 <= len(roots) <= 40:
            raise ValueError("Gmail survey bucket must contain 1 to 40 roots")
        clean_roots: list[str] = []
        for value in roots:
            root = normalize(str(value).strip())
            if not root or len(root) > 80 or any(ch in root for ch in "|\r\n"):
                raise ValueError("Gmail survey returned an invalid keyword root")
            if root not in seen_roots:
                clean_roots.append(root)
                seen_roots.add(root)
        if not clean_roots:
            raise ValueError("Gmail survey bucket contains only duplicate roots")
        rendered.append((name, description, tuple(clean_roots)))
        seen_names.add(name)

    combined = json.dumps(rendered, ensure_ascii=False).lower()
    if _EMAIL_ADDRESS.search(combined) or _URL.search(combined):
        raise ValueError("Gmail survey output contains a forbidden address or URL")
    for fragment in forbidden_fragments or set():
        if len(fragment) >= 4 and fragment.lower() in combined:
            raise ValueError("Gmail survey output contains a configured or sampled identifier")

    taxonomy = [
        "# Gmail classification taxonomy",
        "",
        "Generated from a bounded local survey. Review before committing.",
        "",
        *[f"- `{name}`: {description}" for name, description, _ in rendered],
        "",
        "Keep this taxonomy generic. Never add mailbox addresses, personal names, "
        "organizations, or copied email content.",
        "",
    ]
    rules = [
        "# Gmail Stage-1 rules. Format: bucket | normalized keyword root",
        "# Generated from a bounded local survey; never add personal identifiers.",
        "",
        *[
            f"{name} | {root}"
            for name, _, bucket_roots in rendered
            for root in bucket_roots
        ],
        "",
    ]
    return SurveyProposal("\n".join(taxonomy), "\n".join(rules))


def _forbidden_fragments(
    config: Config, samples: list[NormalizedEmail]
) -> set[str]:
    values = {
        *(sender.address for sender in config.gmail.senders),
        *(sender.name for sender in config.gmail.senders),
        *(label.name for label in config.gmail.labels),
        *(label.display_name for label in config.gmail.labels),
        *(email.sender_address or "" for email in samples),
        *(email.sender_name or "" for email in samples),
    }
    fragments = {value.strip().lower() for value in values if value.strip()}
    for address in tuple(fragments):
        _, separator, domain = address.partition("@")
        if separator and domain:
            fragments.add(domain)
    return fragments


def write_survey_assets(proposal: SurveyProposal) -> None:
    """Replace both assets atomically per file, rolling back on partial failure."""
    rules_path = roots_file_path("gmail")
    taxonomy_path = rules_path.with_name("gmail_classification_taxonomy.md")
    originals = {
        rules_path: rules_path.read_text(encoding="utf-8"),
        taxonomy_path: taxonomy_path.read_text(encoding="utf-8"),
    }
    pending = {
        rules_path: proposal.keyword_rules,
        taxonomy_path: proposal.taxonomy_markdown,
    }
    temporary_paths: list[Path] = []
    replaced: list[Path] = []
    try:
        for path, content in pending.items():
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary_paths.append(temporary)
        for path in pending:
            path.with_suffix(path.suffix + ".tmp").replace(path)
            replaced.append(path)
    except Exception:
        for path in replaced:
            path.write_text(originals[path], encoding="utf-8")
        raise
    finally:
        for path in temporary_paths:
            if path.exists():
                path.unlink()
    load_keyword_rules.cache_clear()
