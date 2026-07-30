"""Low-level helpers shared by every domain module in ``src.config``.

JSON/env/dotenv plumbing and the local-override writer live here, separate
from the per-domain dataclasses and parsers, so a domain module (``hub.py``,
``gmail.py``, ...) can import them without importing the package's own
``__init__.py`` (which imports the domain modules) and creating a cycle.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def project_root() -> Path:
    """Repository root (the directory containing ``config/`` and ``pyproject.toml``)."""
    return Path(__file__).resolve().parents[2]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: ``KEY=value`` lines into ``os.environ`` if not already set."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def _as_bool(env_value: str | None, default: bool) -> bool:
    """Coerce an env string (or fall back to ``default``) to a bool.

    Accepts the usual truthy/falsy spellings; an unrecognized value keeps the
    default rather than silently flipping the flag.
    """
    if env_value is None:
        return bool(default)
    token = env_value.strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _as_sources(value: str | list[Any] | tuple[Any, ...] | None) -> tuple[str, ...]:
    """Normalize a JSON list or comma-separated ``WR_SOURCES`` value.

    Source order is stable and duplicates are removed. An empty/invalid value
    falls back to WhatsApp so the historical single-source configuration keeps
    working instead of silently disabling ingestion.
    """
    raw = value.split(",") if isinstance(value, str) else (value or [])
    sources: list[str] = []
    for item in raw:
        source = str(item).strip().lower()
        if source and source not in sources:
            sources.append(source)
    return tuple(sources or ["whatsapp"])


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _local_config_path(root: Path) -> Path:
    """Return the host override path, honoring the e2e-safe environment seam.

    ``WR_LOCAL_CONFIG_PATH`` lets isolated processes such as the browser e2e
    harness use a disposable override file instead of opening or modifying the
    developer's ignored ``config/local.json``. Relative overrides stay rooted
    at the repository, matching the normal local-config path.
    """
    configured = os.environ.get("WR_LOCAL_CONFIG_PATH")
    if not configured:
        return root / "config" / "local.json"
    path = Path(configured)
    return path if path.is_absolute() else root / path


def save_local_overrides(partial: dict[str, Any], root: Path | None = None) -> Path:
    """Deep-merge ``partial`` into the selected local-config file (atomically).

    This is the per-host override layer the webapp's safe-settings form writes to
    — never the committed ``config/default.json``. ``WR_LOCAL_CONFIG_PATH``
    redirects e2e writes to its disposable fixture. Existing keys not present
    in ``partial`` are preserved. Returns the path written.
    """
    root = root or project_root()
    target = _local_config_path(root)
    current = _load_json(target)
    merged = _deep_merge(current, partial)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target
