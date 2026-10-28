"""Response compression through the vendored stream-safe gzip middleware.

Text responses shrink for a gzip client, while the app's one live stream
(``/api/tts/speak``, headerless PCM) must still hand each chunk to the client
the moment the hub produces it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.types import Message

from app.webapp.routers import chats as chats_router
from app.webapp.server import create_app
from src.webapp_config import WebappConfig
from tests.test_tts import FakeClient, FakeStream, _app_with_db, _seed_message

LOOPBACK = ("127.0.0.1", 5555)
_FIRST_CHUNK = b"\x01\x02" * 2048
_SECOND_CHUNK = b"\x03\x04" * 2048


def _client() -> TestClient:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    return TestClient(app, client=LOOPBACK)


@pytest.mark.parametrize("path", ["/", "/static/styles.css", "/static/main.js"])
def test_text_assets_are_gzipped_for_a_gzip_client(path: str) -> None:
    with _client() as client:
        response = client.get(path, headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["vary"]


def test_text_assets_stay_plain_without_accept_encoding() -> None:
    with _client() as client:
        response = client.get("/", headers={"Accept-Encoding": "identity"})
    assert response.status_code == 200
    assert "content-encoding" not in response.headers


class _GatedStream(FakeStream):
    """Hub stream that releases its second chunk only after the client saw the first."""

    def __init__(self, second_chunk_gate: asyncio.Event) -> None:
        super().__init__()
        self._gate = second_chunk_gate

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield _FIRST_CHUNK
        await self._gate.wait()
        yield _SECOND_CHUNK


def test_tts_stream_delivers_first_chunk_before_the_second_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "s.sqlite3"
    message_id = _seed_message(
        db, text="Please bring the form.", sender_label="Teacher", summary="Bring the form."
    )
    app = _app_with_db(db)

    async def drive() -> list[Message]:
        gate = asyncio.Event()
        monkeypatch.setattr(
            chats_router.httpx, "AsyncClient", FakeClient(_GatedStream(gate), {})
        )
        body = json.dumps({"message_id": message_id}).encode()
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/tts/speak",
            "raw_path": b"/api/tts/speak",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"accept-encoding", b"gzip"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": LOOPBACK,
            "server": ("127.0.0.1", 8455),
            "state": {},
        }
        request_sent = False
        never = asyncio.Event()

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await never.wait()
            return {"type": "http.disconnect"}

        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                gate.set()

        # A middleware that held the first chunk back would never release the gate.
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        return sent

    # A worker thread has no running loop; earlier Playwright tests leave one on the main thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        sent = pool.submit(asyncio.run, drive()).result(timeout=10)
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 200
    assert headers["content-type"].startswith("audio/L16")
    assert "content-encoding" not in headers
    chunks = [m["body"] for m in sent if m["type"] == "http.response.body" and m.get("body")]
    assert chunks == [_FIRST_CHUNK, _SECOND_CHUNK]
