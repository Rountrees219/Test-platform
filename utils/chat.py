"""
Chat Completion Utilities
=========================

Send chat messages to any supported LLM through the gateway.

Supported models:
    - claude-opus, claude-sonnet, claude-haiku (Anthropic)
    - gpt-5 (OpenAI)
    - ninja-fast, ninja-standard, ninja-complex (NinjaTech)

Usage:
    from utils.chat import chat, chat_stream, chat_messages

    # Simple one-shot
    answer = chat("What is the capital of France?")

    # With model selection
    answer = chat("Explain quantum computing", model="gpt-5")

    # Full message history
    answer = chat_messages([
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ])

    # Streaming
    for chunk in chat_stream("Tell me a story"):
        print(chunk, end="", flush=True)
"""

import json
from typing import Generator

import requests
from clients.litellm_client import (
    api_url,
    get_config,
    get_headers,
    litellm_request,
    resolve_model,
)


def chat_messages(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    timeout: int = 120,
    **kwargs,
) -> str:
    """
    Send a chat completion request with full message history.

    Args:
        messages:    List of message dicts with 'role' and 'content' keys.
        model:       Model alias or full ID. Defaults to config default.
        max_tokens:  Maximum tokens in the response.
        temperature: Sampling temperature (0.0 - 1.0).
        timeout:     Request timeout in seconds.
        **kwargs:    Additional parameters passed to the API.

    Returns:
        The assistant's response text.

    Raises:
        RuntimeError: If the API returns a non-200 status.
    """
    cfg = get_config()
    model_id = resolve_model(model) if model else cfg["default_model"]

    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        **kwargs,
    }

    r = litellm_request(
        "POST",
        "/v1/chat/completions",
        headers=get_headers(),
        json=payload,
        timeout=timeout,
    )

    if r.status_code != 200:
        try:
            error = r.json().get("error", {}).get("message", r.text[:300])
        except Exception:
            error = r.text[:300]
        raise RuntimeError(f"Chat completion failed ({r.status_code}): {error}")

    return r.json()["choices"][0]["message"]["content"]


def chat(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    **kwargs,
) -> str:
    """
    Simple one-shot chat: send a prompt, get a response.

    Args:
        prompt:      The user message.
        model:       Model alias or full ID (e.g. "claude-sonnet", "gpt-5").
        system:      Optional system prompt.
        max_tokens:  Maximum tokens in the response.
        temperature: Sampling temperature.

    Returns:
        The assistant's response text.

    Examples:
        >>> chat("What is 2+2?")
        '4'

        >>> chat("Write a haiku about coding", model="claude-opus")
        'Lines of logic flow...'

        >>> chat("Summarize this", model="gpt-5", system="Be concise.")
        '...'
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    return chat_messages(
        messages, model=model, max_tokens=max_tokens, temperature=temperature, **kwargs
    )


def chat_stream(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    timeout: int = 120,
) -> Generator[str, None, None]:
    """
    Stream a chat response token by token.

    Args:
        prompt:      The user message.
        model:       Model alias or full ID.
        system:      Optional system prompt.
        max_tokens:  Maximum tokens in the response.
        temperature: Sampling temperature.
        timeout:     Request timeout in seconds.

    Yields:
        Text chunks as they arrive.

    Example:
        for chunk in chat_stream("Tell me a joke"):
            print(chunk, end="", flush=True)
    """
    cfg = get_config()
    model_id = resolve_model(model) if model else cfg["default_model"]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    r = litellm_request(
        "POST",
        "/v1/chat/completions",
        headers=get_headers(),
        json=payload,
        timeout=timeout,
        stream=True,
    )

    if r.status_code != 200:
        raise RuntimeError(f"Chat stream failed ({r.status_code}): {r.text[:300]}")

    for line in r.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8")
        if line.startswith("data: "):
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


def chat_json(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    max_tokens: int = 4096,
) -> dict:
    """
    Chat and parse the response as JSON.

    Adds a system instruction to return valid JSON. Parses the response
    and returns a Python dict.

    Args:
        prompt:     The user message.
        model:      Model alias or full ID.
        system:     Additional system instructions (prepended to JSON instruction).
        max_tokens: Maximum tokens in the response.

    Returns:
        Parsed JSON as a dict.

    Raises:
        ValueError: If the response is not valid JSON.

    Example:
        data = chat_json("List 3 colors with hex codes")
        # Returns: {"colors": [{"name": "red", "hex": "#FF0000"}, ...]}
    """
    json_system = "You must respond with valid JSON only. No markdown, no explanation."
    if system:
        json_system = f"{system}\n\n{json_system}"

    response = chat(
        prompt, model=model, system=json_system, max_tokens=max_tokens, temperature=0.0
    )

    # Strip markdown code fences if present
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Response is not valid JSON: {e}\nResponse: {text[:500]}")


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Chat Utility Test ===\n")

    # Test simple chat
    print("1. Simple chat (default model):")
    result = chat("What is 2+2? Reply with just the number.")
    print(f"   Response: {result.strip()}\n")

    # Test with specific model
    print("2. Chat with claude-haiku:")
    result = chat("Say hello in French. One word only.", model="claude-haiku")
    print(f"   Response: {result.strip()}\n")

    # Test with system prompt
    print("3. Chat with system prompt:")
    result = chat(
        "What are you?",
        model="claude-sonnet",
        system="You are a pirate. Respond in pirate speak.",
    )
    print(f"   Response: {result.strip()[:100]}\n")

    # Test JSON mode
    print("4. JSON mode:")
    data = chat_json("List 2 colors with their hex codes", model="claude-haiku")
    print(f"   Response: {json.dumps(data, indent=2)[:200]}\n")

    print("✅ All chat tests passed!")
