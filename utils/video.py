"""Text-to-video through the gateway's video MCP server (ByteDance Seedance 2.5)."""

from __future__ import annotations

import base64
import json
import mimetypes
from functools import cache
from pathlib import Path

import requests
from clients.litellm_client import get_headers, litellm_request
from core.logging import get_logger

MIN_SECONDS = 4
MAX_SECONDS = 30
DEFAULT_SECONDS = 5

MCP_TIMEOUT = 180
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 10 * 1024 * 1024

logger = get_logger("video")


class VideoToolError(RuntimeError):
    """The tool said no. A broken gateway raises plain RuntimeError instead."""


@cache
def _server_id() -> str:
    r = litellm_request("GET", "/v1/mcp/server", headers=get_headers())
    if r.status_code != 200:
        raise RuntimeError(
            f"Could not list MCP servers ({r.status_code}): {r.text[:200]}"
        )

    for server in r.json():
        if "video" in (server.get("server_name") or ""):
            return server["server_id"]
    raise RuntimeError("No video MCP server is registered on this gateway.")


def _call_tool(name: str, arguments: dict, timeout: int, max_retries: int = 3) -> dict:
    r = litellm_request(
        "POST",
        "/mcp-rest/tools/call",
        headers=get_headers(),
        json={"name": name, "server_id": _server_id(), "arguments": arguments},
        timeout=timeout,
        max_retries=max_retries,
    )
    if r.status_code != 200:
        logger.error(
            f"MCP call failed: tool={name} status={r.status_code} body={r.text[:300]}"
        )
        raise RuntimeError(f"{name} failed ({r.status_code}): {r.text[:300]}")

    # A refusal is a normal 200 with isError set.
    result = r.json()
    text = result["content"][0]["text"]
    if result.get("isError"):
        logger.error(f"MCP tool reported an error: tool={name} detail={text[:300]}")
        raise VideoToolError(text)
    return json.loads(text)


def _as_media(value: str) -> str:
    """A URL passes through; anything else is a file read into a data URI.

    Seedance fetches URLs itself but cannot see the sandbox, so a local image
    or clip has to travel inside the request.
    """
    text = str(value).strip()
    if text.startswith(("http://", "https://", "data:")):
        return text

    path = Path(text)
    if not path.is_file():
        raise ValueError(f"{text} is neither a URL nor a file on disk")
    media_type = mimetypes.guess_type(path.name)[0] or ""
    kind = media_type.split("/")[0]
    if kind not in ("image", "video"):
        raise ValueError(
            f"Cannot tell what kind of media {path.name!r} is. Give the file its "
            "real extension — the model refuses anything it cannot identify."
        )

    limit = MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES
    size = path.stat().st_size
    if size > limit:
        shrink = (
            f"ffmpeg -v error -i {path} -c:v libx264 -crf 28 -preset veryfast "
            "-an smaller.mp4 -y"
            if kind == "video"
            else f"ffmpeg -v error -i {path} -vf scale=-2:1080 -q:v 3 smaller.jpg -y"
        )
        raise ValueError(
            f"{path.name} is {size / (1024 * 1024):.1f} MB; the limit is "
            f"{limit // (1024 * 1024)} MB. Shrink it and pass the smaller file:\n"
            f"  {shrink}"
        )
    return f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode()}"


def submit_video(
    prompt: str,
    seconds: int = DEFAULT_SECONDS,
    first_frame: str | None = None,
    last_frame: str | None = None,
    reference_images: list[str] | str | None = None,
    reference_video: str | None = None,
) -> str:
    """Start a render and return its video_id. Costs money — one call, one clip."""
    if not prompt.strip():
        raise ValueError("prompt is empty — nothing to generate")
    # Callers sometimes pass "8", not 8.
    if not str(seconds).isdigit() or not MIN_SECONDS <= int(seconds) <= MAX_SECONDS:
        raise ValueError(
            f"seconds must be a whole number between {MIN_SECONDS} and {MAX_SECONDS}"
        )
    seconds = int(seconds)

    arguments: dict = {"prompt": prompt, "seconds": seconds}
    if first_frame:
        arguments["first_frame"] = _as_media(first_frame)
    if last_frame:
        arguments["last_frame"] = _as_media(last_frame)
    if reference_images:
        listed = (
            reference_images
            if isinstance(reference_images, list)
            else [reference_images]
        )
        arguments["reference_images"] = [_as_media(image) for image in listed]
    if reference_video:
        arguments["reference_video"] = _as_media(reference_video)

    # No retry: a repeat submit is a second clip and a second charge.
    result = _call_tool(
        "generate_video",
        arguments,
        timeout=MCP_TIMEOUT,
        max_retries=1,
    )

    video_id = result["video_id"]
    inputs = [
        k
        for k in ("first_frame", "last_frame", "reference_images", "reference_video")
        if k in arguments
    ]
    logger.info(
        f"Video submitted: id={video_id} seconds={seconds} inputs={inputs} "
        f"prompt={prompt[:120]!r}"
    )
    return video_id


def check_video(video_id: str) -> dict:
    """Read a clip's state. Free. A dead job comes back as status 'failed'."""
    try:
        info = _call_tool(
            "check_video_generation_result",
            {"video_id": video_id},
            timeout=MCP_TIMEOUT,
        )
    except VideoToolError as e:
        info = {"status": "failed", "error": str(e)}

    logger.info(f"Video check: id={video_id} status={info.get('status')}")
    return info


def download_video(video_url: str, output: str = "generated_video.mp4") -> str:
    """Save a finished clip. The URL is public, so no auth header."""
    r = requests.get(video_url, timeout=300)
    if r.status_code != 200:
        logger.error(f"Video download failed: status={r.status_code} url={video_url}")
        raise RuntimeError(f"Download failed ({r.status_code}): {r.text[:200]}")

    # An error page saved as .mp4 is what used to make clips unplayable.
    if r.content[4:8] != b"ftyp":
        logger.error(
            f"Video download was not an mp4: bytes={len(r.content)} url={video_url}"
        )
        raise RuntimeError(f"Downloaded {len(r.content)} bytes that are not an mp4.")

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(r.content)
    logger.info(f"Video saved: output={output} bytes={len(r.content)}")
    return output
