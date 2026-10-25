"""`scripts/run_named_tunnel.py` drives the tray's `WebappManager`, not a parallel spawn (#349)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from src.paths import PROJECT_ROOT


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_named_tunnel_under_test", PROJECT_ROOT / "scripts" / "run_named_tunnel.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCloudflared:
    stdout = None

    def wait(self) -> int:
        return 0


def test_main_starts_the_webapp_through_the_manager_then_the_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    config = tmp_path / "cloudflared.yml"
    config.write_text("tunnel: x\n", encoding="utf-8")
    monkeypatch.setenv("CLOUDFLARED_CONFIG", str(config))
    monkeypatch.setenv("WR_WEBAPP_PORT", "9123")

    events: list[str] = []

    class _FakeManager:
        def __init__(self, cfg: Any) -> None:
            events.append(f"config port={cfg.port}")

        def start(self, wait: bool = True) -> Any:
            events.append("manager.start")
            return type("S", (), {"running": True, "detail": "ok"})()

        def stop(self) -> None:
            events.append("manager.stop")

    monkeypatch.setattr(script, "WebappManager", _FakeManager)
    def _spawn(path: Path) -> _FakeCloudflared:
        events.append("cloudflared")
        return _FakeCloudflared()

    def _stop(proc: object, name: str) -> None:
        events.append(f"stop {name}")

    monkeypatch.setattr(script, "_spawn_cloudflared", _spawn)
    monkeypatch.setattr(script.cloudflared_proc, "stop_proc", _stop)
    monkeypatch.setattr(script.cloudflared_proc, "cleanup_url_file", lambda: None)
    monkeypatch.setattr(script.cloudflared_proc, "persist_tunnel_url", lambda *a: None)

    assert script.main() == 0
    assert events == [
        "config port=9123",
        "manager.start",
        "cloudflared",
        "stop cloudflared",
        "manager.stop",
    ]


def test_main_fails_without_a_tunnel_when_the_webapp_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    config = tmp_path / "cloudflared.yml"
    config.write_text("tunnel: x\n", encoding="utf-8")
    monkeypatch.setenv("CLOUDFLARED_CONFIG", str(config))

    class _DownManager:
        def __init__(self, cfg: Any) -> None:
            pass

        def start(self, wait: bool = True) -> Any:
            raise RuntimeError("uvicorn exited before becoming ready")

    def _no_tunnel(path: Path) -> Any:
        raise AssertionError("cloudflared must not start when the webapp is down")

    monkeypatch.setattr(script, "WebappManager", _DownManager)
    monkeypatch.setattr(script, "_spawn_cloudflared", _no_tunnel)

    assert script.main() == 1
