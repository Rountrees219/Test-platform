# Model Catalog

All models available through the NinjaTech LiteLLM gateway.

**Gateway URL**: `https://model-gateway.public.beta.myninja.ai`
**Auth**: Bearer token from `/root/.claude/settings.json`

> **Model ID note:** IDs like `claude-opus-5` are NinjaTech gateway identifiers routed through LiteLLM — they are not necessarily Anthropic public version strings. The gateway handles mapping to the underlying provider model. Prefer the short aliases (`claude-opus`, `claude-sonnet`, `claude-haiku`) for forward compatibility.
>
> **Runtime override:** The active model is read from `litellm_selected_model` in `/dev/shm/sandbox_metadata.json` at orchestrator startup. The model actually in use may differ from the catalog default.

---

## Chat / Text Models

These models accept chat completion requests via `/v1/chat/completions`.

| Alias | Full Model ID | Provider | Best For | Verified |
|-------|---------------|----------|----------|----------|
| `claude-opus` | `claude-opus-5` | Anthropic | **Default.** Complex reasoning, coding, long-horizon agents | ✅ |
| `claude-opus-5` | `claude-opus-5` | Anthropic | Explicit alias for the latest Opus | ✅ |
| `claude-sonnet` | `claude-sonnet-4-6` | Anthropic | Balanced quality/speed at ~40% of Opus cost | ✅ |
| `claude-sonnet-4-6` | `claude-sonnet-4-6` | Anthropic | Explicit alias | ✅ |
| `claude-haiku` | `claude-haiku-4-5-20251001` | Anthropic | Fast responses, simple tasks | ✅ |
| `gpt-5` | `openai/openai/gpt-5.5` | OpenAI | General purpose, strong coding; reasoning model | ✅ |
| `gpt-5.5` | `openai/openai/gpt-5.5` | OpenAI | Explicit alias for the current flagship | ✅ |
| `gpt-5.4` | `openai/openai/gpt-5.4` | OpenAI | Previous-generation GPT-5 (still available) | ✅ |
| `gpt-5.6-sol` | `openai/openai/gpt-5.6-sol` | OpenAI | GPT-5.6 Sol variant | ✅ |
| `ninja-fast` | `ninja-cline-fast` | NinjaTech | Quick agent tasks | ✅ |
| `ninja-standard` | `ninja-cline-standard` | NinjaTech | Standard agent tasks | ✅ |
| `ninja-complex` | `ninja-cline-complex` | NinjaTech | Complex agent tasks | ✅ |

### Choosing a Chat Model

- **Default for most production workflows** → `claude-opus` (= `claude-opus-5`)
- **Need speed / low cost?** → `claude-haiku` or `ninja-fast`
- **Balanced quality/price?** → `claude-sonnet` (~40% cheaper than Opus, same context window)
- **Agent tasks (Cline/autonomous workflows)?** → `ninja-complex`

---

## Image Generation Models

These models accept image generation requests via `/v1/images/generations` and image
edit / multi-reference composition requests via `/v1/images/edits`.

| Alias | Full Model ID | Provider | Verified |
|-------|---------------|----------|----------|
| `gpt-image` | `openai/openai/gpt-image-2.5-sunburst` | OpenAI | ✅ **Default** — latest, state-of-the-art |
| `gpt-image-2.5` | `openai/openai/gpt-image-2.5-sunburst` | OpenAI | ✅ Alias for the 2.5 Sunburst default |
| `gpt-image-2.5-sunburst` | `openai/openai/gpt-image-2.5-sunburst` | OpenAI | ✅ Explicit Sunburst variant |
| `gpt-image-2.5-flare` | `openai/openai/gpt-image-2.5-flare` | OpenAI | ✅ Flare variant |
| `gpt-image-2` | `alias/openai/gpt-image-2.0` | OpenAI | ✅ Previous generation (still available) |
| `gpt-image-1.5` | `openai/openai/gpt-image-1.5` | OpenAI | ✅ Legacy (kept for backward compatibility) |

### Choosing an Image Model

- **Default for new work** → `gpt-image` (= `gpt-image-2.5-sunburst`). Highest quality,
  supports text rendering, multi-reference composition, and flexible sizes.
- **Flare variant** → `gpt-image-2.5-flare`. Alternate 2.5 variant.
- **Previous generation** → `gpt-image-2`. Still available if needed.
- **Legacy workflows** → `gpt-image-1.5`. Keep during validation only.

### gpt-image-2.5 Capabilities

| Capability | Notes |
|---|---|
| Resolution | Any res up to 2K stable, 2K–4K experimental. Max edge < 3840, multiples of 16, ratio ≤ 3:1, 655K ≤ pixels ≤ 8.3M |
| Reference images | **Up to 16** in a single `/v1/images/edits` call |
| Text in images | Crisp, multilingual. Put literal strings in quotes or ALL CAPS. |
| `quality` | `low` / `medium` / `high`. Low is fast; high for dense text/infographics. |
| `input_fidelity` | **Not supported** (output is high-fidelity by default) |
| Output format | PNG URL (downloaded by the utility) |

