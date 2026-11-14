"""Password-route lock-out: 429 + Retry-After after wrong passwords, and the guessing alert.

A fake clock drives the wait and a fake notifier stands in for Telegram, so nothing
sleeps and nothing is sent.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp.routers import auth
from app.webapp.server import create_app
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)
REMOTE = ("203.0.113.5", 5555)
PASSWORD = "hunter2"
TOKEN = "thetoken"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeNotifier:
    """Stands in for the Telegram notifier: records each text and wakes the waiting test."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.sent = threading.Event()

    def send_text(self, text: str) -> None:
        self.texts.append(text)
        self.sent.set()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notifier(monkeypatch: pytest.MonkeyPatch) -> FakeNotifier:
    fake = FakeNotifier()
    monkeypatch.setattr(auth, "_alert_notifier", lambda: fake)
    return fake


def _client(peer: tuple[str, int], clock: FakeClock) -> TestClient:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token=TOKEN, auth_password=PASSWORD)
    app.state.login_lockout = auth.build_login_lockout(clock)
    return TestClient(app, client=peer)


def _wrong(c: TestClient, **kw: Any) -> Any:
    return c.post("/api/login", json={"password": "wrong"}, **kw)


def test_wrong_passwords_get_401_then_429_with_retry_after_then_success_after_the_wait(
    clock: FakeClock, notifier: FakeNotifier
) -> None:
    with _client(REMOTE, clock) as c:
        # Five failures are free; the sixth is still a 401 but starts the wait.
        assert [_wrong(c).status_code for _ in range(6)] == [401] * 6
        locked = _wrong(c)
        assert locked.status_code == 429
        wait = int(locked.headers["Retry-After"])
        assert wait >= 1
        # Still locked mid-wait, even with the right password: refused before it is read.
        clock.now += wait - 1
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 429
        clock.now += 1
        ok = c.post("/api/login", json={"password": PASSWORD})
        assert ok.status_code == 200
        assert ok.json() == {"token": TOKEN}


def test_three_wrong_passwords_raise_exactly_one_alert(
    clock: FakeClock, notifier: FakeNotifier
) -> None:
    with _client(REMOTE, clock) as c:
        for _ in range(3):
            _wrong(c)
        assert notifier.sent.wait(timeout=5)
        # The same streak keeps failing: no second alert.
        for _ in range(2):
            clock.now += 1000
            _wrong(c)
    assert len(notifier.texts) == 1
    text = notifier.texts[0]
    assert "Family Radar" in text
    assert REMOTE[0] in text
    assert "3 wrong passwords" in text
    assert PASSWORD not in text and TOKEN not in text


def test_the_pc_itself_is_never_locked_out(clock: FakeClock, notifier: FakeNotifier) -> None:
    with _client(LOOPBACK, clock) as c:
        assert [_wrong(c).status_code for _ in range(8)] == [401] * 8
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    assert notifier.texts == []


def test_a_tunnelled_request_from_a_loopback_peer_is_still_locked_out(
    clock: FakeClock, notifier: FakeNotifier
) -> None:
    edge = {"cf-ray": "abc", "cf-connecting-ip": "1.2.3.4"}
    with _client(LOOPBACK, clock) as c:
        assert [_wrong(c, headers=edge).status_code for _ in range(6)] == [401] * 6
        assert _wrong(c, headers=edge).status_code == 429


class _AlertOutcome(logging.Handler):
    """Sets ``seen`` when the alert thread logs its outcome."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.seen = threading.Event()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())
        self.seen.set()


def test_no_alert_channel_is_reported_unconfigured(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "_alert_notifier", lambda: None)
    outcome = _AlertOutcome()
    alert_log = logging.getLogger("src.login_alert")
    alert_log.addHandler(outcome)
    try:
        with _client(REMOTE, clock) as c:
            for _ in range(3):
                _wrong(c)
        assert outcome.seen.wait(5)
    finally:
        alert_log.removeHandler(outcome)
    assert "unconfigured" in outcome.messages[0] and "3 wrong passwords" in outcome.messages[0]
