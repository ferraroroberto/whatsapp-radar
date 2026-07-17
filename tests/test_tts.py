"""Offline client and API regression coverage for summary TTS (#94, #157)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp.routers import chats as chats_router
from app.webapp.server import create_app
from src import _loopback_http, tts_client
from src.config import TtsConfig, VoiceProfile
from src.db import store
from src.models import ChatRecord, MessageRecord
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)

_EN_LONG = "Reminder for tomorrow: " + "please bring the signed form and 12 euros. " * 8
_ES_LONG = (
    "Recordatorio para mañana: por favor trae el formulario firmado y "
    "el dinero para la excursión escolar antes de las nueve de la mañana. " * 4
)


def test_health_and_payload_match_current_hub_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> dict[str, str]:
        seen.update(method=method, url=url, kwargs=kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(_loopback_http, "request", fake_request)
    assert tts_client.health("http://127.0.0.1:8000/") is True
    assert seen["url"] == "http://127.0.0.1:8000/health"

    payload = tts_client.build_speech_payload("Read this summary.")
    assert payload == {
        "model": "orpheus-tts",
        "input": "Read this summary.",
        "voice": "tara",
        "response_format": "pcm",
        "stream_format": "audio",
    }
    assert tts_client.speech_url("http://127.0.0.1:8000/").endswith(
        "/v1/audio/speech"
    )


def test_unknown_voice_falls_back_and_speed_is_forwarded() -> None:
    payload = tts_client.build_speech_payload("hello", voice="unknown", speed=1.2)
    assert payload["voice"] == "tara"
    assert payload["speed"] == 1.2


def test_kokoro_voice_is_preserved_not_overridden_by_orpheus_default() -> None:
    """A valid Spanish (kokoro-tts) voice must survive — not fall back to 'tara'."""
    payload = tts_client.build_speech_payload("hola", voice="ef_dora", model="kokoro-tts")
    assert payload == {
        "model": "kokoro-tts",
        "input": "hola",
        "voice": "ef_dora",
        "response_format": "pcm",
        "stream_format": "audio",
    }


def test_unknown_kokoro_voice_falls_back_within_kokoro_not_to_orpheus() -> None:
    payload = tts_client.build_speech_payload("hola", voice="bogus", model="kokoro-tts")
    assert payload["model"] == "kokoro-tts"
    assert payload["voice"] == "ef_dora"  # a kokoro voice, never "tara"


def _tts_profiles() -> TtsConfig:
    return TtsConfig(
        en_female=VoiceProfile("orpheus-tts", "tara"),
        en_male=VoiceProfile("orpheus-tts", "leo"),
        es_female=VoiceProfile("kokoro-tts", "ef_dora"),
        es_male=VoiceProfile("kokoro-tts", "em_alex"),
    )


def _app(
    *,
    sender_voice_genders: dict[str, str] | None = None,
    default_voice_gender: str = "female",
) -> Any:
    app = create_app()
    app.state.webapp_config = WebappConfig(
        auth_token="",
        sender_voice_genders=sender_voice_genders or {},
        default_voice_gender=default_voice_gender,
    )
    app.state.hub_base_url = "http://127.0.0.1:8000"
    app.state.tts_profiles = _tts_profiles()
    return app


def test_tts_health_degrades_to_unavailable() -> None:
    app = _app()

    def down(_base: str) -> bool:
        raise tts_client.TtsError("hub unreachable", status=503)

    app.state.tts_health = down
    with TestClient(app, client=LOOPBACK) as client:
        assert client.get("/api/tts/health").json() == {"available": False}


class FakeStream:
    status_code = 200
    headers = {"x-sample-rate": "24000"}

    def __init__(self, status_code: int = 200, body: bytes = b"") -> None:
        self.status_code = status_code
        self._body = body

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield b"\xc2\xff\xc0\xff"

    async def aread(self) -> bytes:
        return self._body


class FakeClient:
    def __init__(self, response: FakeStream, captured: dict[str, Any]) -> None:
        self._response = response
        self._captured = captured

    def __call__(self, *_args: Any, **_kwargs: Any) -> FakeClient:
        return self

    def stream(self, method: str, url: str, **kwargs: Any) -> FakeStream:
        self._captured.update(method=method, url=url, json=kwargs.get("json"))
        return self._response

    async def aclose(self) -> None:
        pass


def _seed_message(
    db: Path, *, text: str, sender_label: str, summary: str | None
) -> int:
    conn = store.connect(db)
    try:
        chat = store.upsert_chat(
            conn, ChatRecord(source_chat_id="c", display_name="Class 4A Group")
        )
        store.insert_message(
            conn, chat,
            MessageRecord(source_message_id="m1", message_timestamp="2026-06-10T10:00:00+00:00",
                          text=text, sender_label=sender_label),
        )
        message_id = int(
            conn.execute(
                "SELECT id FROM messages WHERE source_message_id = 'm1'"
            ).fetchone()["id"]
        )
        if summary is not None:
            store.set_message_summary(conn, message_id, summary)
        return message_id
    finally:
        conn.close()


def _app_with_db(db: Path, **kwargs: Any) -> Any:
    app = _app(**kwargs)
    app.state.db_path = db
    return app


def test_tts_speak_resolves_english_default_female_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "s.sqlite3"
    message_id = _seed_message(
        db, text=_EN_LONG, sender_label="Teacher", summary="Bring the form tomorrow."
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        chats_router.httpx, "AsyncClient", FakeClient(FakeStream(), captured)
    )
    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        with client.stream(
            "POST", "/api/tts/speak", json={"message_id": message_id}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("audio/L16")
    assert captured["json"]["model"] == "orpheus-tts"
    assert captured["json"]["voice"] == "tara"
    assert captured["json"]["input"] == "Bring the form tomorrow."


def test_tts_speak_resolves_spanish_male_profile_from_sender_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gender comes only from the explicit mapping — never guessed from the name."""
    db = tmp_path / "s.sqlite3"
    message_id = _seed_message(
        db, text=_ES_LONG, sender_label="Maria", summary="Trae el dinero mañana."
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        chats_router.httpx, "AsyncClient", FakeClient(FakeStream(), captured)
    )
    app = _app_with_db(db, sender_voice_genders={"maria": "male"})
    with TestClient(app, client=LOOPBACK) as client:
        with client.stream("POST", "/api/tts/speak", json={"message_id": message_id}):
            pass
    assert captured["json"]["model"] == "kokoro-tts"
    assert captured["json"]["voice"] == "em_alex"
    assert captured["json"]["input"] == "Trae el dinero mañana."


