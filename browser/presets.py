"""
Task Presets — Quick shortcuts for common browser automation operations.

Presets are pre-configured task templates that reduce the need for
verbose natural language instructions for common operations.

Usage (CLI):
    python -m ninja "screenshot https://example.com"
    python -m ninja "extract text from https://example.com"
    python -m ninja "search for AI news 2026"

Usage (Python):
    from browser.presets import get_preset_task
    task = get_preset_task("screenshot", url="https://example.com")
"""

from typing import Optional

# Preset definitions: name → task template
PRESETS = {
    "screenshot": {
        "description": "Take a screenshot of a URL",
        "task": "Navigate to {url} and take a screenshot. Then use done() with 'Screenshot taken'.",
        "requires_url": True,
    },
    "extract": {
        "description": "Extract the main text content from a page",
        "task": "Navigate to {url} and extract the main text content of the page. Use extract_text('body') to get all the text, then use done() with the extracted text.",
        "requires_url": True,
    },
    "extract_links": {
        "description": "Extract all links from a page",
        "task": "Navigate to {url} and extract all links (a[href]) from the page. For each link, get the text and href. Use done() with the list of links.",
        "requires_url": True,
    },
    "search": {
        "description": "Search the web using a search engine",
        "task": "Go to https://www.bing.com and search for: {query}. Type the query and press Enter. Then extract the top 5 search result titles and URLs from the results page. Use done() with the results.",
        "requires_url": False,
    },
    "fill_form": {
        "description": "Fill and submit a form on a page",
        "task": "Navigate to {url}. Find the form on the page and fill it with the provided data: {query}. Then submit the form. Use done() with the result.",
        "requires_url": True,
    },
    "pdf": {
        "description": "Take a full-page screenshot (PDF-like capture)",
        "task": "Navigate to {url}. Scroll through the entire page to load all content, then take a screenshot. Use done() with 'Full page captured'.",
        "requires_url": True,
    },
    "monitor": {
        "description": "Check if a page loads successfully and report status",
        "task": "Navigate to {url}. Check if the page loads correctly (no errors, reasonable title). Report the page title, URL, and whether it loaded successfully. Use done() with the status report.",
        "requires_url": True,
    },
}


def get_preset_task(
    preset_name: str,
    url: Optional[str] = None,
    query: Optional[str] = None,
) -> str:
    """
    Build a task string from a preset template.

    Args:
        preset_name: Name of the preset (e.g., "screenshot", "extract", "search")
        url: URL to use in the task template
        query: Query/text to use in the task template

    Returns:
        The formatted task string

    Raises:
        ValueError: If preset name is unknown or required parameters are missing
    """
    preset_name = preset_name.lower().strip()

    if preset_name not in PRESETS:
        available = ", ".join(sorted(PRESETS.keys()))
        raise ValueError(f"Unknown preset '{preset_name}'. Available: {available}")

    preset = PRESETS[preset_name]

    if preset.get("requires_url") and not url:
        raise ValueError(f"Preset '{preset_name}' requires a --url parameter")

    task = preset["task"]
    task = task.replace("{url}", url or "")
    task = task.replace("{query}", query or "")

    return task


def list_presets() -> str:
    """Return a formatted list of available presets."""
    lines = ["Available presets:"]
    for name, preset in sorted(PRESETS.items()):
        req = " (requires --url)" if preset.get("requires_url") else ""
        lines.append(f"  {name:15s} — {preset['description']}{req}")
    return "\n".join(lines)
