#!/usr/bin/env python3
"""
Message Sanitizer — Strip LLM artifacts from outgoing text.

Removes emojis, em dashes, over-punctuation, Slack emoji codes, and
other common LLM-isms to produce clean natural text for posting.

Usage:
    python tools/message_sanitizer.py "Here's some text with 🚀 emojis — and fancy punctuation!!!"
    echo "text" | python tools/message_sanitizer.py --stdin

Python API:
    from tools.message_sanitizer import sanitize
    clean = sanitize("🤖 Hello!!! — World")
"""

import re
import sys


def sanitize(text: str) -> str:
    """Clean LLM artifacts from a message, returning natural text."""
    if not text:
        return text

    # 1. Remove emoji characters (unicode emoji ranges)
    text = _strip_emojis(text)

    # 2. Replace em dashes and double dashes with commas
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    text = re.sub(r"\s*--\s*", ", ", text)

    # 3. Collapse over-punctuation
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r"\.{3,}", "...", text)  # keep single ellipsis
    text = re.sub(r"[!?]{2,}", "?", text)  # mixed like !? or ?!

    # 4. Remove Slack emoji codes like :ghost: :rocket: :wave: etc
    text = re.sub(r":[a-z0-9_+-]+:", "", text)

    # 5. Clean up resulting whitespace
    text = re.sub(r"  +", " ", text)  # collapse multiple spaces
    text = re.sub(r" ,", ",", text)  # fix space-before-comma
    text = re.sub(r",\s*,", ",", text)  # collapse double commas
    text = re.sub(r"^\s*,\s*", "", text)  # remove leading comma
    text = re.sub(r",\s*$", "", text.rstrip())  # remove trailing comma
    lines = text.splitlines()
    lines = [line.rstrip() for line in lines]
    # Remove fully blank lines at start/end
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines)

    # Final trim of leading/trailing whitespace per line
    lines = text.splitlines()
    lines = [line.strip() if not line.strip() else line.lstrip() for line in lines]
    text = "\n".join(lines)

    return text


def _strip_emojis(text: str) -> str:
    """Remove unicode emoji characters from text."""
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # emoticons
        "\U0001f300-\U0001f5ff"  # symbols & pictographs
        "\U0001f680-\U0001f6ff"  # transport & map
        "\U0001f1e0-\U0001f1ff"  # flags
        "\U00002702-\U000027b0"  # dingbats
        "\U000024c2-\U0001f251"  # misc
        "\U0001f900-\U0001f9ff"  # supplemental symbols
        "\U0001fa00-\U0001fa6f"  # chess symbols
        "\U0001fa70-\U0001faff"  # symbols extended-A
        "\U00002600-\U000026ff"  # misc symbols
        "\U0000fe00-\U0000fe0f"  # variation selectors
        "\U0000200d"  # zero width joiner
        "\U0000200b-\U0000200f"  # zero-width chars
        "\U0000e000-\U0000f8ff"  # private use area
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub("", text)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Message Sanitizer — Strip LLM artifacts from text",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/message_sanitizer.py "Hello!!! 🚀 World — great"
  echo "text with 🤖 emojis" | python tools/message_sanitizer.py --stdin
        """,
    )
    parser.add_argument("text", nargs="?", help="Text to sanitize")
    parser.add_argument("--stdin", action="store_true", help="Read from stdin")

    args = parser.parse_args()

    if args.stdin:
        text = sys.stdin.read()
    elif args.text:
        text = args.text
    else:
        parser.error("Provide text as argument or use --stdin")
        return

    print(sanitize(text))


if __name__ == "__main__":
    main()
