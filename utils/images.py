"""
Image Generation & Editing Utilities
=====================================

Generate and edit images via the NinjaTech LiteLLM gateway.

Supported models (chat completions → /v1/images/generations and /v1/images/edits):
    - gpt-image       → gpt-image-2.5 (default, state-of-the-art, up to 2K, up to 16 reference images)
    - gpt-image-2.5
    - gpt-image-2.5-sunburst
    - gpt-image-2.5-flare
    - gpt-image-2     → explicit alias for gpt-image-2
    - gpt-image-1.5   → legacy (kept for backward compatibility)

Supported sizes (gpt-image-2):
    Any resolution where:
      • Max edge  < 3840 px
      • Both edges are multiples of 16
      • Long:short ratio ≤ 3:1
      • 655,360 ≤ total pixels ≤ 8,294,400
    Popular sizes: "1024x1024" (square), "1024x1536" (portrait),
                   "1536x1024" (landscape), "2048x2048" (2K square),
                   "auto" (model chooses), or omit for default.

Quick usage:
    from utils.images import generate_image, edit_image

    # Text → image
    path = generate_image("A sunset over mountains")

    # Text + reference images → new image  (multi-reference compositing)
    path = edit_image(
        prompt=(
            "Image 1 is a wooden chair. Image 2 is a tabby cat. "
            "Place the cat from Image 2 on the chair from Image 1. "
            "White background, studio lighting."
        ),
        references=["chair.png", "cat.png"],
        output="cat_on_chair.png",
    )

    # Reference from a directory (all images inside)
    path = edit_image(
        prompt="Arrange all the provided products into a clean catalog layout.",
        reference_dir="./product_refs/",
        output="catalog.png",
    )

Prompting fundamentals for gpt-image-2 (see agent-docs/LITELLM_GUIDE.md for full guide):
    1. Structure: [Subject + adjectives] doing [Action] in [Scene].
                  [Composition/Camera]. [Lighting]. [Style]. [Exact text]. [Aspect ratio].
    2. For references, INDEX them in the prompt: "Image 1: <description>... Image 2: ..."
    3. Put literal text in QUOTES or ALL CAPS for crisp in-image rendering.
    4. For edits, restate the PRESERVE list on every iteration to avoid drift.
    5. Use quality="low" for fast drafts, "medium" default, "high" for dense text/infographics.

CLI:
    python -m utils.images generate "A red apple on a white background"
    python -m utils.images edit --ref chair.png --ref cat.png "Put the cat on the chair"
    python -m utils.images edit --ref-dir ./refs/ "Compose all references into one scene"
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Iterable, Sequence

import requests
from clients.litellm_client import (
    _DEFAULT_BACKOFF_BASE,
    _DEFAULT_MAX_RETRIES,
    RETRIABLE_5XX,
    api_url,
    get_config,
    get_headers,
    litellm_request,
    resolve_model,
)
from utils.cost import record_tool_call_cost

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Popular sizes (gpt-image-2 also accepts any size meeting the general constraints).
# These are the sizes we *recommend* and validate in CLI mode.
RECOMMENDED_SIZES = [
    "1024x1024",  # Square (general-purpose default)
    "1024x1536",  # Portrait (HD)
    "1536x1024",  # Landscape (HD)
    "2048x2048",  # 2K square (experimental)
    "auto",  # Let the model choose
]

# Default model for image generation — gpt-image-2 is the current state-of-the-art.
DEFAULT_IMAGE_MODEL = "gpt-image"  # resolves to alias/openai/gpt-image-2.0

# Reference-image limits
MAX_REFERENCE_IMAGES = 16  # per OpenAI: gpt-image-2 supports up to 16 refs
SUPPORTED_REF_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Quality presets
VALID_QUALITIES = ("low", "medium", "high")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _download_to(url: str, output: str, timeout: int = 60) -> str:
    """Download the generated image URL to `output`, returning the path."""
    r = requests.get(url, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to download image from {url}: HTTP {r.status_code}")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(r.content)
    return output


def _save_item(item: dict, output: str) -> str:
    """Save one `data[i]` object (url or b64_json) to `output`."""
    if item.get("url"):
        return _download_to(item["url"], output)
    if item.get("b64_json"):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(base64.b64decode(item["b64_json"]))
        return output
    raise RuntimeError(f"Response item has no 'url' or 'b64_json': {list(item.keys())}")


def _gather_references(
    references: Sequence[str] | None,
    reference_dir: str | None,
) -> list[Path]:
    """
    Build the final ordered list of reference image paths from an explicit list
    and/or a directory. Validates existence, extension, and count.
    """
    paths: list[Path] = []

    if references:
        for r in references:
            p = Path(r).expanduser()
            if not p.is_file():
                raise FileNotFoundError(f"Reference image not found: {p}")
            if p.suffix.lower() not in SUPPORTED_REF_EXTS:
                raise ValueError(
                    f"Unsupported reference extension {p.suffix!r} for {p.name}. "
                    f"Supported: {sorted(SUPPORTED_REF_EXTS)}"
                )
            paths.append(p)

    if reference_dir:
        d = Path(reference_dir).expanduser()
        if not d.is_dir():
            raise NotADirectoryError(f"Reference directory not found: {d}")
        # Stable, alphabetical ordering so Image 1..N matches file sort order.
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in SUPPORTED_REF_EXTS:
                paths.append(p)

    if not paths:
        raise ValueError(
            "No reference images provided. Pass `references=[...]` or "
            "`reference_dir='path/to/refs'`."
        )

    if len(paths) > MAX_REFERENCE_IMAGES:
        raise ValueError(
            f"Too many reference images ({len(paths)}). gpt-image-2 accepts up to "
            f"{MAX_REFERENCE_IMAGES}. Drop some or split across multiple edits."
        )

    return paths


# ---------------------------------------------------------------------------
# Generate (text → image)
# ---------------------------------------------------------------------------


def generate_image(
    prompt: str,
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = "1024x1024",
    output: str = "generated_image.png",
    n: int = 1,
    quality: str | None = None,
    timeout: int = 180,
) -> str:
    """
    Generate an image from a text prompt (text → image).

    Args:
        prompt:   Text description. For best results follow the structured
                  "Subject → Action → Scene → Composition → Lighting → Style"
                  template (see module docstring / LITELLM_GUIDE.md).
        model:    Model alias or full ID. Default "gpt-image" → gpt-image-2.
        size:     Image dimensions. "1024x1024", "1024x1536", "1536x1024",
                  "2048x2048", "auto", or any gpt-image-2-legal custom size.
        output:   Output file path for the first generated image.
        n:        Number of images (server-side variants). Only the first is saved here;
                  use `generate_images(...)` to save all.
        quality:  Optional "low" | "medium" | "high". Omit to use gateway default.
        timeout:  Request timeout in seconds.

    Returns:
        Path to the saved image file.
    """
    if quality is not None and quality not in VALID_QUALITIES:
        raise ValueError(f"Invalid quality {quality!r}. Use one of {VALID_QUALITIES}.")

    payload: dict = {
        "model": resolve_model(model),
        "prompt": prompt,
        "n": n,
        "size": size,
    }
    if quality:
        payload["quality"] = quality

    r = litellm_request(
        "POST",
        "/v1/images/generations",
        headers=get_headers(),
        json=payload,
        timeout=timeout,
    )

    if r.status_code != 200:
        try:
            err = r.json().get("error", {}).get("message", r.text[:300])
        except Exception:
            err = r.text[:300]
        raise RuntimeError(f"Image generation failed ({r.status_code}): {err}")

    data = r.json()
    if not data.get("data"):
        raise RuntimeError(f"No image data in response: {data}")

    record_tool_call_cost(dict(r.headers), prompt, model)
    return _save_item(data["data"][0], output)


def generate_images(
    prompt: str,
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = "1024x1024",
    n: int = 2,
    output_dir: str = ".",
    prefix: str = "image",
    quality: str | None = None,
    timeout: int = 240,
) -> list[str]:
    """
    Generate multiple images from a single prompt. Files are named
    `{output_dir}/{prefix}_{i}.png` starting at i=1.
    """
    if quality is not None and quality not in VALID_QUALITIES:
        raise ValueError(f"Invalid quality {quality!r}. Use one of {VALID_QUALITIES}.")

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    payload: dict = {
        "model": resolve_model(model),
        "prompt": prompt,
        "n": n,
        "size": size,
    }
    if quality:
        payload["quality"] = quality

    r = litellm_request(
        "POST",
        "/v1/images/generations",
        headers=get_headers(),
        json=payload,
        timeout=timeout,
    )

    if r.status_code != 200:
        try:
            err = r.json().get("error", {}).get("message", r.text[:300])
        except Exception:
            err = r.text[:300]
        raise RuntimeError(f"Image generation failed ({r.status_code}): {err}")

    saved: list[str] = []
    for i, item in enumerate(r.json().get("data", []), 1):
        out_path = str(Path(output_dir) / f"{prefix}_{i}.png")
        try:
            saved.append(_save_item(item, out_path))
        except RuntimeError:
            # Skip individual failures; continue with the rest.
            continue
    return saved


# ---------------------------------------------------------------------------
# Edit (text + reference images → image) — multi-reference compositing
# ---------------------------------------------------------------------------


def edit_image(
    prompt: str,
    references: Sequence[str] | None = None,
    reference_dir: str | None = None,
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = "1024x1024",
    output: str = "edited_image.png",
    n: int = 1,
    quality: str | None = None,
    timeout: int = 240,
) -> str:
    """
    Edit / compose an image using one or more reference images plus a text prompt.

    This is the workflow for "group of files as context":
      • Pass up to 16 reference images (by list and/or directory).
      • Index them in the prompt: "Image 1: …  Image 2: …  Image 3: …".
      • State what must be PRESERVED (identity, layout, brand marks) and what
        must CHANGE. Repeat the preserve list on every iteration to prevent drift.

    Args:
        prompt:        The instruction. Index references as "Image 1 / Image 2 / …".
        references:    Explicit list of reference image paths (processed in order).
        reference_dir: Directory of reference images (picked up alphabetically).
                       You can combine with `references`; the list comes first,
                       then directory files in alphabetical order.
        model:         Default "gpt-image" → gpt-image-2.
        size:          Output size (see RECOMMENDED_SIZES or module docstring).
        output:        Output file path.
        n:             Variants to request; only first saved here.
        quality:       Optional "low" | "medium" | "high".
        timeout:       Request timeout in seconds (edits are slower than generations).

    Returns:
        Path to the saved output image.

    Raises:
        FileNotFoundError: If a listed reference path doesn't exist.
        ValueError: If nothing is provided, >16 references, or bad extension.
        RuntimeError: If the gateway returns a non-200.
    """
    if quality is not None and quality not in VALID_QUALITIES:
        raise ValueError(f"Invalid quality {quality!r}. Use one of {VALID_QUALITIES}.")

    paths = _gather_references(references, reference_dir)

    # Multipart form with the `image` field repeated for each reference.
    # Both "image" (repeated) and "image[]" are accepted by the gateway; we use
    # "image" which matches the OpenAI Python SDK's `image=[...]` pattern.
    # File handles are re-opened inside the retry loop so each attempt gets
    # a fresh read position.
    data = {
        "model": resolve_model(model),
        "prompt": prompt,
        "size": size,
        "n": str(n),
    }
    if quality:
        data["quality"] = quality

    # Omit Content-Type so requests can set the multipart boundary itself.
    headers = {k: v for k, v in get_headers().items() if k != "Content-Type"}

    # edit_image uses a multipart upload, so file handles must be re-opened on
    # each retry attempt — litellm_request() cannot do this transparently.
    # We use the shared constants from litellm_client for consistency.
    r = None
    for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
        fields = [("image", (p.name, open(p, "rb"), _mime_for(p))) for p in paths]
        try:
            r = litellm_request(
                "POST",
                "/v1/images/edits",
                headers=headers,
                data=data,
                files=fields,
                timeout=timeout,
                # Pass max_retries=1 so litellm_request makes a single attempt;
                # the outer loop handles retries with fresh file handles.
                max_retries=1,
            )
        finally:
            for _, (_, fh, _) in fields:
                try:
                    fh.close()
                except Exception:
                    pass

        if r.status_code == 200:
            break
        if r.status_code in RETRIABLE_5XX and attempt < _DEFAULT_MAX_RETRIES:
            time.sleep(_DEFAULT_BACKOFF_BASE * (2 ** (attempt - 1)))
            continue
        try:
            err = r.json().get("error", {}).get("message", r.text[:400])
        except Exception:
            err = r.text[:400]
        raise RuntimeError(f"Image edit failed ({r.status_code}): {err}")

    payload = r.json()
    if not payload.get("data"):
        raise RuntimeError(f"No image data in edit response: {payload}")

    record_tool_call_cost(dict(r.headers), prompt, model)
    return _save_item(payload["data"][0], output)


def _mime_for(path: Path) -> str:
    """Lightweight MIME detection sufficient for multipart uploads."""
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    """
    Command-line interface:

        python -m utils.images generate [options] "<prompt>"
        python -m utils.images edit     [options] "<prompt>"
        python -m utils.images test                            # self-test
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m utils.images",
        description="Generate or edit images via the NinjaTech LiteLLM gateway.",
        epilog=(
            "Examples:\n"
            "  python -m utils.images generate 'A red apple on white background'\n"
            "  python -m utils.images generate --size 1024x1536 --quality high \\\n"
            "         'An infographic of the water cycle'\n"
            "  python -m utils.images edit --ref chair.png --ref cat.png \\\n"
            "         'Image 1 is a chair. Image 2 is a cat. Put the cat on the chair.'\n"
            "  python -m utils.images edit --ref-dir ./product_refs/ \\\n"
            "         'Compose all referenced products into a catalog row on white.'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # shared args
    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--model",
            default=DEFAULT_IMAGE_MODEL,
            help=f"Model alias (default: {DEFAULT_IMAGE_MODEL})",
        )
        p.add_argument(
            "--size",
            default="1024x1024",
            help=f"Image size. Common: {', '.join(RECOMMENDED_SIZES)}",
        )
        p.add_argument(
            "--quality",
            choices=VALID_QUALITIES,
            help="low | medium | high (omit for gateway default)",
        )
        p.add_argument("--n", type=int, default=1, help="Number of variants to request")
        p.add_argument(
            "--timeout", type=int, default=240, help="Request timeout in seconds"
        )

    # generate
    g = sub.add_parser("generate", help="Generate an image from a text prompt.")
    g.add_argument("prompt", help="Text description of the desired image.")
    g.add_argument(
        "-o",
        "--output",
        default="generated_image.png",
        help="Output file path (default: generated_image.png)",
    )
    add_common(g)

    # edit
    e = sub.add_parser(
        "edit",
        help="Edit / composite with one or more reference images.",
    )
    e.add_argument(
        "prompt", help="Instruction. Index refs as 'Image 1', 'Image 2', ..."
    )
    e.add_argument(
        "--ref",
        "--reference",
        action="append",
        default=[],
        dest="references",
        help="Path to a reference image. Repeat for multiple refs "
        f"(up to {MAX_REFERENCE_IMAGES}).",
    )
    e.add_argument(
        "--ref-dir",
        "--reference-dir",
        dest="reference_dir",
        help="Directory of reference images (alphabetical order, merged after --ref).",
    )
    e.add_argument(
        "-o",
        "--output",
        default="edited_image.png",
        help="Output file path (default: edited_image.png)",
    )
    add_common(e)

    # test (self-test)
    sub.add_parser("test", help="Run a quick self-test.")

    args = parser.parse_args()

    try:
        cfg = get_config()
    except Exception as exc:
        print(f"❌ Could not load gateway config: {exc}")
        return 2
    if not cfg.get("api_key") or not cfg.get("base_url"):
        print(
            "❌ Gateway config missing api_key or base_url. "
            "Check /root/.claude/settings.json."
        )
        return 2

    if args.cmd == "generate":
        path = generate_image(
            prompt=args.prompt,
            model=args.model,
            size=args.size,
            output=args.output,
            n=args.n,
            quality=args.quality,
            timeout=args.timeout,
        )
        print(f"✅ Saved: {path} ({os.path.getsize(path):,} bytes)")
        return 0

    if args.cmd == "edit":
        path = edit_image(
            prompt=args.prompt,
            references=args.references or None,
            reference_dir=args.reference_dir,
            model=args.model,
            size=args.size,
            output=args.output,
            n=args.n,
            quality=args.quality,
            timeout=args.timeout,
        )
        print(f"✅ Saved: {path} ({os.path.getsize(path):,} bytes)")
        return 0

    if args.cmd == "test":
        return _selftest()

    parser.print_help()
    return 1


def _selftest() -> int:
    """Quick sanity test against the live gateway."""
    print("=== Image Generation & Editing Self-Test ===\n")

    rc = 0

    print("1. generate_image() with default model (gpt-image-2):")
    try:
        p = generate_image(
            "A minimalist red apple on a pure white background, studio lighting",
            output="/tmp/selftest_gen.png",
            size="1024x1024",
            quality="low",  # keep the test fast
        )
        print(f"   ✅ {p} ({os.path.getsize(p):,} bytes)\n")
    except Exception as e:
        print(f"   ❌ {e}\n")
        rc = 1

    print("2. edit_image() with single reference:")
    try:
        p = edit_image(
            prompt="Add a small green leaf to the apple stem. Preserve everything else.",
            references=["/tmp/selftest_gen.png"],
            output="/tmp/selftest_edit.png",
            size="1024x1024",
            quality="low",
        )
        print(f"   ✅ {p} ({os.path.getsize(p):,} bytes)\n")
    except Exception as e:
        print(f"   ❌ {e}\n")
        rc = 1

    print("Done." if rc == 0 else "Some tests failed.")
    return rc


if __name__ == "__main__":
    raise SystemExit(_cli())
