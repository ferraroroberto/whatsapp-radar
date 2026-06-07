"""Hub Whisper client (OpenAI-shape POST /v1/audio/transcriptions)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from src.config import TranscriptionConfig

_TIMEOUT_SECONDS = 120


class TranscriptionError(Exception):
    """Hub transcription request failed."""


def transcribe_file(path: Path, config: TranscriptionConfig) -> str:
    """Transcribe one audio file; auto-detect language, no translation."""
    url = f"{config.hub_base_url.rstrip('/')}/v1/audio/transcriptions"
    data = path.read_bytes()
    boundary = uuid.uuid4().hex
    filename = path.name
    mime = "audio/ogg" if path.suffix == ".ogg" else "application/octet-stream"

    body_parts: list[bytes] = []
    for name, value in (("model", config.model),):
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body_parts.append(value.encode())
        body_parts.append(b"\r\n")
    body_parts.append(f"--{boundary}\r\n".encode())
    body_parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    )
    body_parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
    body_parts.append(data)
    body_parts.append(b"\r\n")
    body_parts.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(body_parts)

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise TranscriptionError(f"transcription request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TranscriptionError("transcription returned non-JSON") from exc

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TranscriptionError("transcription response missing text")
    return text.strip()
