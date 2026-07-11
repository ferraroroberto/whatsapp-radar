"""save_local_overrides deep-merges into the gitignored config/local.json.

This is the host-override writer the Config tab's safe settings persist through.
It must merge (never clobber sibling keys) and survive an absent file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import config


def _make_root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    return tmp_path


def test_creates_file_when_absent(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    config.save_local_overrides({"connector": "linked_device"}, root=root)
    written = json.loads((root / "config" / "local.json").read_text(encoding="utf-8"))
    assert written == {"connector": "linked_device"}


def test_deep_merges_without_clobbering(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    local = root / "config" / "local.json"
    local.write_text(
        json.dumps({"connector": "fixture", "hub": {"base_url": "http://x", "model": "old"}}),
        encoding="utf-8",
    )
    config.save_local_overrides({"classifier": "cascade", "hub": {"model": "new"}}, root=root)
    written = json.loads(local.read_text(encoding="utf-8"))
    assert written == {
        "connector": "fixture",          # untouched
        "classifier": "cascade",         # added
        "hub": {"base_url": "http://x", "model": "new"},  # nested-merged
    }


def test_load_config_reads_local_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # load_config layers WR_* env over the file values; a leaked WR_CONNECTOR from
    # the developer's real .env (read into os.environ by an earlier load_config)
    # would otherwise mask the default — isolate it so the merge is what's tested.
    monkeypatch.delenv("WR_CONNECTOR", raising=False)
    monkeypatch.delenv("WR_CLASSIFIER", raising=False)
    root = _make_root(tmp_path)
    (root / "config" / "default.json").write_text(
        json.dumps({"connector": "fixture", "classifier": "stub"}), encoding="utf-8"
    )
    config.save_local_overrides({"classifier": "cascade"}, root=root)
    cfg = config.load_config(root=root)
    assert cfg.classifier == "cascade"
    assert cfg.connector == "fixture"


def test_sources_load_from_json_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    (root / "config" / "default.json").write_text(
        json.dumps({"connector": "fixture", "sources": ["whatsapp"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("WR_SOURCES", " gmail, whatsapp, gmail ")
    cfg = config.load_config(root=root)
    assert cfg.sources == ("gmail", "whatsapp")


def test_empty_sources_fall_back_to_whatsapp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    (root / "config" / "default.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("WR_SOURCES", " , ")
    assert config.load_config(root=root).sources == ("whatsapp",)


def test_loads_named_gmail_whitelist_and_resolves_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WR_GMAIL_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("WR_GMAIL_TOKEN_PATH", raising=False)
    root = _make_root(tmp_path)
    (root / "config" / "default.json").write_text(
        json.dumps(
            {
                "gmail": {
                    "credentials_path": "auth/gmail/client.json",
                    "token_path": "auth/gmail/token.json",
                    "senders": [
                        {"address": "SCHOOL@EXAMPLE.COM", "name": "School"}
                    ],
                    "labels": [
                        {"name": "Family/Activities", "display_name": "Activities"}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = config.load_config(root=root).gmail

    assert loaded.credentials_path == root / "auth/gmail/client.json"
    assert loaded.token_path == root / "auth/gmail/token.json"
    assert loaded.senders[0] == config.GmailSender("school@example.com", "School")
    assert loaded.labels[0] == config.GmailLabel("Family/Activities", "Activities")
