"""Blocking route work runs on its named pool, and every /api answer says where its time went.

The run poll's folder read and the hub-bound taps each run on their own
``TimedExecutor``, never on the event loop's shared default pool, so a slow hub
call cannot hold the threads the 1.5 s run poll needs. The request-timing
middleware names the pool in the ``Server-Timing`` header, which is what these
tests read alongside the worker thread's own name. Fully offline: the runs folder
and the hub probe are stubbed.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp import runs
from app.webapp.server import create_app
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)


def _client(app: Any) -> TestClient:
    app.state.webapp_config = WebappConfig(auth_token="")
    return TestClient(app, client=LOOPBACK)


def test_the_run_poll_reads_the_runs_folder_on_the_hot_poll_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads: list[str] = []

    def fake_list_runs(limit: int = 50) -> list[dict[str, Any]]:
        threads.append(threading.current_thread().name)
        return []

    monkeypatch.setattr(runs, "list_runs", fake_list_runs)
    monkeypatch.setattr(runs, "active_run", lambda: None)
    with _client(create_app()) as client:
        res = client.get("/api/execution/runs")
    assert res.status_code == 200
    assert threads and threads[0].startswith("hot-poll")
    assert 'desc="hot-poll"' in res.headers["server-timing"]


def test_a_hub_probe_runs_on_the_on_demand_pool() -> None:
    threads: list[str] = []

    def fake_probe(_base: str) -> bool:
        threads.append(threading.current_thread().name)
        return True

    app = create_app()
    app.state.tts_health = fake_probe
    with _client(app) as client:
        res = client.get("/api/tts/health")
    assert res.json() == {"available": True}
    assert threads and threads[0].startswith("on-demand")
    assert 'desc="on-demand"' in res.headers["server-timing"]


def test_an_api_answer_with_no_thread_work_still_carries_server_timing() -> None:
    with _client(create_app()) as client:
        res = client.get("/api/version")
    assert res.status_code == 200
    assert "app;dur=" in res.headers["server-timing"]
