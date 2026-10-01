"""Image and video understanding through the gateway's vision MCP server.

Eight tools differing only in the instructions the gateway pairs with your
prompt, so picking the right one matters more than wording the prompt well.
"""

from __future__ import annotations

import base64
import mimetypes
import shutil
import subprocess
import tempfile
from functools import cache
from pathlib import Path

from clients.litellm_client import get_headers, litellm_request
from core.logging import get_logger

MAX_IMAGE_MB = 5
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v")
OUTPUT_TYPES = ("code", "prompt", "spec", "description")
MAX_FRAMES = 30
FRAME_FPS = 1
FRAME_HEIGHT = 360
FRAME_QUALITY = 3

# An image travels inline, and the model thinks before it answers.
MCP_TIMEOUT = 180

logger = get_logger("vision")


class VisionToolError(RuntimeError):
    """The tool said no. A broken gateway raises plain RuntimeError instead."""


@cache
def _server_id() -> str:
    r = litellm_request("GET", "/v1/mcp/server", headers=get_headers())
    if r.status_code != 200:
        raise RuntimeError(
            f"Could not list MCP servers ({r.status_code}): {r.text[:200]}"
        )

    for server in r.json():
        if "vision" in (server.get("server_name") or ""):
            return server["server_id"]
    raise RuntimeError("No vision MCP server is registered on this gateway.")


def _image(source: str) -> str:
    """A URL passes through; a local file is read into a data URI.

    The gateway is another container, so a path means nothing to it. Checking
    here also refuses an oversized file before it is sent, which costs nothing.
    """
    source = str(source).strip()
    if source.startswith(("http://", "https://", "data:")):
        return source

    path = Path(source)
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(
            f"{path.suffix or '(no extension)'} is not an image format the model reads. "
            f"Convert it to one of {', '.join(IMAGE_SUFFIXES)}."
        )
    if not path.is_file():
        raise ValueError(
            f"{source} is not a file — download it from the chat URL first"
        )

    data = path.read_bytes()
    if len(data) > MAX_IMAGE_MB * 1024 * 1024:
        raise ValueError(
            f"{path.name} is {len(data) / 1024 / 1024:.1f} MB; the limit is {MAX_IMAGE_MB} MB. "
            "Shrink it with ffmpeg and try again."
        )
    mime = mimetypes.guess_type(path.name)[0] or "image/*"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _call(tool: str, arguments: dict) -> str:
    if not arguments.get("prompt", "").strip():
        raise ValueError("prompt is empty — say what you want from the image")

    r = litellm_request(
        "POST",
        "/mcp-rest/tools/call",
        headers=get_headers(),
        json={"name": tool, "server_id": _server_id(), "arguments": arguments},
        timeout=MCP_TIMEOUT,
        # No retry: every call is charged, and a repeat rarely reads differently.
        max_retries=1,
    )
    if r.status_code != 200:
        logger.error(
            f"MCP call failed: tool={tool} status={r.status_code} body={r.text[:300]}"
        )
        raise RuntimeError(f"{tool} failed ({r.status_code}): {r.text[:300]}")

    result = r.json()
    text = result["content"][0]["text"]
    if result.get("isError"):
        logger.error(f"MCP tool reported an error: tool={tool} detail={text[:300]}")
        raise VisionToolError(text)

    logger.info(f"Vision: tool={tool} chars={len(text)}")
    return text


def download_attachment(event_id: str, file_id: str, output: str) -> str:
    """Save a picture or clip sent in chat and return its path.

    Downloads via the event-cache ``/files/download`` endpoint so the sandbox
    never needs a provider token of its own.
    """
    from clients.agent_event_cache_client import AgentEventCacheClient

    r = AgentEventCacheClient().download_file(event_id, file_id, timeout=120)
    if r.content[:1] == b"<":
        raise RuntimeError(
            "Got a web page, not a file — the auth token is probably stale."
        )

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(r.content)
    logger.info(f"Attachment downloaded: output={output} bytes={len(r.content)}")
    return output


