"""
Unified model pricing for API usage tracking.

All prices are USD per million tokens.
Prices are aligned with the LiteLLM gateway config.yaml.
Margin 1.0 = 2x markup applied to the base price.
"""

MODEL_PRICING = {
    # --- Anthropic ----------------
    "opus-5": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
        "margin": 1.0,
    },
    "fable-5": {
        "input": 10.0,
        "output": 50.0,
        "cache_write_5m": 12.50,
        "cache_write_1h": 20.0,
        "cache_read": 1.0,
        "margin": 1.0,
    },
    "sonnet-5": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
        "margin": 1.0,
    },
    "sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
        "margin": 1.0,
    },
    "sonnet-4.6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
        "margin": 1.0,
    },
    "sonnet-4-5": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
        "margin": 1.0,
    },
    "sonnet-4.5": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
        "margin": 1.0,
    },
    "sonnet-4": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
        "margin": 1.0,
    },
    "haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.0,
        "cache_read": 0.10,
        "margin": 1.0,
    },
    "haiku-4.5": {
        "input": 1.0,
        "output": 5.0,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.0,
        "cache_read": 0.10,
        "margin": 1.0,
    },
    # --- OpenAI (routed through LiteLLM, margin 0.0) -----------------------
    "gpt-5.6-sol": {
        "input": 5.0,
        "output": 30.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 0.0,
        "cache_read": 0.50,
        "margin": 1.0,
    },
    # GPT-5.5 via Azure — custom pricing (Sonnet-4.6 equivalent per gateway).
    "gpt-5.5": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 0.0,
        "cache_read": 0.30,
        "margin": 1.0,
    },
    # --- Kimi K3 (Fireworks) ------------------------------------------------
    "kimi-k3": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 0.0,
        "cache_write_1h": 0.0,
        "cache_read": 0.30,
        "margin": 1.0,
    },
}

DEFAULT_PRICING = MODEL_PRICING["opus-5"]


def get_pricing(model: str) -> dict:
    """Get pricing for a model by substring match, with fallback to Opus 5.0 defaults."""
    model_lower = (model or "").lower()
    for pattern, pricing in MODEL_PRICING.items():
        if pattern in model_lower:
            return pricing
    return DEFAULT_PRICING
