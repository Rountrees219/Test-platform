# Agent Communication Protocol

## Overview

This document defines the communication standards and protocols for agent interaction within the team WhatsApp channel using the `messaging/whatsapp/interface.py` CLI tool.

## 🚨 CRITICAL: Workflow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     TASK EXECUTION PROTOCOL                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   1. Agent receives task via WhatsApp or --task flag                        │
│   2. Agent reads spec: cat agent-docs/NINJA_SPEC.md                   │
│   3. Agent executes task using browser toolkit                           │
│   4. Agent reports results back to WhatsApp                                 │
│                                                                          │
│   WAKE UP INSTRUCTION                                                    │
│   ═══════════════════                                                    │
│   When agent receives "WAKE UP" → Run: systemctl start ninja.service    │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## WhatsApp Interface Tool

All agents communicate via the `messaging/whatsapp/interface.py` CLI tool. See [WHATSAPP_INTERFACE.md](WHATSAPP_INTERFACE.md) for complete documentation.

### Quick Reference

```bash
# Configure your agent identity (do this first!)
python messaging/whatsapp/interface.py config --set-agent <your-agent-name>
python messaging/whatsapp/interface.py config --set-channel "#your-channel"

# Read messages from the channel
python messaging/whatsapp/interface.py read              # Last 50 messages
python messaging/whatsapp/interface.py read -l 100       # Last 100 messages

# Send messages as configured agent
python messaging/whatsapp/interface.py say "Your message here"

# Upload files (uploads with agent impersonation)
python messaging/whatsapp/interface.py upload design.png --title "Design Mockup"
```

## Channel Structure

### Primary Channel

- **Name**: Your configured default channel
- **Purpose**: All agent and human communication
- **Visibility**: All agents + human team members

### Thread Usage

- **Always reply in threads** when responding to questions or requests — never as a new top-level message
- Main channel for session update posts and critical announcements only
- Threads for technical discussions, reviews, debugging, and all replies

### Session Update Protocol (Threading)

Session updates follow a **single-thread pattern**:

1. **The first agent to post** creates a top-level message: `"Session N Update 🧵"`
2. **All other agents reply under that thread** with their individual updates
3. **Never create separate top-level posts** for individual updates

**Step 1 — First agent posts the session header:**

```bash
# Post top-level session update message
python messaging/whatsapp/interface.py say "Session 5 Update 🧵"
```

**Step 2 — Same agent immediately replies in the thread with their update:**

```bash
# Reply to the thread with your status (use the timestamp from step 1)
python messaging/whatsapp/interface.py say "✅ Completed homepage mockup, pushed to designs/ folder
🔄 Starting mobile responsive variants
🚧 No blockers" -t <thread_timestamp>
```

**Step 3 — All other agents reply in the same thread:**

```bash
# Other agents find the "Session N Update 🧵" thread and reply under it
python messaging/whatsapp/interface.py say "✅ Implemented API endpoints for user auth
🔄 Working on frontend integration
🚧 Waiting on design specs for settings page" -t <thread_timestamp>
```

> **Key rule:** There should be exactly ONE "Session N Update" top-level post per session. Every agent posts their update as a reply under it.

### Message Length

- **Keep all WhatsApp messages SHORT** — 2-4 sentences max
- No walls of text. Be direct and concise
- If detail is needed, put it in a thread reply or link to a GitHub issue/PR

## Agent Identities

The agent is configured in `agents_config.py` with a name, role, emoji, and custom avatar.

Stakeholders are human team members who provide direction, approve work, and can override agent decisions.

## Message Formats

### Project Initialization Messages

#### Task Acknowledgment

```bash
python messaging/whatsapp/interface.py say "**Task Received**

I've received the browser automation task. Here's my plan:

1. Connect to the persistent browser
2. Navigate to the target page
3. Execute the required actions
4. Report results with screenshots

Starting now!"
```

#### Share Live Browser View

```bash
# Always share the VNC link so stakeholders can watch the browser live.
# Use ninja/vnc.py to generate the auto-connect URL (no password needed).
python messaging/whatsapp/interface.py say "**🖥️ Live Browser View**

Watch the browser automation in real-time:
$(python -c 'from browser.vnc import get_vnc_url; print(get_vnc_url())')

Click the link above to view the browser session live — no install needed."
```

> **NOTE:** Always post the VNC link at the start of a task so stakeholders
> can observe the browser in real-time. This is especially important for
> long-running tasks, debugging, or when the agent encounters CAPTCHAs
> or other issues requiring human intervention.

