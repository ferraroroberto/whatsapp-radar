"""Cross-router helpers — no router imports another router; shared utility
lives here instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def maybe_json(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def cert_present() -> bool:
    return (
        (PROJECT_ROOT / "webapp" / "certificates" / "cert.pem").exists()
        and (PROJECT_ROOT / "webapp" / "certificates" / "key.pem").exists()
    )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"
