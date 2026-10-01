# LiteLLM Gateway Guide

Complete guide for agents to use AI models through the NinjaTech LiteLLM gateway.

---

## Table of Contents

1. [Overview](#overview)
2. [Configuration](#configuration)
3. [Utility Library](#utility-library)
4. [Chat Completions](#chat-completions)
5. [Image Generation](#image-generation)
6. [Video Generation](#video-generation)
7. [Embeddings](#embeddings)
8. [Raw API Access](#raw-api-access)
9. [Error Handling](#error-handling)
10. [Building Custom Utilities](#building-custom-utilities)

---

## Overview

The NinjaTech LiteLLM gateway provides a unified OpenAI-compatible API for accessing models from multiple providers (Anthropic, OpenAI, NinjaTech). All requests go through a single base URL with a single API key.

**Gateway URL**: `https://model-gateway.public.beta.myninja.ai`
**Protocol**: OpenAI-compatible REST API
**Auth**: Bearer token

### Available Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/models` | GET | List available models |
| `/v1/chat/completions` | POST | Chat / text generation |
| `/v1/images/generations` | POST | Image generation |
| `/v1/videos` | POST | Video generation (submit) |
| `/v1/videos/{id}` | GET | Video status (poll) |
| `/v1/videos/{id}/content` | GET | Video download |
| `/v1/embeddings` | POST | Text embeddings |

### Available Models

See [MODELS.md](MODELS.md) for the complete model catalog with aliases, capabilities, and parameters.

---

## Configuration

### Settings File

Credentials are stored in `/root/.claude/settings.json`:

```json
{
    "env": {
        "ANTHROPIC_AUTH_TOKEN": "sk-your-api-key",
        "ANTHROPIC_BASE_URL": "https://model-gateway.public.beta.myninja.ai",
        "ANTHROPIC_MODEL": "ninja-cline-complex"
    }
}
```

The utility library reads this file automatically. No manual configuration needed.

### Environment Variable Overrides

You can override settings with environment variables:

```bash
export LITELLM_API_KEY="sk-different-key"
export LITELLM_BASE_URL="https://different-gateway.example.com"
```

### Verifying Configuration

```python
from clients.litellm_client import get_config

cfg = get_config()
print(f"Gateway: {cfg['base_url']}")
print(f"Key:     {cfg['api_key'][:10]}...")
print(f"Default: {cfg['default_model']}")
print(f"Source:  {cfg['source']}")
```

Or from the command line:

```bash
cd /workspace/ninja
python -m clients.litellm_client
```

---

## Utility Library

The `utils/` package provides ready-to-use functions for all model types.

### Package Structure

```
utils/
├── __init__.py          # Package init
├── litellm_client.py    # Core config, auth, model aliases
├── chat.py              # Chat completions (text generation)
├── images.py            # Image generation
├── video.py             # Video generation (async workflow)
└── embeddings.py        # Text embeddings
```

### Quick Import Reference

```python
# Chat
from utils.chat import chat, chat_messages, chat_stream, chat_json

# Images
from utils.images import generate_image, generate_images, edit_image

# Video
from utils.video import submit_video, check_video, download_video

# Embeddings
from utils.embeddings import embed, embed_batch, cosine_similarity

# Config & models
from clients.litellm_client import get_config, resolve_model, MODELS
```

---

## Chat Completions

### Simple One-Shot

```python
from utils.chat import chat

# Default model (from settings.json)
answer = chat("What is the capital of France?")
print(answer)  # "Paris"

# Specific model
answer = chat("Explain quantum computing in one sentence", model="claude-opus")

# With system prompt
answer = chat(
    "What should I cook tonight?",
    model="gpt-5",
    system="You are a professional chef. Be creative and concise.",
)
```

### Full Message History

```python
from utils.chat import chat_messages

response = chat_messages([
    {"role": "system", "content": "You are a Python expert."},
    {"role": "user", "content": "How do I read a CSV file?"},
    {"role": "assistant", "content": "You can use pandas: pd.read_csv('file.csv')"},
    {"role": "user", "content": "What about without pandas?"},
])
print(response)
```

### Streaming

```python
from utils.chat import chat_stream

for chunk in chat_stream("Tell me a short story about a robot"):
    print(chunk, end="", flush=True)
print()  # newline at end
```

### JSON Mode

```python
from utils.chat import chat_json

# Returns a parsed Python dict
data = chat_json("List 3 programming languages with their year of creation")
print(data)
# {"languages": [{"name": "Python", "year": 1991}, ...]}

# With additional system instructions
data = chat_json(
    "Analyze the sentiment of: 'I love this product!'",
    system="You are a sentiment analysis engine.",
    model="claude-haiku",
)
print(data)
# {"sentiment": "positive", "confidence": 0.95}
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | str | required | The user message |
| `model` | str | config default | Model alias or full ID |
| `system` | str | None | System prompt |
| `max_tokens` | int | 4096 | Max response tokens |
| `temperature` | float | 0.7 | Randomness (0.0-1.0) |
| `timeout` | int | 120 | Request timeout (seconds) |

---

## Image Generation

Two workflows are supported:

- **Generation** (text → image) via `/v1/images/generations`
- **Editing / multi-reference composition** (text + images → image) via `/v1/images/edits`

The default model is **`gpt-image`** (alias for `gpt-image-2`), OpenAI's current
state-of-the-art image model. It supports up to **16 reference images**, text
rendering, flexible sizes up to 2K, and reliable instruction-following.

### Basic Generation

```python
from utils.images import generate_image

# Default model (gpt-image-2) and size (1024x1024)
path = generate_image("A sunset over mountain peaks, oil painting style")
print(f"Saved to: {path}")  # "generated_image.png"

# With options
path = generate_image(
    prompt="A futuristic city skyline at night, neon lights",
    model="gpt-image",          # or "gpt-image-2" or "gpt-image-1.5"
    size="1536x1024",           # landscape
    quality="high",             # low | medium | high
    output="city_skyline.png",
)
```

### Multiple Variants

```python
from utils.images import generate_images

paths = generate_images(
    prompt="Variations of a logo for a tech startup",
    n=3,
    output_dir="logos/",
    prefix="logo",
)
print(paths)  # ["logos/logo_1.png", "logos/logo_2.png", "logos/logo_3.png"]
```

### Editing with Reference Images ("group of files as context")

Pass up to **16 reference images** to composite, style-transfer, or surgically edit
existing visuals. This is the most powerful mode of gpt-image-2.

```python
from utils.images import edit_image

# Two references → composite
path = edit_image(
    prompt=(
        "Image 1 is a wooden chair. "
        "Image 2 is an orange tabby cat. "
        "Compose: the cat from Image 2 sits on the chair from Image 1. "
        "Plain white background, studio lighting. "
        "Preserve chair design and cat coloring exactly."
    ),
    references=["chair.png", "cat.png"],
    output="cat_on_chair.png",
)

# Whole directory of references (picked up in alphabetical order)
path = edit_image(
    prompt=(
        "Compose all referenced products into a clean catalog row on white. "
        "Preserve each product's label and colors exactly."
    ),
    reference_dir="./product_refs/",
    output="catalog.png",
    quality="medium",
)

# Mix an explicit ordered list AND a directory
path = edit_image(
    prompt="Use Image 1 as the hero. Images 2–N are secondary props.",
    references=["hero.png"],
    reference_dir="./secondary/",
    output="composition.png",
)
```

### Prompting Fundamentals (gpt-image-2)

Follow this structure for reliable, production-ready outputs:

```
[Subject + Adjectives] doing [Action] in [Scene/Context].
[Composition/Camera]. [Lighting/Atmosphere]. [Style/Medium].
[Exact Text]. [Aspect Ratio / Use Case].
```

**Specific techniques:**

1. **Index references**: Say `"Image 1: …  Image 2: …"` in the prompt. Describe
   each input and how they interact (`"apply Image 2's style to Image 1"`,
   `"put the bird from Image 1 on the elephant in Image 2"`).
2. **Literal text**: Put exact in-image copy in **quotes** or **ALL CAPS**, and
   specify typography (`bold sans-serif, white on charcoal, centered`). Spell
   tricky words letter-by-letter if needed.
3. **Preserve list on edits**: Say what must **not** change — face, pose, brand
   logo, background — and repeat that list on every iteration to prevent drift.
4. **Quality vs latency**:
   - `quality="low"` → fast drafts, large batches, ideation
   - `quality="medium"` → default for most production use
   - `quality="high"` → dense text, infographics, small-font charts, identity-sensitive edits
5. **Photorealism**: Add `photorealistic`, mention a lens (`50mm`, `85mm`) and
   lighting (`soft coastal daylight`, `golden hour`). Ask for real texture
   (`pores`, `fabric wear`, `imperfections`) to avoid "AI polish".
6. **Iterate small**: A clean base prompt + small follow-ups (`"make lighting
   warmer"`, `"remove the extra tree"`) consistently beats one giant rewrite.

### Common Use Cases

| Task | Tip |
|---|---|
| Infographics / diagrams | `quality="high"`, size `1024x1536`, use labeled sections, explicit arrows |
| Logos | Ask for "flat design, minimal strokes, strong silhouette, plain background, no watermark" |
| Product on white | `quality="medium"`, specify "centered product, crisp silhouette, subtle contact shadow" |
| Ads with text | Quote copy verbatim: `'Fresh and clean'`, request "bold sans-serif, high contrast, centered" |
| Translation (localize existing ad) | `edit_image()` with the original as ref, prompt: "Translate text to Spanish. Do not change any other aspect." |
| Character consistency across pages | Generate a "character anchor" image, then use it as a ref for every subsequent page |
| Virtual try-on | Lock identity explicitly ("do not change face, skin tone, pose"). Only change clothes. |
| Compositing from multiple sources | Index every reference (`Image 1/2/3…`) and state the spatial/stylistic relationship |

### Parameters — `generate_image()` / `generate_images()`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | str | required | Image description |
| `model` | str | `"gpt-image"` | `"gpt-image"`, `"gpt-image-2"`, `"gpt-image-1.5"` |
| `size` | str | `"1024x1024"` | `"1024x1024"`, `"1024x1536"`, `"1536x1024"`, `"2048x2048"`, `"auto"`, or any gpt-image-2-legal custom size |
| `quality` | str | (gateway default) | `"low"` / `"medium"` / `"high"` |
| `output` | str | `"generated_image.png"` | Output file path |
| `n` | int | 1 | Number of variants to request |
| `timeout` | int | 180 | Request timeout (seconds) |

### Parameters — `edit_image()`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | str | required | Instruction; index refs as `"Image 1", "Image 2", ...` |
| `references` | list[str] | None | Ordered list of reference image paths |
| `reference_dir` | str | None | Directory of references (alphabetical; merged after `references`) |
| `model` | str | `"gpt-image"` | Same options as generation |
| `size` | str | `"1024x1024"` | Same options as generation |
| `output` | str | `"edited_image.png"` | Output file path |
| `quality` | str | (gateway default) | `"low"` / `"medium"` / `"high"` |
| `n` | int | 1 | Variants requested |
| `timeout` | int | 240 | Request timeout (edits are slower than generations) |

You must provide at least one of `references=[...]` or `reference_dir=...`. The
**combined total cannot exceed 16 images** (gpt-image-2 limit).

### CLI Usage

Quick one-off runs without writing Python:

```bash
# Text → image
python -m utils.images generate "A red apple on a white background"

# With options
python -m utils.images generate --size 1024x1536 --quality high \
    --output infographic.png \
    "An infographic of the water cycle with labeled arrows"

# Edit / compose with multiple references (--ref repeated)
python -m utils.images edit \
    --ref chair.png --ref cat.png \
    --output cat_on_chair.png \
    "Image 1 is a chair. Image 2 is a cat. Put the cat on the chair."

# Or reference a whole directory (alphabetical order)
python -m utils.images edit \
    --ref-dir ./product_refs/ \
    --output catalog.png --size 1536x1024 \
    "Compose all referenced products into a catalog row on pure white."

# Mix both
python -m utils.images edit \
    --ref hero.png --ref-dir ./secondary/ \
    "Image 1 is the hero. The rest are supporting props behind it."

# Quick self-test
python -m utils.images test
```

### Gateway Behavior Notes

- Responses return a **URL** to a PNG. The utilities download it automatically.
- Attaching many references (≥ 4–6) may cause the gateway to **auto-downgrade
  `quality` to `"low"`** to stay within capacity. Harmless for drafts; for
  production, use fewer refs and request `quality="medium"` or `"high"` explicitly.
- `output_format` is accepted but always returns PNG today.
- `background="transparent"` is accepted but doesn't reliably produce true alpha.
- `input_fidelity` is **not supported** on gpt-image-2 (not needed — output is
  high-fidelity by default).

### Model Comparison

| Model | When to Use |
|---|---|
| `gpt-image` (gpt-image-2) | **Default.** Text rendering, multi-ref compositing, flexible sizes, highest quality. |
| `gpt-image-1.5` | Legacy migration only. Validated prompts that haven't been retested on gpt-image-2. |

---

## Video Generation

Videos come from **ByteDance Seedance**, through the gateway's video MCP server.
A clip takes a few minutes, so it is three steps — submit, poll, download —
which leaves you free to talk to the user in between.

```python
from utils.video import submit_video, check_video, download_video

video_id = submit_video("A cat playing with a ball of yarn", seconds=5)  # 4-12

# then, every 15 seconds until it is ready (free):
info = check_video(video_id)
# in_progress -> keep going;  completed -> "video_url";  failed -> "error"

path = download_video(info["video_url"], "cat.mp4")
```

Put the wait in a separate `sleep` command, not a Python loop.

### Tips

- Always **1280x720 landscape MP4**. Resolution and orientation can't be changed.
- The content filter is strict **and inconsistent** — an innocent prompt is
  sometimes rejected. Rewording and trying once more usually works.
- **Every submit costs money, including one the filter rejects.** Never loop;
  tell the user what happened instead.
- Keep the `video_id`. If your run is interrupted you can still collect the clip
  later with `check_video(video_id)` — it is already paid for.

---

## Embeddings

### Single Text

```python
from utils.embeddings import embed

vector = embed("Hello world")
print(f"Dimensions: {len(vector)}")  # 1536

# Higher-dimensional model
vector = embed("Hello world", model="embed-large")
print(f"Dimensions: {len(vector)}")  # 3072
```

### Batch Embedding

```python
from utils.embeddings import embed_batch

texts = ["cat", "dog", "fish", "car", "bicycle"]
vectors = embed_batch(texts)
print(f"{len(vectors)} vectors, {len(vectors[0])} dimensions each")
```

### Similarity Comparison

```python
from utils.embeddings import embed, cosine_similarity

v1 = embed("machine learning")
v2 = embed("artificial intelligence")
v3 = embed("cooking recipes")

sim_close = cosine_similarity(v1, v2)
sim_far = cosine_similarity(v1, v3)

print(f"ML ↔ AI:      {sim_close:.4f}")  # ~0.85
print(f"ML ↔ cooking: {sim_far:.4f}")    # ~0.15
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | str | required | Text to embed |
| `texts` | list[str] | required | Texts to embed (batch) |
| `model` | str | `"embed-small"` | `"embed-small"` (1536d) or `"embed-large"` (3072d) |
| `timeout` | int | 30 | Request timeout (seconds) |

### Use Cases

- **Semantic search**: Embed queries and documents, find nearest neighbors
- **Document similarity**: Compare documents by cosine similarity
- **Clustering**: Group similar texts together
- **RAG**: Retrieve relevant context for chat completions

---

## Raw API Access

If you need to make custom API calls beyond what the utilities provide:

### Using the Client Helpers

```python
from clients.litellm_client import get_headers, api_url, resolve_model
import requests

# Build URL and headers automatically
url = api_url("/v1/chat/completions")
headers = get_headers()

# Make custom request
r = requests.post(url, headers=headers, json={
    "model": resolve_model("claude-sonnet"),
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100,
    "temperature": 0.5,
    "top_p": 0.9,  # custom parameter
})

data = r.json()
print(data["choices"][0]["message"]["content"])
```

### Using curl

```bash
# List models
curl -s -H "Authorization: Bearer $API_KEY" \
  "https://model-gateway.public.beta.myninja.ai/v1/models" | jq .

# Chat completion
curl -s -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -X POST "https://model-gateway.public.beta.myninja.ai/v1/chat/completions" \
  -d '{
    "model": "claude-opus-5",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100
  }'

# Image generation
curl -s -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -X POST "https://model-gateway.public.beta.myninja.ai/v1/images/generations" \
  -d '{
    "model": "alias/openai/gpt-image-2.0",
    "prompt": "A sunset",
    "size": "1024x1024"
  }'

# Video generation — no curl equivalent; it is an MCP tool call.
# Use utils.video (see the Video Generation section above).

# Embeddings
curl -s -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -X POST "https://model-gateway.public.beta.myninja.ai/v1/embeddings" \
  -d '{
    "model": "openai/openai/text-embedding-3-small",
    "input": "Hello world"
  }'
```

---

## Error Handling

### Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `500 APIConnectionError` | Transient gateway issue | Retry after a few seconds |
| `400 Invalid size` | Wrong image/video dimensions | Check valid sizes in MODELS.md |
| `404 Model not found` | Wrong model ID or endpoint | Use aliases or check MODELS.md |
| `503 No available capacity` | Model overloaded | Retry later or use different model |
| `401 Unauthorized` | Invalid API key | Check settings.json |

### Retry Pattern

```python
import time
from utils.chat import chat

def chat_with_retry(prompt, model="claude-sonnet", max_retries=3):
    """Chat with automatic retry on transient errors."""
    for attempt in range(max_retries):
        try:
            return chat(prompt, model=model)
        except RuntimeError as e:
            if attempt < max_retries - 1 and ("500" in str(e) or "503" in str(e)):
                wait = 2 ** attempt  # exponential backoff: 1s, 2s, 4s
                print(f"Retry {attempt + 1}/{max_retries} in {wait}s...")
                time.sleep(wait)
            else:
                raise

answer = chat_with_retry("What is 2+2?")
```

### Image Generation with Retry

```python
from utils.images import generate_image

def generate_image_with_retry(prompt, retries=2, **kwargs):
    """Generate with gpt-image-2, retrying on transient errors."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return generate_image(prompt, model="gpt-image", **kwargs)
        except RuntimeError as e:
            last_err = e
            print(f"Attempt {attempt} failed: {e}. Retrying...")
    raise last_err

path = generate_image_with_retry("A beautiful landscape")
```

---

## Building Custom Utilities

### Pattern: Wrap an Existing Utility

```python
"""Custom utility for generating product descriptions."""

from utils.chat import chat_json

def generate_product_description(product_name: str, features: list[str]) -> dict:
    """Generate a marketing description for a product."""
    prompt = f"""Create a product description for "{product_name}" with these features:
    {', '.join(features)}

    Return JSON with keys: tagline, description, bullet_points"""

    return chat_json(prompt, model="claude-sonnet")

# Usage
result = generate_product_description("SuperWidget", ["fast", "lightweight", "durable"])
print(result["tagline"])
```

### Pattern: Combine Multiple Utilities

```python
"""Custom utility for creating illustrated blog posts."""

from utils.chat import chat
from utils.images import generate_image

def create_illustrated_post(topic: str) -> dict:
    """Generate a blog post with a matching illustration."""

    # Generate the text
    post = chat(
        f"Write a short blog post about: {topic}",
        model="claude-sonnet",
        system="Write engaging, concise blog posts. 2-3 paragraphs.",
    )

    # Generate a matching image
    image_prompt = chat(
        f"Describe a single image that would illustrate this blog post: {post[:500]}",
        model="claude-haiku",
        system="Describe an image in one detailed sentence. No text in the image.",
    )

    image_path = generate_image(image_prompt, model="gpt-image")

    return {"text": post, "image": image_path, "image_prompt": image_prompt}

result = create_illustrated_post("The future of AI")
```

### Pattern: Semantic Search

```python
"""Simple semantic search using embeddings."""

from utils.embeddings import embed, embed_batch, cosine_similarity

class SemanticSearch:
    def __init__(self, documents: list[str]):
        self.documents = documents
        self.vectors = embed_batch(documents)

    def search(self, query: str, top_k: int = 3) -> list[tuple[str, float]]:
        """Find the most relevant documents for a query."""
        query_vec = embed(query)
        scores = [
            (doc, cosine_similarity(query_vec, vec))
            for doc, vec in zip(self.documents, self.vectors)
        ]
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

# Usage
docs = [
    "Python is a programming language",
    "Cats are popular pets",
    "Machine learning uses neural networks",
    "The weather is sunny today",
]
search = SemanticSearch(docs)
results = search.search("AI and deep learning")
for doc, score in results:
    print(f"  {score:.4f}: {doc}")
```

### Pattern: New API Endpoint

If the gateway adds a new endpoint, here's how to create a utility for it:

```python
"""Template for a new utility module."""

import requests
from clients.litellm_client import get_headers, api_url, resolve_model

def my_new_function(param1: str, model: str = "default-model") -> dict:
    """
    Description of what this function does.

    Args:
        param1: Description.
        model:  Model alias or full ID.

    Returns:
        Description of return value.
    """
    model_id = resolve_model(model)

    r = requests.post(
        api_url("/v1/new-endpoint"),
        headers=get_headers(),
        json={
            "model": model_id,
            "param1": param1,
        },
        timeout=60,
    )

    if r.status_code != 200:
        error = r.json().get("error", {}).get("message", r.text[:300])
        raise RuntimeError(f"Request failed ({r.status_code}): {error}")

    return r.json()
```

The key building blocks from `litellm_client.py` are:
- **`get_headers()`** — Returns auth headers
- **`api_url(path)`** — Builds full URL from relative path
- **`resolve_model(alias)`** — Converts short alias to full model ID
- **`get_config()`** — Returns API key, base URL, default model

---

## Running Self-Tests

Each utility module has a built-in self-test:

```bash
cd /workspace/ninja

# Test configuration
python -m clients.litellm_client

# Test chat (fast, ~5s)
python -m utils.chat

# Test embeddings (fast, ~5s)
python -m utils.embeddings

# Test images (~30s, uses API credits)
python -m utils.images
```

---

## Quick Reference

### One-Liners

```python
# Chat
from utils.chat import chat; print(chat("Hello!"))

# Image
from utils.images import generate_image; generate_image("A sunset")

# Video
from utils.video import submit_video; print(submit_video("A bouncing ball"))

# Embedding
from utils.embeddings import embed; print(len(embed("Hello")))

# Similarity
from utils.embeddings import embed, cosine_similarity
print(cosine_similarity(embed("cat"), embed("dog")))
```

### Model Quick Reference

```
CHAT:   claude-opus | claude-sonnet | claude-haiku | gpt-5 | ninja-fast
IMAGE:  gpt-image (default, = gpt-image-2) | gpt-image-1.5 (legacy)
VIDEO:  (no alias — utils.video: submit, poll, download)
EMBED:  embed-small | embed-large
```
