"""
Turn text into spoken audio using ElevenLabs Eleven v3 through the gateway.
Voices: alloy, amber, ash, august, blue, coral, lily, onyx, sage, verse.
Output is always MP3.

Usage:
    # From the command line (preferred — no shell quoting problems)
    python -m utils.speech "Good morning, here is your summary." -o brief.mp3
    python -m utils.speech --file article.txt -o article.mp3 --voice onyx

    # From Python
    from utils.speech import speak
    path = speak("Good morning.", output="brief.mp3", voice="sage")
"""

import argparse
import sys
from pathlib import Path

from clients.litellm_client import get_headers, litellm_request, resolve_model
from core.logging import get_logger

VOICES = (
    "alloy",
    "amber",
    "ash",
    "august",
    "blue",
    "coral",
    "lily",
    "onyx",
    "sage",
    "verse",
)

DEFAULT_VOICE = "alloy"
DEFAULT_TTS_MODEL = "ninja-tts"

# ElevenLabs rejects past 5000 per request; kept under that so an uneven split
# still fits. Checked here because the gateway reports going over as a bare 500.
MAX_CHARS = 4500

logger = get_logger("speech")


def speak(
    text: str,
    output: str = "speech.mp3",
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_TTS_MODEL,
    timeout: int = 120,
) -> str:
    """
    Generate speech from text and save it as an MP3.

    Args:
        text:    The words to speak.
        output:  Output file path.
        voice:   One of VOICES, or a raw ElevenLabs voice id.
        model:   Model alias or full gateway ID.
        timeout: Request timeout in seconds.

    Returns:
        Path to the saved audio file.

    Raises:
        ValueError:   If text is empty, too long, or voice is blank.
        RuntimeError: If the request fails or returns something that is not audio.

    Example:
        >>> speak("Your build finished.", output="done.mp3")
        'done.mp3'
    """
    if not text.strip():
        raise ValueError("text is empty — nothing to speak")
    if not voice or not voice.strip():
        raise ValueError(f"voice is required. Use one of: {', '.join(VOICES)}")
    if len(text) > MAX_CHARS:
        raise ValueError(
            f"text is {len(text):,} characters; the limit is {MAX_CHARS:,} per "
            f"request. Split it into parts and generate one file each."
        )

    resolved = resolve_model(model)
    logger.info(
        f"Requesting speech: model={resolved} voice={voice} "
        f"chars={len(text)} output={output}"
    )

    r = litellm_request(
        "POST",
        "/v1/audio/speech",
        headers=get_headers(),
        json={"model": resolved, "input": text, "voice": voice},
        timeout=timeout,
    )

    if r.status_code != 200:
        logger.error(
            f"Speech generation failed: model={resolved} "
            f"status={r.status_code} body={r.text[:300]}"
        )
        raise RuntimeError(
            f"Speech generation failed ({r.status_code}): {r.text[:300]}"
        )

    # A failing gateway can answer 200 with an error page — don't save that as .mp3.
    content_type = r.headers.get("Content-Type", "")
    if "audio" not in content_type:
        logger.error(
            f"Expected audio, got Content-Type={content_type!r} "
            f"body={r.content[:200]!r}"
        )
        raise RuntimeError(
            f"Expected audio, got Content-Type {content_type!r}: {r.content[:200]!r}"
        )

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(r.content)
    logger.info(f"Speech saved: output={output} bytes={len(r.content)}")
    return output


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Turn text into speech (MP3).")
    parser.add_argument("text", nargs="?", help="Text to speak")
    parser.add_argument("--file", help="Read the text from this file instead")
    parser.add_argument("-o", "--output", default="speech.mp3", help="Output MP3 path")
    parser.add_argument(
        "--voice", default=DEFAULT_VOICE, help=f"One of: {', '.join(VOICES)}"
    )
    args = parser.parse_args()

    text = Path(args.file).read_text() if args.file else args.text
    if not text:
        parser.error("provide text as an argument or use --file")

    path = speak(text, output=args.output, voice=args.voice)
    print(f"Saved {path} ({Path(path).stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