### Popular `gpt-image-2.5` Sizes

| Label | Resolution | Notes |
|-------|------------|-------|
| Square | `1024x1024` | Good general-purpose default |
| HD portrait | `1024x1536` | Standard portrait |
| HD landscape | `1536x1024` | Standard landscape |
| 2K square | `2048x2048` | Experimental upper reliability boundary |
| Auto | `"auto"` | Let the model choose (returns ~1254x1254) |

### Image Prompting Fundamentals

1. **Structure**: `[Subject + adjectives] doing [Action] in [Scene/Context]. [Composition/Camera]. [Lighting/Atmosphere]. [Style/Medium]. [Exact Text]. [Aspect Ratio].`
2. **Reference indexing**: For multi-ref edits, name each input in the prompt — `"Image 1: <desc>... Image 2: <desc>..."` — and describe how they interact (`"apply Image 2's style to Image 1"`, `"place the cat from Image 2 on the chair from Image 1"`).
3. **Literal text**: Put exact in-image text in **quotes** or **ALL CAPS**. For tricky words, spell them out letter-by-letter.
4. **Preserve list on edits**: State invariants explicitly (`"keep the face, pose, background, and brand logo unchanged"`). Repeat the preserve list on every iteration to prevent drift.
5. **Iterate small**: A base prompt + small single-change follow-ups beats one giant rewrite.

### Gateway Behavior Notes

- Responses return a signed **URL** to the generated PNG (not base64 by default).
- When you attach many reference images (≥ 4–6), the gateway may **auto-downgrade `quality` to `low`** to stay within capacity. This is harmless for drafts; for production, request fewer refs and retry with explicit `quality="medium"` or `"high"`.
- `output_format` parameter is accepted but currently **always returns PNG** regardless of value.
- `background="transparent"` is accepted but does not consistently produce true alpha channels — verify after generation.

---

## Video Generation Models

Video does **not** go through `/v1/videos` or a model alias. It is served by the
gateway's video MCP server, in three steps — you start the render, poll for it,
then download it:

```python
from utils.video import submit_video, check_video, download_video

video_id = submit_video("A red balloon over a green field", seconds=5)
# ...poll check_video(video_id) until it returns a video_url...
path = download_video(url, "balloon.mp4")
```

| Model | Provider | Speed | Verified |
|-------|----------|-------|----------|
| ByteDance Seedance 2.5 | Together AI (via MCP) | a few min | ✅ |

### Video Parameters

- **Duration**: 4-12 seconds. Anything outside that is rejected.
- **Resolution**: always 1280x720 landscape — it cannot be changed.
- **Generation time**: a few minutes — barely affected by clip length.
- **Output format**: MP4

Every attempt is charged, including one the content filter refuses, so never
retry in a loop. See LITELLM_GUIDE.md for the details.

---

## Audio Models

| Alias | Full Model ID | Provider | Best For | Verified |
|-------|---------------|----------|----------|----------|
| `ninja-transcribe` | `openai/openai/gpt-transcribe` | OpenAI | Audio transcription | ✅ |
| `ninja-tts` | `eleven-v3` | ElevenLabs | Text-to-speech | ✅ |

---

## Embedding Models

These models accept embedding requests via `/v1/embeddings`.

| Alias | Full Model ID | Provider | Dimensions | Verified |
|-------|---------------|----------|------------|----------|
| `embed-small` | `openai/openai/text-embedding-3-small` | OpenAI | 1,536 | ✅ |
| `embed-large` | `openai/openai/text-embedding-3-large` | OpenAI | 3,072 | ✅ |

### Choosing an Embedding Model

- **`embed-small`** — Good for most use cases, lower cost, 1536 dimensions
- **`embed-large`** — Higher accuracy, better for semantic search, 3072 dimensions

### Use Cases

- Semantic search and retrieval
- Document similarity comparison
- Clustering and classification
- RAG (Retrieval-Augmented Generation)

---

## Model Aliases

The utility library supports short aliases. Use `resolve_model()` to convert:

```python
from clients.litellm_client import resolve_model

resolve_model("claude-opus")    # → "claude-opus-5"
resolve_model("claude-sonnet")  # → "claude-sonnet-4-6"
resolve_model("gpt-5")          # → "openai/openai/gpt-5.5"
resolve_model("gpt-image")      # → "openai/openai/gpt-image-2.5-sunburst"
resolve_model("embed-small")    # → "openai/openai/text-embedding-3-small"

# Full IDs are passed through unchanged
resolve_model("claude-opus-5")  # → "claude-opus-5"
```

---

## Rate Limits & Best Practices

1. **Retry on transient errors** — Gateway may return 500 for temporary issues
2. **Use appropriate models** — Don't use `claude-opus` for simple tasks
3. **Batch embeddings** — Use `embed_batch()` instead of multiple `embed()` calls
4. **Video polling** — Use 5-second intervals, don't poll too aggressively
5. **Image retries** — If `gpt-image` returns a transient error, retry the same call (up to 2x)
