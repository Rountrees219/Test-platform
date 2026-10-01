"""Audio generation through the gateway's audio MCP server (ElevenLabs).

Tools that produce audio return the bytes inline, base64-encoded, so every
function here takes a path and returns a path — the audio never passes through
the caller's context.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from functools import cache
from pathlib import Path

import requests
from clients.litellm_client import get_headers, litellm_request
from core.logging import get_logger
from tinytag import TinyTag

MIN_DURATION_SECONDS = 0.5
MAX_DURATION_SECONDS = 30

MAX_SPEECH_CHARS = 2000
MAX_DIALOGUE_CHARS = 2000

MIN_MUSIC_MS = 3000
MAX_MUSIC_MS = 600000

MAX_INPUT_BYTES = 20 * 1024 * 1024
# Bytes say nothing about length, and these tools are priced by the second.
MAX_INPUT_SECONDS = 900
MIN_ISOLATION_SECONDS = 5

# 225 credits a second measured, times the credit rate, doubled for margin.
DUBBING_USD_PER_SECOND = 225 * 0.00022 * 2

MCP_TIMEOUT = 180
_FFPROBE_FLAGS = "-v error -show_entries format=duration -of default=nw=1:nk=1".split()

logger = get_logger("audio")


class AudioToolError(RuntimeError):
    """The tool said no. A broken gateway raises plain RuntimeError instead."""


@cache
def _server_id() -> str:
    r = litellm_request("GET", "/v1/mcp/server", headers=get_headers())
    if r.status_code != 200:
        raise RuntimeError(
            f"Could not list MCP servers ({r.status_code}): {r.text[:200]}"
        )

    for server in r.json():
        if "audio" in (server.get("server_name") or ""):
            return server["server_id"]
    raise RuntimeError("No audio MCP server is registered on this gateway.")


def _call_tool(name: str, arguments: dict, max_retries: int = 3) -> list[dict]:
    """Call an audio tool. Anything that generates passes ``max_retries=1``:
    a retry is a second generation and a second charge."""
    r = litellm_request(
        "POST",
        "/mcp-rest/tools/call",
        headers=get_headers(),
        json={"name": name, "server_id": _server_id(), "arguments": arguments},
        timeout=MCP_TIMEOUT,
        max_retries=max_retries,
    )
    if r.status_code != 200:
        logger.error(
            f"MCP call failed: tool={name} status={r.status_code} body={r.text[:300]}"
        )
        raise RuntimeError(f"{name} failed ({r.status_code}): {r.text[:300]}")

    # A refusal is a normal 200 with isError set.
    result = r.json()
    content = result["content"]
    if result.get("isError"):
        text = content[0].get("text", "")
        logger.error(f"MCP tool reported an error: tool={name} detail={text[:300]}")
        raise AudioToolError(text)
    return content


def _save(part: dict, output: str) -> str:
    """Write the audio carried in a tool result. Bytes arrive base64-encoded."""
    data = base64.b64decode(part["resource"]["blob"])
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(data)
    logger.info(f"Audio saved: output={output} bytes={len(data)}")
    return output


def download_audio(event_id: str, file_id: str, output: str = "recording.mp3") -> str:
    """Save a voice message or audio file sent in chat. Free.

    Downloads via the event-cache ``/files/download`` endpoint so the sandbox
    never needs a provider token of its own.
    """
    from clients.agent_event_cache_client import AgentEventCacheClient

    r = AgentEventCacheClient().download_file(event_id, file_id, timeout=120)
    if r.content[:1] == b"<":
        raise RuntimeError(
            "Got a web page, not audio — the auth token is probably stale."
        )

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(r.content)
    logger.info(f"Audio downloaded: output={output} bytes={len(r.content)}")
    return output


def _audio_seconds(path: str, size: int) -> float:
    """How long a recording runs — the number the per-second tools are billed on."""
    probe = shutil.which("ffprobe")
    if probe:
        try:
            out = subprocess.run(
                [probe, *_FFPROBE_FLAGS, path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            seconds = float(out.stdout.strip())
            logger.info(f"Duration of {path}: {seconds:.1f}s (ffprobe)")
            return seconds
        except (ValueError, subprocess.SubprocessError):
            pass  # fall through to the headers

    tag = TinyTag.get(path)
    declared = float(tag.duration or 0)
    implied = size * 8 / (tag.bitrate * 1000) if tag.bitrate else 0.0
    if declared and implied and max(declared, implied) > 1.2 * min(declared, implied):
        logger.warning(
            f"Duration readings disagree for {path}: header says {declared:.1f}s, "
            f"bitrate implies {implied:.1f}s — billing on the larger."
        )
    seconds = max(declared, implied)
    logger.info(f"Duration of {path}: {seconds:.1f}s (headers)")
    return seconds


def _read_input(
    path: str, max_bytes: int | None = MAX_INPUT_BYTES
) -> tuple[str, float]:
    """Encode a recording for sending, with the duration the call is priced on.

    Measured here rather than passed in, so the caller cannot under-report it.
    ``max_bytes=None`` is dubbing, which is bounded by its price gate instead.
    """
    data = Path(path).read_bytes()
    if not data:
        raise ValueError(f"{path} is empty")
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(
            f"{path} is {len(data) / 1_048_576:.1f} MB; the limit is "
            f"{max_bytes // 1_048_576} MB per call. Cut out the part that "
            "matters and send that instead — there is no audio editor installed, "
            "but you can apt-get ffmpeg if it is worth the time."
        )

    seconds = _audio_seconds(path, len(data))
    if not seconds:
        raise ValueError(f"Could not read the length of {path} — is it audio?")
    if max_bytes is not None and seconds > MAX_INPUT_SECONDS:
        raise ValueError(
            f"{path} runs {seconds / 60:.1f} minutes; the limit is "
            f"{MAX_INPUT_SECONDS // 60} per call, which already costs about "
            f"${MAX_INPUT_SECONDS * 0.003667 * 2:.2f}. Ask which section they "
            "actually want rather than processing the lot; to cut it yourself, "
            "apt-get ffmpeg first."
        )
    return base64.b64encode(data).decode(), float(seconds)


def create_dubbing(
    path: str,
    target_language: str,
    source_language: str | None = None,
    confirmed: bool = False,
) -> dict:
    """Start dubbing into another language. **The priciest thing here.**

    Refuses unless ``confirmed=True``, and the refusal carries the actual price
    of this particular file — quote that to the user, get a yes, then call again.
    Returns the ids to poll ``check_dubbing`` with; the audio is not ready yet.
    """
    audio_base64, seconds = _read_input(path, max_bytes=None)
    cost = seconds * DUBBING_USD_PER_SECOND
    if not confirmed:
        raise ValueError(
            f"Dubbing {path} ({seconds / 60:.1f} minutes) will cost about "
            f"${cost:.2f}. Tell the user that figure, wait for them to agree, "
            "then call again with confirmed=True. Do not assume they are fine "
            "with it."
        )

    arguments: dict = {
        "audio_base64": audio_base64,
        "target_language": target_language,
        "filename": Path(path).name,
        "seconds": seconds,
    }
    if source_language:
        arguments["source_language"] = source_language

    ids = json.loads(_call_tool("create_dubbing", arguments, max_retries=1)[0]["text"])
    logger.info(
        f"Dubbing started: project={ids.get('project_id')} seconds={seconds:.0f} "
        f"target={target_language} est_usd={cost:.2f}"
    )
    return ids


def check_dubbing(project_id: str, language_id: str) -> dict:
    """Read a dubbing job's state. Free. Poll every 30s; it takes about as long
    as the recording. Once done the reply carries a link that expires in an hour."""
    info = json.loads(
        _call_tool(
            "check_dubbing", {"project_id": project_id, "language_id": language_id}
        )[0]["text"]
    )
    logger.info(f"Dubbing check: project={project_id} status={info.get('status')}")
    return info


def get_dubbing_transcript(
    project_id: str, language_id: str | None = None
) -> list[dict]:
    """Read a finished job's transcript. Free. Pass language_id for the translation."""
    arguments: dict = {"project_id": project_id}
    if language_id:
        arguments["language_id"] = language_id
    body = json.loads(_call_tool("get_dubbing_transcript", arguments)[0]["text"])
    logger.info(
        f"Dubbing transcript: project={project_id} segments={body.get('count')}"
    )
    return body.get("segments", [])


