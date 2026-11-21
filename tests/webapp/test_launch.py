"""The webapp child launches through ``app.webapp.serve``: HTTP/2 first, uvicorn as fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.webapp.manager import WebappManager, WebappManagerConfig, webapp_manager_config
from src.webapp_config import load_webapp_config, save_webapp_config


def test_command_launches_the_serve_module_with_the_configured_server() -> None:
    cmd = WebappManager(WebappManagerConfig(port=18455))._build_command()
    assert cmd[cmd.index("-m") + 1] == "app.webapp.serve"
    assert cmd[cmd.index("--server") + 1] == "hypercorn"
    fallback = WebappManager(WebappManagerConfig(port=18455, server="uvicorn"))._build_command()
    assert fallback[fallback.index("--server") + 1] == "uvicorn"


def test_config_server_defaults_round_trips_and_rejects_unknown_values(tmp_path: Path) -> None:
    path = tmp_path / "webapp_config.json"
    assert load_webapp_config(path).server == "hypercorn"
    cfg = load_webapp_config(path)
    save_webapp_config(type(cfg)(**{**cfg.__dict__, "server": "uvicorn"}), path)
    assert load_webapp_config(path).server == "uvicorn"
    path.write_text('{"server": "gunicorn"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_webapp_config(path)


def test_manager_config_reads_webapp_config_and_the_port_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "webapp_config.json"
    path.write_text('{"host": "127.0.0.1", "port": 9001, "server": "uvicorn"}', encoding="utf-8")
    monkeypatch.setattr("src.webapp_config.DEFAULT_CONFIG_PATH", path)
    monkeypatch.delenv("WR_WEBAPP_PORT", raising=False)
    cfg = webapp_manager_config()
    assert (cfg.host, cfg.port, cfg.server, cfg.enabled) == ("127.0.0.1", 9001, "uvicorn", True)
    monkeypatch.setenv("WR_WEBAPP_PORT", "9002")
    assert webapp_manager_config().port == 9002


def test_serve_foreground_serves_with_the_spawned_childs_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: list[list[str]] = []
    monkeypatch.setattr("app.webapp.manager.check_tailscale_cert", lambda: None)
    monkeypatch.setattr("app.webapp.serve.main", lambda argv: served.append(argv) or 0)
    manager = WebappManager(WebappManagerConfig(host="127.0.0.1", port=18455))
    assert manager.serve_foreground() == 0
    assert served == [manager.serve_args()]
    assert manager.serve_args()[:4] == ["--host", "127.0.0.1", "--port", "18455"]


def test_serve_foreground_refuses_a_disabled_webapp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.webapp.serve.main", lambda argv: pytest.fail("a disabled webapp must not serve")
    )
    assert WebappManager(WebappManagerConfig(enabled=False)).serve_foreground() == 1
