"""
Text Embedding Utilities
=========================

Generate vector embeddings for text using OpenAI embedding models.

Supported models:
    - embed-small (openai/openai/text-embedding-3-small) — 1536 dimensions
    - embed-large (openai/openai/text-embedding-3-large) — 3072 dimensions

Usage:
    from utils.embeddings import embed, embed_batch, cosine_similarity

    # Single text
    vector = embed("Hello world")

    # Batch embedding
    vectors = embed_batch(["Hello", "World", "Foo"])

    # Similarity comparison
    score = cosine_similarity(
        embed("king"),
        embed("queen"),
    )
"""

import math

from clients.litellm_client import get_headers, litellm_request, resolve_model

DEFAULT_EMBED_MODEL = "embed-small"


def embed(
    text: str,
    model: str = DEFAULT_EMBED_MODEL,
    timeout: int = 30,
) -> list[float]:
    """
    Generate an embedding vector for a single text string.

    Args:
        text:    The text to embed.
        model:   Model alias or full ID. Options: "embed-small", "embed-large".
        timeout: Request timeout in seconds.

    Returns:
        A list of floats representing the embedding vector.

    Raises:
        RuntimeError: If the API returns an error.

    Examples:
        >>> vec = embed("Hello world")
        >>> len(vec)
        1536

        >>> vec = embed("Hello world", model="embed-large")
        >>> len(vec)
        3072
    """
    model_id = resolve_model(model)

    r = litellm_request(
        "POST",
        "/v1/embeddings",
        headers=get_headers(),
        json={"model": model_id, "input": text},
        timeout=timeout,
    )

    if r.status_code != 200:
        try:
            error = r.json().get("error", {}).get("message", r.text[:300])
        except Exception:
            error = r.text[:300]
        raise RuntimeError(f"Embedding failed ({r.status_code}): {error}")

    return r.json()["data"][0]["embedding"]


def embed_batch(
    texts: list[str],
    model: str = DEFAULT_EMBED_MODEL,
    timeout: int = 60,
) -> list[list[float]]:
    """
    Generate embeddings for multiple texts in a single API call.

    Args:
        texts:   List of text strings to embed.
        model:   Model alias or full ID.
        timeout: Request timeout in seconds.

    Returns:
        List of embedding vectors (one per input text).

    Example:
        >>> vecs = embed_batch(["Hello", "World"])
        >>> len(vecs)
        2
        >>> len(vecs[0])
        1536
    """
    model_id = resolve_model(model)

    r = litellm_request(
        "POST",
        "/v1/embeddings",
        headers=get_headers(),
        json={"model": model_id, "input": texts},
        timeout=timeout,
    )

    if r.status_code != 200:
        try:
            error = r.json().get("error", {}).get("message", r.text[:300])
        except Exception:
            error = r.text[:300]
        raise RuntimeError(f"Batch embedding failed ({r.status_code}): {error}")

    data = r.json()["data"]
    # Sort by index to ensure correct order
    data.sort(key=lambda x: x["index"])
    return [item["embedding"] for item in data]


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """
    Compute cosine similarity between two embedding vectors.

    Args:
        vec_a: First embedding vector.
        vec_b: Second embedding vector.

    Returns:
        Cosine similarity score between -1.0 and 1.0.
        1.0 = identical, 0.0 = orthogonal, -1.0 = opposite.

    Example:
        >>> a = embed("king")
        >>> b = embed("queen")
        >>> score = cosine_similarity(a, b)
        >>> print(f"Similarity: {score:.4f}")
        Similarity: 0.8234
    """
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = math.sqrt(sum(a * a for a in vec_a))
    mag_b = math.sqrt(sum(b * b for b in vec_b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Embedding Utility Test ===\n")

    print("1. Single embedding (embed-small):")
    vec = embed("Hello world")
    print(f"   ✅ Dimensions: {len(vec)}")
    print(f"   First 5 values: {vec[:5]}\n")

    print("2. Single embedding (embed-large):")
    vec = embed("Hello world", model="embed-large")
    print(f"   ✅ Dimensions: {len(vec)}\n")

    print("3. Batch embedding:")
    vecs = embed_batch(["cat", "dog", "car"])
    print(f"   ✅ {len(vecs)} vectors, {len(vecs[0])} dimensions each\n")

    print("4. Cosine similarity:")
    v1 = embed("king")
    v2 = embed("queen")
    v3 = embed("automobile")
    sim_close = cosine_similarity(v1, v2)
    sim_far = cosine_similarity(v1, v3)
    print(f"   king ↔ queen:      {sim_close:.4f}")
    print(f"   king ↔ automobile: {sim_far:.4f}")
    print(f"   ✅ king-queen similarity > king-automobile: {sim_close > sim_far}\n")

    print("✅ All embedding tests passed!")
