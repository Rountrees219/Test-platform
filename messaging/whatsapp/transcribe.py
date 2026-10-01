#!/usr/bin/env python3
"""Transcribe a WhatsApp voice note to text.

Downloads are handled separately via ``whatsapp_interface fetch-media``; this
script POSTs a local audio file to LiteLLM (``ninja-transcribe``).

Usage:
    python messaging/whatsapp/transcribe.py <local_audio_path>

Exit codes:
    0  — transcript printed to stdout
    1  — missing argument, missing file, or transcription failure
"""

import mimetypes
import sys
from pathlib import Path

from clients.litellm_client import get_headers, litellm_request, resolve_model
from messaging.message_utils import audio_upload_part


class _LocalAudioResponse:
    """Minimal response shape for :func:`audio_upload_part`."""

    def __init__(self, *, content_type: str, content: bytes) -> None:
        self.headers = {"Content-Type": content_type}
        self.content = content


def transcribe(audio_path: str) -> str:
    """Transcribe a local audio file and return the transcript."""
    path = Path(audio_path).expanduser()
    if not path.is_file():
        raise RuntimeError(f"Audio file not found: {path}")

    data = path.read_bytes()
    if not data:
        raise RuntimeError(f"Audio file is empty: {path}")

    content_type = mimetypes.guess_type(str(path))[0] or "audio/ogg"
    file_part = audio_upload_part(
        str(path), _LocalAudioResponse(content_type=content_type, content=data)
    )

    headers = {k: v for k, v in get_headers().items() if k.lower() != "content-type"}
    transcription_resp = litellm_request(
        "POST",
        "/v1/audio/transcriptions",
        headers=headers,
        files={"file": file_part},
        data={"model": resolve_model("ninja-transcribe")},
        timeout=120,
    )

    if not transcription_resp.ok:
        raise RuntimeError(
            f"Transcription failed ({transcription_resp.status_code}): "
            f"{transcription_resp.text[:200]}"
        )

    text = transcription_resp.json().get("text", "")
    if not text:
        raise RuntimeError("Transcription returned empty text.")
    return text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: transcribe.py <local_audio_path>", file=sys.stderr)
        sys.exit(1)

    try:
        print(transcribe(sys.argv[1]))
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