def download_dubbing(audio_url: str, output: str = "dubbed.flac") -> str:
    """Save a finished dub. The link is signed and public, so no token is sent."""
    r = requests.get(audio_url, timeout=300)
    if not r.ok:
        logger.error(f"Dub download failed: status={r.status_code}")
        raise RuntimeError(
            f"Download failed ({r.status_code}) — the link expires an hour after "
            "check_dubbing hands it over. Call check_dubbing again for a fresh one."
        )
    if r.content[:4] != b"fLaC":
        raise RuntimeError(
            f"Downloaded {len(r.content)} bytes that are not a FLAC file."
        )

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(r.content)
    logger.info(f"Dub saved: output={output} bytes={len(r.content)}")
    return output


def voice_changer(
    path: str,
    voice_id: str,
    output: str = "voice_changed.mp3",
    remove_background_noise: bool = False,
) -> str:
    """Re-say a recording in another voice. Costs money, by the second of input."""
    audio_base64, seconds = _read_input(path)

    content = _call_tool(
        "voice_changer",
        {
            "audio_base64": audio_base64,
            "voice_id": voice_id,
            "seconds": seconds,
            "remove_background_noise": remove_background_noise,
        },
        max_retries=1,
    )
    logger.info(f"Voice changed: seconds={seconds:.1f} voice={voice_id}")
    return _save(content[0], output)