#### Task Completion

```bash
python messaging/whatsapp/interface.py say "**Task Complete**

Browser automation task finished. Results:
- Screenshots attached
- Data extracted and saved

Let me know if you need anything else!"
```

### Sync / Session Update Messages

#### Session Update — Thread Starter

```bash
# The FIRST agent to update posts this as a top-level message
python messaging/whatsapp/interface.py say "Session 3 Update 🧵"
```

#### Session Update — Agent Reply (in thread)

```bash
# ALL agents reply in the thread using -t <timestamp>
python messaging/whatsapp/interface.py say "✅ Completed: [what you finished]
🔄 In Progress: [current work]
🚧 Blockers: [any blockers, or None]" -t <session_thread_ts>
```

### Work Phase Messages

#### Asking for Help (reply in relevant thread)

```bash
python messaging/whatsapp/interface.py say "Quick question about [topic]: [details]" -t <message_id>
```

#### Sharing Work

```bash
python messaging/whatsapp/interface.py say "**[Work Type] Update**

[Brief description]

📎 GitHub: [link to PR/issue/commit]
📎 WhatsApp: File uploaded in thread below

@[relevant_agent] — ready for review"
```

#### Reporting Blockers

```bash
python messaging/whatsapp/interface.py say "🚨 **Blocker**

Blocked on [task]:
- **Issue**: [Description]
- **Need**: [What's needed to unblock]
- **Impact**: [What's affected]"
```

### End of Cycle Messages

#### Work Summary (reply in session thread)

```bash
python messaging/whatsapp/interface.py say "📊 **Cycle Summary**

- [Accomplishment 1]
- [Accomplishment 2]

🔜 Next: [Planned work]" -t <session_thread_ts>
```

## Communication Rules

### 1. Thread Etiquette

- **Reply in threads** — all responses go in threads, not as new top-level messages
- Keep main channel clean — only session updates and critical announcements as top-level posts
- Session updates: ONE top-level post per session, all agents reply in that thread
- Never duplicate updates — find the existing session thread and reply there

### 2. Mention Protocol

- Mention relevant agents when their input is needed
- Report escalations and blockers in WhatsApp
- Use `@channel` sparingly (emergencies only)

### 3. Response Expectations

- During sync: Respond within the sync window
- During work phase: Respond when relevant to current task
- Blockers: Respond as soon as possible

### 4. File Sharing Protocol

**When sharing files, do BOTH:**

1. **Commit to the repo** — all files must be version-controlled
   - Designs → `designs/` folder
   - Code → appropriate source folder
   - Documents → `docs/` or `agent-docs/` folder
   - Test Reports → `reports/` folder

2. **Upload to WhatsApp** — so the team can view files immediately

   ```bash
   # Upload file to WhatsApp (uses agent impersonation)
   python messaging/whatsapp/interface.py upload path/to/file.png --title "Design Mockup v2"

   # Upload to a specific thread
   python messaging/whatsapp/interface.py upload report.pdf --title "Test Report" -t <message_id>
   ```

3. **Post the GitHub link** — reference where the file lives in the repo
   ```bash
   python messaging/whatsapp/interface.py say "📎 Design mockup committed: [GitHub link]
   File also uploaded in thread above for quick preview"
   ```

> **Key rule:** Files should be accessible both in WhatsApp (for quick viewing) AND in the repo (for version control). Always do both.

### 5. Audio / Voice Message Protocol

WhatsApp users (and other agents) may send **audio messages** or **voice clips** in channels and threads.

**When you encounter an audio/voice message in WhatsApp:**

1. **Detect the audio attachment** — check the message for audio/voice file attachments
2. **Transcribe using the channel-specific transcriber**:

   ```bash
   python messaging/whatsapp/transcribe.py
   ```

3. **Process the transcript** — treat the transcribed text as if it were a regular text message and respond accordingly
4. **Acknowledge the voice message** — when replying, mention that you received and transcribed the voice message:
   ```bash
   python messaging/whatsapp/interface.py say "🎤 I listened to your voice message. Here's my response: ..." -t <message_id>
   ```

> **Key rule:** Never ignore audio/voice messages. Always transcribe them using `python messaging/whatsapp/transcribe.py` and respond to their content just like any text message. If transcription fails, acknowledge the voice message and ask the sender to provide a text version.

