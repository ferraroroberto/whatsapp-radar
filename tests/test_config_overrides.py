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


def test_e2e_local_config_override_never_opens_or_writes_host_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    host_local = root / "config" / "local.json"
    host_local.write_text(
        json.dumps({"family": {"home_address": "real home"}}), encoding="utf-8"
    )
    fixture_local = tmp_path / "e2e-local.json"
    fixture_local.write_text(
        json.dumps({"family": {"home_address": "sanitized fixture"}}), encoding="utf-8"
    )
    monkeypatch.setenv("WR_LOCAL_CONFIG_PATH", str(fixture_local))

    opened: list[Path] = []
    original_load_json = config._load_json

    def recording_load_json(path: Path) -> dict:
        opened.append(path)
        return original_load_json(path)

    monkeypatch.setattr(config, "_load_json", recording_load_json)
    loaded = config.load_config(root=root)
    written = config.save_local_overrides({"family": {"enabled": True}}, root=root)

    assert loaded.family.home_address == "sanitized fixture"
    assert host_local not in opened
    assert json.loads(host_local.read_text(encoding="utf-8")) == {
        "family": {"home_address": "real home"}
    }
    assert written == fixture_local
    assert json.loads(fixture_local.read_text(encoding="utf-8")) == {
        "family": {"enabled": True, "home_address": "sanitized fixture"}
    }


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


def test_tts_profiles_default_when_unconfigured(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "config" / "default.json").write_text("{}", encoding="utf-8")
    tts = config.load_config(root=root).tts
    assert tts.en_female == config.VoiceProfile("orpheus-tts", "tara")
    assert tts.es_male == config.VoiceProfile("kokoro-tts", "em_alex")


def test_tts_profiles_load_from_json_partial_override(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "config" / "default.json").write_text(
        json.dumps(
            {
                "tts": {
                    "profiles": {
                        "es_female": {"model": "kokoro-tts", "voice": "af_bella"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    tts = config.load_config(root=root).tts
    # Overridden profile takes the JSON value...
    assert tts.es_female == config.VoiceProfile("kokoro-tts", "af_bella")
    # ...siblings not mentioned keep their defaults.
    assert tts.en_female == config.VoiceProfile("orpheus-tts", "tara")
    assert tts.es_male == config.VoiceProfile("kokoro-tts", "em_alex")


def test_tts_profiles_local_json_overrides_default(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "config" / "default.json").write_text(
        json.dumps({"tts": {"profiles": {"en_male": {"model": "orpheus-tts", "voice": "leo"}}}}),
        encoding="utf-8",
    )
    config.save_local_overrides(
        {"tts": {"profiles": {"en_male": {"model": "orpheus-tts", "voice": "zac"}}}}, root=root
    )
    tts = config.load_config(root=root).tts
    assert tts.en_male == config.VoiceProfile("orpheus-tts", "zac")
