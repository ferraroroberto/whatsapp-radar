"""Config tab (#10): inspect the classifier, edit the safe settings subset.

Read-only views of the LLM system prompt and the keyword roots — both are
edited in their source files by design, never from the app. Editable: the safe
runtime knobs (connector, classifier, notifier, hub model) which persist to the
gitignored ``config/local.json`` host-override layer, plus the Telegram delivery
secrets which persist to ``config/webapp_config.json``.

The Telegram bot token is never returned in clear — only whether it is set and a
last-4 hint — and a blank token field on save never overwrites a stored one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import src.analysis.keywords as keywords
from src.analysis.prompts import load_prompt
from src.config import load_config, save_local_overrides
from src.webapp_config import load_webapp_config, update_webapp_config

router = APIRouter()

_VALID_CONNECTORS = {"fixture", "linked_device"}
_VALID_SOURCES = {"whatsapp", "gmail"}
_VALID_CLASSIFIERS = {"stub", "hub", "cascade"}
_VALID_NOTIFIERS = {"none", "telegram"}

_FREQUENCY_NOTE = "Scan frequency is configured in App Launcher's Jobs tab, not here."


class ConfigUpdate(BaseModel):
    sources: list[str] | None = None
    connector: str | None = None
    classifier: str | None = None
    notifier: str | None = None
    hub_base_url: str | None = None
    hub_model: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


def _read_roots_text() -> str:
    """Verbatim contents of the keyword-roots file (shown read-only)."""
    return keywords.roots_file_path().read_text(encoding="utf-8")


def _classification_assets() -> dict[str, Any]:
    """Every editable classification asset, labelled by source and stage."""
    return {
        "shared_system_prompt": {
            "label": "Shared Stage 2 system prompt",
            "file": "src/analysis/prompts/classification_system.md",
            "content": load_prompt("classification_system"),
        },
        "whatsapp": {
            "stage1_rules": {
                "label": "WhatsApp Stage 1 keyword roots",
                "file": "src/analysis/prompts/keyword_roots.txt",
                "content": keywords.roots_file_path("whatsapp").read_text(encoding="utf-8"),
            },
            "stage2_note": (
                "Uses the shared system prompt with Source: WhatsApp in the "
                "rendered user prompt."
            ),
        },
        "gmail": {
            "stage1_rules": {
                "label": "Gmail Stage 1 bucket and keyword rules",
                "file": "src/analysis/prompts/gmail_keyword_roots.txt",
                "content": keywords.roots_file_path("gmail").read_text(encoding="utf-8"),
            },
            "taxonomy": {
                "label": "Gmail survey taxonomy (reference; not sent to Stage 2)",
                "file": "src/analysis/prompts/gmail_classification_taxonomy.md",
                "content": load_prompt("gmail_classification_taxonomy"),
            },
            "stage2_note": (
                "Uses the shared system prompt with Source: Gmail in the rendered user prompt."
            ),
        },
    }


def _mask(token: str) -> dict[str, Any]:
    return {"configured": bool(token), "hint": ("…" + token[-4:]) if len(token) >= 4 else ""}


@router.get("/api/config")
async def get_config() -> dict[str, Any]:
    cfg = load_config()
    wcfg = load_webapp_config()
    return {
        "prompt": load_prompt("classification_system"),
        "keyword_roots": _read_roots_text(),
        "classification_assets": _classification_assets(),
        "gmail": {
            "enabled": "gmail" in cfg.sources,
            "senders": [
                {"address": sender.address, "name": sender.name}
                for sender in cfg.gmail.senders
            ],
            "labels": [
                {"name": label.name, "display_name": label.display_name}
                for label in cfg.gmail.labels
            ],
            "history_scope": (
                "All Gmail messages matching the whitelist; no lookback limit is "
                "applied during sync."
            ),
        },
        "settings": {
            "connector": cfg.connector,
            "sources": list(cfg.sources),
            "classifier": cfg.classifier,
            "notifier": cfg.notifier,
            "hub": {"base_url": cfg.hub.base_url, "model": cfg.hub.model},
        },
        "telegram": {
            "token": _mask(wcfg.telegram_bot_token),
            "chat_id": wcfg.telegram_chat_id,
        },
        "options": {
            "connector": sorted(_VALID_CONNECTORS),
            "sources": sorted(_VALID_SOURCES),
            "classifier": sorted(_VALID_CLASSIFIERS),
            "notifier": sorted(_VALID_NOTIFIERS),
        },
        "note": _FREQUENCY_NOTE,
    }


@router.post("/api/config")
async def update_config(payload: ConfigUpdate) -> dict[str, Any]:
    # Validate the enum-like fields before touching disk.
    if payload.connector is not None and payload.connector not in _VALID_CONNECTORS:
        raise HTTPException(status_code=400, detail=f"invalid connector {payload.connector!r}")
    normalized_sources: list[str] | None = None
    if payload.sources is not None:
        normalized_sources = list(
            dict.fromkeys(source.strip().lower() for source in payload.sources)
        )
        if not normalized_sources or any(
            source not in _VALID_SOURCES for source in normalized_sources
        ):
            raise HTTPException(
                status_code=400,
                detail="sources must contain whatsapp or gmail",
            )
    if payload.classifier is not None and payload.classifier not in _VALID_CLASSIFIERS:
        raise HTTPException(status_code=400, detail=f"invalid classifier {payload.classifier!r}")
    if payload.notifier is not None and payload.notifier not in _VALID_NOTIFIERS:
        raise HTTPException(status_code=400, detail=f"invalid notifier {payload.notifier!r}")

    # Safe runtime knobs → config/local.json (gitignored per-host override).
    overrides: dict[str, Any] = {}
    if normalized_sources is not None:
        overrides["sources"] = normalized_sources
    if payload.connector is not None:
        overrides["connector"] = payload.connector
    if payload.classifier is not None:
        overrides["classifier"] = payload.classifier
    if payload.notifier is not None:
        overrides["notifier"] = payload.notifier
    hub: dict[str, Any] = {}
    if payload.hub_base_url is not None:
        hub["base_url"] = payload.hub_base_url
    if payload.hub_model is not None:
        hub["model"] = payload.hub_model
    if hub:
        overrides["hub"] = hub
    if overrides:
        save_local_overrides(overrides)

    # Telegram secrets → config/webapp_config.json. A blank token never
    # overwrites a stored one (masked-secret pattern); chat_id may be cleared.
    tg_fields: dict[str, Any] = {}
    if payload.telegram_bot_token:
        tg_fields["telegram_bot_token"] = payload.telegram_bot_token
    if payload.telegram_chat_id is not None:
        tg_fields["telegram_chat_id"] = payload.telegram_chat_id
    if tg_fields:
        update_webapp_config(**tg_fields)

    return {"ok": True}