def analyze_image(image: str, prompt: str) -> str:
    """Anything the specialised tools below do not cover."""
    return _call("analyze_image", {"image_source": _image(image), "prompt": prompt})


def extract_text_from_screenshot(
    image: str, prompt: str = "Extract all the text."
) -> str:
    """OCR a screenshot, keeping indentation and structure."""
    return _call(
        "extract_text_from_screenshot",
        {"image_source": _image(image), "prompt": prompt},
    )


def diagnose_error_screenshot(
    image: str, prompt: str = "What went wrong and how do I fix it?"
) -> str:
    """Read an error, stack trace or exception and suggest fixes."""
    return _call(
        "diagnose_error_screenshot", {"image_source": _image(image), "prompt": prompt}
    )


def understand_technical_diagram(
    image: str, prompt: str = "Explain this diagram."
) -> str:
    """Explain an architecture, flow, UML or ER diagram."""
    return _call(
        "understand_technical_diagram",
        {"image_source": _image(image), "prompt": prompt},
    )


def analyze_data_visualization(
    image: str, prompt: str = "What does this chart show?"
) -> str:
    """Read a chart or dashboard and surface the trends in it."""
    return _call(
        "analyze_data_visualization", {"image_source": _image(image), "prompt": prompt}
    )


def ui_to_artifact(image: str, output_type: str, prompt: str) -> str:
    """Turn a UI screenshot into code, a prompt, a design spec, or prose."""
    if output_type not in OUTPUT_TYPES:
        raise ValueError(f"output_type must be one of {', '.join(OUTPUT_TYPES)}")
    return _call(
        "ui_to_artifact",
        {"image_source": _image(image), "output_type": output_type, "prompt": prompt},
    )


def ui_diff_check(
    expected: str, actual: str, prompt: str = "Where do these differ?"
) -> str:
    """Compare a reference design against the build. Order matters."""
    return _call(
        "ui_diff_check",
        {
            "expected_image_source": _image(expected),
            "actual_image_source": _image(actual),
            "prompt": prompt,
        },
    )


def _duration(path: str) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _frames(path: str) -> tuple[list[str], float]:
    """Sample a local clip into ordered data URIs. Returns them and the interval.

    The budget is spread over the whole clip: answering coarsely beats answering
    confidently about only its first 30 seconds.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg is not installed — run: apt-get update -qq && apt-get install -y -qq ffmpeg"
        )
    seconds = _duration(path)
    fps = min(FRAME_FPS, MAX_FRAMES / seconds) if seconds > 0 else FRAME_FPS

    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                path,
                "-vf",
                f"fps={fps:.4f},scale=-2:{FRAME_HEIGHT}",
                "-frames:v",
                str(MAX_FRAMES),
                "-q:v",
                str(FRAME_QUALITY),
                f"{tmp}/f_%03d.jpg",
                "-y",
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise RuntimeError(
                f"ffmpeg could not read {path}: {r.stderr.strip()[:200]}"
            )
        shots = sorted(Path(tmp).glob("f_*.jpg"))
        if not shots:
            raise ValueError(f"ffmpeg read no frames from {path} — is it a video?")
        return (
            [
                "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode()
                for f in shots
            ],
            round(1 / fps, 1),
        )


def analyze_video(video: str, prompt: str) -> str:
    """Describe what happens in a clip — scenes, actions, objects.

    A public URL goes straight through; anything on disk is sampled into frames.
    """
    if video.startswith(("http://", "https://")):
        return _call("analyze_video", {"video_source": video, "prompt": prompt})

    path = Path(video)
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError(
            f"{path.suffix or '(no extension)'} is not a video format the model reads. "
            f"Convert it to one of {', '.join(VIDEO_SUFFIXES)}."
        )
    if not path.is_file():
        raise ValueError(f"{video} is not a file — download it from the chat URL first")

    shots, interval = _frames(video)
    logger.info(f"Video sampled: frames={len(shots)} interval={interval}s path={video}")
    return _call(
        "analyze_video",
        {"frames": shots, "frame_interval_seconds": interval, "prompt": prompt},
    )
