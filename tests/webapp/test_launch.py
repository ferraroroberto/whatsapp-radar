"""The webapp child launches through ``app.webapp.serve``: HTTP/2 first, uvicorn as fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.webapp.manager import WebappManager, WebappManagerConfig
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