### 5a. WhatsApp Media (Ninja mode)

When running under `MESSAGING_CHANNEL=whatsapp`, voice / image / pdf flow through
the gateway instead of Slack — but the agent-side transcription / vision /
document handling is identical to Slack:

| Source | Inbox surface | Bytes endpoint | Send back |
|--------|----------------|----------------|-----------|
| Slack  | `files[].url_private_download` (bot token) | Slack file API | `slack_interface upload` |
| WhatsApp | `media_kind` + `media_id` on the inbox record | `GET /media/:id` (gateway bearer token) | `python messaging/whatsapp/interface.py upload --kind …` |

The WhatsApp monitor prompt includes a `fetch-media` command per inbound
media message. The gateway URL and bearer token are resolved from
`WHATSAPP_GATEWAY_URL` / `WHATSAPP_GATEWAY_TOKEN` (or the `whatsapp`
sub-object in `~/.agent_settings.json`) — **never paste the token into the
chat or the prompt body**.

```bash
# Voice: fetch and feed to LiteLLM Whisper (same model + endpoint as Slack).
python messaging/whatsapp/interface.py fetch-media --media-id <id> --out /tmp/v.ogg
# … POST /v1/audio/transcriptions, model=whisper-1, files={"file": …}

# Image: fetch and send as input_image content block to a vision-capable Claude model.
# PDF:   fetch and send as document content block to Claude.
```

To reply with media, use the `upload` verb with the same `--ninja-prefix`
behaviour as `say` — when no caption is supplied it synthesizes
`🥷 Ninja: shared a/an <kind>` so the human still sees a bot tag.

## Interaction Patterns

### Stakeholder → Agent

```
Direction Flow:
Stakeholder ──task──▶ Ninja (via WhatsApp or --task flag)
Ninja ──results──▶ Stakeholder (via WhatsApp)
```

### Stakeholders → Agents

```
Stakeholders can:
- Provide direction to any agent
- Override agent decisions
- Approve/reject work
- Add context and requirements
- All agents take orders from stakeholders
```

## GitHub Integration Protocol

### Issue References

```
When referencing GitHub issues in WhatsApp:
"Working on #42 - [Issue Title]"
```

### PR Notifications

```bash
python messaging/whatsapp/interface.py say "🔀 PR Ready: [Title] - [GitHub Link]
Ready for review"
```

### Code Review Comments

```bash
python messaging/whatsapp/interface.py say "📝 Review feedback on PR #[number]:
- [Comment 1]
- [Comment 2]
Please address these"
```

## Error Handling

### Agent Failure

```
If an agent fails to respond during sync:
1. Agent notes the absence
2. Work continues with available agents
3. Failed agent catches up next cycle via WhatsApp history and open issues
```

### Integration Failure

```
If WhatsApp is unavailable:
1. Agent logs the failure
2. Retries with exponential backoff
3. Stores pending messages for later delivery
```

## Escalation to Stakeholders

### When to Escalate

- Conflicting requirements
- Technical decisions with major impact
- Blockers that can't be resolved by agents
- Approval needed for significant changes

### Escalation Format

```bash
python messaging/whatsapp/interface.py say "👤 **Stakeholder Input Needed**

We need your input on:
- **Topic**: [Description]
- **Options**:
  1. [Option A]
  2. [Option B]
- **Recommendation**: [Agent's suggestion]
- **Deadline**: [When decision is needed]"
```

## Tavily Web Research Tools

All agents have access to **Tavily** — a web research toolkit available via the LiteLLM gateway's MCP endpoint. Tavily provides 5 tools for web search, content extraction, crawling, site mapping, and deep research.

### Quick Reference

```python
from clients.tavily_client import Tavily

tavily = Tavily()  # Reads credentials from settings.json automatically

# Search the web
results = tavily.search("query", max_results=10)

# Extract full content from URLs
pages = tavily.extract(["https://example.com/page"])

# Crawl a website (follow links)
site = tavily.crawl("https://docs.example.com", max_depth=2, limit=20)

# Map a website's URL structure
urls = tavily.map("https://docs.example.com", limit=50)

# Deep multi-source research report
report = tavily.research("Research topic description")
```

### Tool Capabilities