def isolate_audio(path: str, output: str = "isolated.mp3") -> str:
    """Strip noise and music, leaving the voice. Costs money, by the second."""
    audio_base64, seconds = _read_input(path)
    if seconds < MIN_ISOLATION_SECONDS:
        raise ValueError(
            f"{path} is {seconds:.1f}s; isolation needs at least "
            f"{MIN_ISOLATION_SECONDS}s and a shorter clip is refused after billing."
        )

    content = _call_tool(
        "isolate_audio",
        {"audio_base64": audio_base64, "seconds": seconds},
        max_retries=1,
    )
    logger.info(f"Audio isolated: seconds={seconds:.1f} input={path}")
    return _save(content[0], output)


def list_voices(search: str | None = None, page_size: int = 20) -> list[dict]:
    """List voices available for speech. Free. Use this before naming a voice_id."""
    arguments: dict = {"page_size": page_size}
    if search:
        arguments["search"] = search

    voices = json.loads(_call_tool("list_voices", arguments)[0]["text"])["voices"]
    logger.info(f"Voices listed: count={len(voices)} search={search!r}")
    return voices


def text_to_speech(
    text: str,
    output: str = "speech.mp3",
    voice_id: str | None = None,
) -> str:
    """Read text aloud and save it. Costs money — one call, one recording."""
    if not text.strip():
        raise ValueError("text is empty — nothing to say")
    if len(text) > MAX_SPEECH_CHARS:
        raise ValueError(
            f"text is {len(text):,} characters; the limit is {MAX_SPEECH_CHARS:,} per "
            "call. Split it at a paragraph boundary and make one call per part."
        )

    arguments: dict = {"text": text}
    if voice_id:
        arguments["voice_id"] = voice_id

    content = _call_tool("text_to_speech", arguments, max_retries=1)
    logger.info(f"Speech created: chars={len(text)} voice={voice_id or 'default'}")
    return _save(content[0], output)


def create_dialogue(turns: list[dict], output: str = "dialogue.mp3") -> str:
    """Read a scripted exchange aloud, one voice per turn. Costs money."""
    if not turns:
        raise ValueError("turns is empty — nothing to say")
    total = sum(len(str(turn.get("text", ""))) for turn in turns)
    if total > MAX_DIALOGUE_CHARS:
        raise ValueError(
            f"the turns add up to {total:,} characters; the limit is "
            f"{MAX_DIALOGUE_CHARS:,} per call. Split the script."
        )

    content = _call_tool("create_dialogue", {"inputs": turns}, max_retries=1)
    logger.info(f"Dialogue created: turns={len(turns)} chars={total}")
    return _save(content[0], output)


def compose_music(
    prompt: str,
    output: str = "music.mp3",
    seconds: float | None = None,
    instrumental: bool = False,
) -> str:
    """Compose music and save it. The priciest tool here — cost scales with length."""
    if not prompt.strip():
        raise ValueError("prompt is empty — nothing to compose")

    arguments: dict = {"prompt": prompt, "force_instrumental": instrumental}
    if seconds is not None:
        length_ms = int(float(seconds) * 1000)
        if not MIN_MUSIC_MS <= length_ms <= MAX_MUSIC_MS:
            raise ValueError(
                f"seconds must be between {MIN_MUSIC_MS / 1000:g} and "
                f"{MAX_MUSIC_MS / 1000:g} ({MAX_MUSIC_MS / 60000:g} minutes)"
            )
        arguments["length_ms"] = length_ms

    content = _call_tool("compose_music", arguments, max_retries=1)

    # The second part carries the title the composer chose; absent on older servers.
    title = None
    if len(content) > 1:
        title = json.loads(content[1]["text"]).get("title")
    logger.info(f"Music composed: seconds={seconds} title={title!r}")
    return _save(content[0], output)


def create_sound_effect(
    text: str,
    output: str = "sound_effect.mp3",
    seconds: float | None = None,
) -> str:
    """Generate a sound effect and save it. Costs money — one call, one effect."""
    if not text.strip():
        raise ValueError("text is empty — nothing to generate")

    arguments: dict = {"text": text}
    if seconds is not None:
        if not MIN_DURATION_SECONDS <= float(seconds) <= MAX_DURATION_SECONDS:
            raise ValueError(
                f"seconds must be between {MIN_DURATION_SECONDS} and {MAX_DURATION_SECONDS}"
            )
        arguments["duration_seconds"] = float(seconds)

    content = _call_tool("create_sound_effect", arguments, max_retries=1)
    logger.info(f"Sound effect created: seconds={seconds} text={text[:120]!r}")
    return _save(content[0], output)
