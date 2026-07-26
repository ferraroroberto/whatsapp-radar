from __future__ import annotations

import socket

import pytest

from tests.e2e import _e2e_live_guard as guard


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_free_port_boots_fresh_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SCAFFOLD_TEST_E2E_LIVE", raising=False)
    port = _reserve_free_port()

    live_opt_in = guard.require_disposable_instance(port, "SCAFFOLD_TEST_E2E_LIVE")

    assert live_opt_in is False
    assert "booting disposable instance" in capsys.readouterr().out


def test_occupied_port_without_flag_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCAFFOLD_TEST_E2E_LIVE", raising=False)
    port = _reserve_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)

        with pytest.raises(pytest.exit.Exception, match="SCAFFOLD_TEST_E2E_LIVE") as excinfo:
            guard.require_disposable_instance(port, "SCAFFOLD_TEST_E2E_LIVE")

    # #197: the exit message must never prescribe "kill + fresh restart" as
    # what the opt-in means -- that meaning is caller-owned and repo-specific.
    # ("kill" alone still legitimately appears describing the default danger
    # being refused, and in "never a by-hand kill".)
    message = str(excinfo.value).lower()
    assert "kill + fresh restart" not in message
    assert "reclaiming this port" not in message
    assert "acting on the live instance" in message


def test_occupied_port_with_flag_acts_on_live_instance_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCAFFOLD_TEST_E2E_LIVE", "1")
    port = _reserve_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)

        live_opt_in = guard.require_disposable_instance(port, "SCAFFOLD_TEST_E2E_LIVE")

    out = capsys.readouterr().out
    assert live_opt_in is True
    assert "acting on the live instance" in out
    # #197: the log line must never say "kill" or "reclaim" either -- those
    # words baked in a specific, wrong meaning for every adopter.
    assert "kill" not in out.lower()
    assert "reclaim" not in out.lower()


def test_port_is_in_use_reflects_a_bound_listener() -> None:
    port = _reserve_free_port()
    assert guard.port_is_in_use(port) is False

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)

        assert guard.port_is_in_use(port) is True