| Tool         | What It Does                            | Speed   | Best For                                 |
| ------------ | --------------------------------------- | ------- | ---------------------------------------- |
| **search**   | Web search with structured results      | ~1s     | Quick lookups, news, finding URLs        |
| **extract**  | Extract full content from specific URLs | ~2-5s   | Reading docs, articles, specs            |
| **crawl**    | Crawl a site following links            | ~5-15s  | Documentation, comprehensive analysis    |
| **map**      | Discover URL structure of a site        | ~2-5s   | Finding the right page before extracting |
| **research** | Multi-source deep research report       | ~30-60s | Complex topics, comparisons, analysis    |

### Search Parameters

| Parameter             | Values                                 | Description                     |
| --------------------- | -------------------------------------- | ------------------------------- |
| `search_depth`        | `"basic"`, `"advanced"`                | Depth of search                 |
| `topic`               | `"general"`, `"news"`, `"finance"`     | Search category                 |
| `time_range`          | `"day"`, `"week"`, `"month"`, `"year"` | Recency filter                  |
| `include_raw_content` | `True`/`False`                         | Include cleaned HTML per result |
| `include_domains`     | `["site.com"]`                         | Whitelist domains               |
| `exclude_domains`     | `["site.com"]`                         | Blacklist domains               |

### Credentials

Tavily reads from `settings.json` (the same file used by `claude-wrapper.sh`) via the `clients.litellm_client` module. **No manual API key setup needed.**

### When to Use Tavily vs Internet Search

| Scenario                                | Use                 |
| --------------------------------------- | ------------------- |
| Need structured results with metadata   | **Tavily search**   |
| Need full page content from a known URL | **Tavily extract**  |
| Need to crawl an entire docs site       | **Tavily crawl**    |
| Quick fact lookup                       | Either works        |
| Need a comprehensive research report    | **Tavily research** |

---

## AI Models & Utility Library

All agents have access to AI models through the NinjaTech LiteLLM gateway. A ready-to-use Python utility library is available in `utils/`.

### Key Resources

| Document                                     | Purpose                                                                     |
| -------------------------------------------- | --------------------------------------------------------------------------- |
| [MODELS.md](MODELS.md)                       | Complete model catalog — aliases, capabilities, parameters, sizes           |
| [LITELLM_GUIDE.md](LITELLM_GUIDE.md)         | Usage guide — code examples, error handling, building custom utilities      |
| [PIPEDREAM_CONNECT.md](PIPEDREAM_CONNECT.md) | Connected app integrations — OAuth dashboard, `tools/pdx.py`, and `pdx` CLI |

### Quick Import Reference

```python
from utils.chat import chat, chat_json, chat_stream     # Text generation
from utils.images import generate_image, generate_images, edit_image  # Image generation + multi-reference editing
from utils.video import submit_video, check_video, download_video  # Video generation
from utils.embeddings import embed, cosine_similarity      # Embeddings
from clients.litellm_client import resolve_model, get_config # Config & model aliases
from clients.tavily_client import Tavily                   # Web research
```

### Model Recommendations

| Task              | Recommended Model | Notes                                          |
| ----------------- | ----------------- | ---------------------------------------------- |
| Complex reasoning | `claude-opus`     | Highest quality                                |
| General tasks     | `claude-sonnet`   | Best balance of quality/speed                  |
| Quick responses   | `claude-haiku`    | Fastest                                        |
| Image generation  | `gpt-image`       | Default — gpt-image-2.5-sunburst, state-of-the-art |
| Video generation  | *(no alias)*      | `utils.video` — submit, poll, download. A few min |
| Embeddings        | `embed-small`     | 1536 dimensions                                |
| Web research      | **Tavily**        | 5 tools: search, extract, crawl, map, research |

> **Image generation:** Always use `gpt-image` (resolves to `gpt-image-2.5-sunburst`) as the default. It supports text rendering, multi-reference compositing, and flexible sizes up to 2K. Variants: `gpt-image-2.5-flare` for the Flare model, `gpt-image-2` for the previous generation.

---

## Running the Orchestrator

The orchestrator runs as a systemd service managed by the system:

```bash
systemctl start ninja.service     # start one work cycle
systemctl status ninja.service    # check current state
journalctl -u ninja.service -f    # follow logs
```

`ninja.service` is a single work cycle — systemd restarts it after each completion. Use `systemctl start` to trigger a cycle; do not run `python processes/orchestrator.py` directly in production.

This starts:

- **Work process**: Executes the current task using browser toolkit
- **Monitor process**: Watches for new WhatsApp mentions

For one-off operator tasks from a terminal:

```bash
python processes/orchestrator.py --task "your task description here"
```
