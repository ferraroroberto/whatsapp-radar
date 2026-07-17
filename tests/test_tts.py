"""Offline client and API regression coverage for summary TTS (#94)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp.routers import chats as chats_router
from app.webapp.server import create_app
from src import _loopback_http, tts_client
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)


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


def _app() -> Any:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.hub_base_url = "http://127.0.0.1:8000"
    return app


def test_tts_health_degrades_to_unavailable() -> None:
    app = _app()

    def down(_base: str) -> bool:
        raise tts_client.TtsError("hub unreachable", status=503)

    app.state.tts_health = down
    with TestClient(app, client=LOOPBACK) as client:
        assert client.get("/api/tts/health").json() == {"available": False}


def test_tts_speak_streams_pcm_and_current_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeStream:
        status_code = 200
        headers = {"x-sample-rate": "24000"}

        async def __aenter__(self) -> FakeStream:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def aiter_bytes(self) -> AsyncIterator[bytes]:
            yield b"\xc2\xff\xc0\xff"

        async def aread(self) -> bytes:
            return b""

    class FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def stream(self, method: str, url: str, **kwargs: Any) -> FakeStream:
            captured.update(method=method, url=url, json=kwargs.get("json"))
            return FakeStream()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(chats_router.httpx, "AsyncClient", FakeClient)
    with TestClient(_app(), client=LOOPBACK) as client:
        with client.stream("POST", "/api/tts/speak", json={"text": "Read it."}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("audio/L16")
            assert response.headers["x-sample-rate"] == "24000"
            assert b"".join(response.iter_bytes()) == b"\xc2\xff\xc0\xff"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:8000/v1/audio/speech"
    assert captured["json"]["model"] == "orpheus-tts"
    assert captured["json"]["input"] == "Read it."
    assert captured["json"]["response_format"] == "pcm"


def test_tts_speak_rejects_empty_text() -> None:
    with TestClient(_app(), client=LOOPBACK) as client:
        assert client.post("/api/tts/speak", json={"text": "   "}).status_code == 400