def test_tts_speak_resolves_english_male_profile_from_sender_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "s.sqlite3"
    message_id = _seed_message(
        db, text=_EN_LONG, sender_label="Dad", summary="Bring the form tomorrow."
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        chats_router.httpx, "AsyncClient", FakeClient(FakeStream(), captured)
    )
    app = _app_with_db(db, sender_voice_genders={"dad": "male"})
    with TestClient(app, client=LOOPBACK) as client:
        with client.stream("POST", "/api/tts/speak", json={"message_id": message_id}):
            pass
    assert captured["json"]["model"] == "orpheus-tts"
    assert captured["json"]["voice"] == "leo"


def test_tts_speak_resolves_spanish_female_profile_from_sender_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "s.sqlite3"
    message_id = _seed_message(
        db, text=_ES_LONG, sender_label="Mom", summary="Trae el dinero mañana."
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        chats_router.httpx, "AsyncClient", FakeClient(FakeStream(), captured)
    )
    app = _app_with_db(db, sender_voice_genders={"mom": "female"}, default_voice_gender="male")
    with TestClient(app, client=LOOPBACK) as client:
        with client.stream("POST", "/api/tts/speak", json={"message_id": message_id}):
            pass
    assert captured["json"]["model"] == "kokoro-tts"
    assert captured["json"]["voice"] == "ef_dora"


def test_tts_speak_unmapped_sender_uses_configured_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "s.sqlite3"
    message_id = _seed_message(
        db, text=_ES_LONG, sender_label="Unknown Number", summary="Trae el dinero mañana."
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        chats_router.httpx, "AsyncClient", FakeClient(FakeStream(), captured)
    )
    app = _app_with_db(db, sender_voice_genders={}, default_voice_gender="male")
    with TestClient(app, client=LOOPBACK) as client:
        with client.stream("POST", "/api/tts/speak", json={"message_id": message_id}):
            pass
    assert captured["json"]["model"] == "kokoro-tts"
    assert captured["json"]["voice"] == "em_alex"


def test_tts_speak_rejects_message_with_no_summary_yet(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    message_id = _seed_message(db, text=_EN_LONG, sender_label="Teacher", summary=None)
    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        r = client.post("/api/tts/speak", json={"message_id": message_id})
        assert r.status_code == 400


def test_tts_speak_404_for_missing_message(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        assert client.post("/api/tts/speak", json={"message_id": 99999}).status_code == 404


def test_tts_speak_distinguishes_voice_backend_unavailable_from_hub_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "s.sqlite3"
    message_id = _seed_message(
        db, text=_EN_LONG, sender_label="Teacher", summary="Bring the form tomorrow."
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        chats_router.httpx,
        "AsyncClient",
        FakeClient(FakeStream(status_code=503, body=b"TTS engine still loading"), captured),
    )
    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        r = client.post("/api/tts/speak", json={"message_id": message_id})
        assert r.status_code == 503
        assert "loading" in r.json()["detail"]


def test_tts_speak_hub_unreachable_surfaces_as_502(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import httpx

    db = tmp_path / "s.sqlite3"
    message_id = _seed_message(
        db, text=_EN_LONG, sender_label="Teacher", summary="Bring the form tomorrow."
    )

    class BrokenStream:
        async def __aenter__(self) -> BrokenStream:
            raise httpx.ConnectError("connection refused")

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

    class BrokenClient:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def stream(self, *_a: Any, **_k: Any) -> BrokenStream:
            return BrokenStream()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(chats_router.httpx, "AsyncClient", BrokenClient)
    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        r = client.post("/api/tts/speak", json={"message_id": message_id})
        assert r.status_code == 502
