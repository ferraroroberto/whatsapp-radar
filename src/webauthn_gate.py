"""WebAuthn passkey enrollment for the admin webapp.

Actual route access is gated by the bearer-token middleware
(``app/webapp/middleware.py``) plus the Tailscale-only check on the passkey
endpoints themselves — **not** by anything in this module. This module owns:

- the enrolled-credential store (``config/webauthn_devices.json``),
- the registration ceremony (py_webauthn),
- a one-time enrollment window (opened from the tray) so a new device can only
  be added deliberately.

Enrollment is provisioning state only: there is no passkey sign-in (assertion)
ceremony, and no unlock token is minted or tracked (#353).

Single-user by design: one logical user, a small whitelist of devices.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from webauthn import (
    base64url_to_bytes,
    generate_registration_options,
    options_to_json,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from src.paths import PROJECT_ROOT
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

DEFAULT_DEVICES_PATH = PROJECT_ROOT / "config" / "webauthn_devices.json"

# Fixed user handle — this app has exactly one logical user.
_USER_ID = b"whatsapp-radar-user"
_USER_NAME = "whatsapp-radar"

_CHALLENGE_TTL = 300.0           # 5 min to complete a ceremony
_ENROLL_WINDOW_DEFAULT = 300.0   # tray "enroll device" window length


@dataclass
class _Challenge:
    value: bytes
    label: str
    created_at: float


class WebAuthnGate:
    """Stateful holder for ceremonies and the device whitelist."""

    def __init__(self, devices_path: Path | None = None) -> None:
        self._devices_path = devices_path or DEFAULT_DEVICES_PATH
        self._lock = threading.Lock()
        self._reg_challenge: _Challenge | None = None
        self._enroll_until = 0.0

    # ----------------------------------------------------------- config
    @staticmethod
    def configured(cfg: WebappConfig) -> bool:
        """True when a relying party is set — i.e. the passkey gate is live."""
        return bool(
            getattr(cfg, "webauthn_rp_id", "")
            and getattr(cfg, "webauthn_origin", "")
        )

    # ------------------------------------------------- enrollment window
    def open_enrollment_window(self, seconds: float = _ENROLL_WINDOW_DEFAULT) -> float:
        """Open a one-time window during which a new passkey may register."""
        with self._lock:
            self._enroll_until = time.time() + seconds
        logger.info(f"🔐 Passkey enrollment window open for {int(seconds)}s")
        return self._enroll_until

    def enrollment_open(self) -> bool:
        with self._lock:
            return time.time() < self._enroll_until

    def enrollment_seconds_left(self) -> int:
        with self._lock:
            return max(0, int(self._enroll_until - time.time()))

    # ------------------------------------------------------ device store
    def load_devices(self) -> list[dict[str, Any]]:
        if not self._devices_path.exists():
            return []
        try:
            raw = json.loads(self._devices_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"⚠️  Could not read {self._devices_path}: {exc}")
            return []
        return list(raw.get("devices") or [])

    def _save_devices(self, devices: list[dict[str, Any]]) -> None:
        self._devices_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._devices_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"devices": devices}, indent=2), encoding="utf-8")
        os.replace(tmp, self._devices_path)

    def list_devices(self) -> list[dict[str, Any]]:
        """Public view of enrolled devices (no key material)."""
        return [
            {
                "id": d.get("id"),
                "label": d.get("label"),
                "added_at": d.get("added_at"),
            }
            for d in self.load_devices()
        ]

    def remove_device(self, device_id: str) -> bool:
        with self._lock:
            devices = self.load_devices()
            kept = [d for d in devices if d.get("id") != device_id]
            if len(kept) == len(devices):
                return False
            self._save_devices(kept)
        logger.info(f"🗑️  Removed enrolled passkey {device_id}")
        return True

    # ----------------------------------------------------- registration
    def begin_registration(self, cfg: WebappConfig, label: str) -> dict[str, Any]:
        """Build registration options for a new platform passkey.

        Only allowed while the enrollment window is open.
        """
        if not self.enrollment_open():
            raise PermissionError("enrollment window is closed")
        existing = self.load_devices()
        exclude = [
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(d["credential_id"]))
            for d in existing
            if d.get("credential_id")
        ]
        options = generate_registration_options(
            rp_id=cfg.webauthn_rp_id,
            rp_name=cfg.webauthn_rp_name or "WhatsApp Radar",
            user_id=_USER_ID,
            user_name=_USER_NAME,
            user_display_name=label or "WhatsApp Radar device",
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude or None,
        )
        with self._lock:
            self._reg_challenge = _Challenge(
                value=options.challenge,
                label=label or "device",
                created_at=time.time(),
            )
        result: dict[str, Any] = json.loads(options_to_json(options))
        return result

    def finish_registration(self, cfg: WebappConfig, credential: Any) -> dict[str, Any]:
        """Verify a registration response and persist the new passkey."""
        with self._lock:
            challenge = self._reg_challenge
            self._reg_challenge = None
        if challenge is None or time.time() - challenge.created_at > _CHALLENGE_TTL:
            raise PermissionError("registration challenge expired — retry")
        if not self.enrollment_open():
            raise PermissionError("enrollment window closed before finish")
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge.value,
            expected_rp_id=cfg.webauthn_rp_id,
            expected_origin=cfg.webauthn_origin,
            require_user_verification=True,
        )
        device = {
            "id": secrets.token_hex(8),
            "label": challenge.label,
            "credential_id": bytes_to_base64url(verification.credential_id),
            "public_key": bytes_to_base64url(verification.credential_public_key),
            "sign_count": verification.sign_count,
            "added_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self._lock:
            devices = self.load_devices()
            devices.append(device)
            self._save_devices(devices)
            self._enroll_until = 0.0  # one device per opened window
        logger.info(f"✅ Enrolled passkey '{device['label']}' ({device['id']})")
        return {"id": device["id"], "label": device["label"]}
