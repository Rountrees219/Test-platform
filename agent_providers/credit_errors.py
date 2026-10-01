"""Out-of-credits classification for agent CLI runs.

The LiteLLM gateway that fronts every model signals an exhausted account
with one of two terminal responses:

    API Error: 400 {... 'litellm.BudgetExceededError: Budget has been exceeded ...'}
    API Error: 402 402: {'detail': 'Customer keys are not active'}

Claude Code puts a start-of-run failure on stderr but a mid-run one inside the ``--output-format json`` payload on stdout,
and Codex streams it as a JSONL event on stdout.
"""

from __future__ import annotations

# (status code, message fragment) pairs; both halves must appear, lowercased.
_CREDIT_ERROR_SIGNATURES = (
    ("400", "budget has been exceeded"),
    ("429", "budget has been exceeded"),
    ("402", "customer keys are not active"),
)


def is_customer_key_error(output: str | None) -> bool:
    """Return True if ``output`` carries an out-of-credits API error."""
    if not output:
        return False
    lower = output.lower()
    return any(
        code in lower and fragment in lower
        for code, fragment in _CREDIT_ERROR_SIGNATURES
    )


def result_out_of_credits(result) -> bool:
    """Return True if any stream of an ``AgentRunResult`` signals no credits.

    Accepts anything with ``stdout``/``stderr`` (and optionally ``parsed``),
    so it works for both the Claude and the Codex provider.
    """
    if result is None:
        return False
    parsed = getattr(result, "parsed", None)
    streams = (
        getattr(result, "stdout", "") or "",
        getattr(result, "stderr", "") or "",
        getattr(parsed, "text", "") or "",
    )
    return any(is_customer_key_error(stream) for stream in streams)
