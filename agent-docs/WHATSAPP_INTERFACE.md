# WhatsApp Interface CLI

A command-line tool and Python API for interacting with a **single bound**
WhatsApp chat via the local Baileys gateway (`127.0.0.1:8090`).

## Features

- 🔑 **Gateway auth** — bearer token from `WHATSAPP_GATEWAY_TOKEN` or `~/.agent_settings.json`
- 💬 **Single-chat binding** — Ninja listens and replies in one group or DM only
- 📥 **Cursor-based read** — `read` advances `whatsapp.last_read_seq` in settings
- 📤 **DM and group send** — `say` with `--to` or `--group-jid`
- 🎤 **Media** — inbound voice/image/pdf via `fetch-media`; outbound via `upload`
- 😀 **Reactions** — `react` on inbox `message_key` values
- 👥 **Groups** — `group list` / `group create` (manual; auto-bind is operator setup)
- 🐍 **Python API** — `WhatsAppInterface` for factory/health checks

## Installation

The tool is included in this repository. The gateway runs as
`ninja-whatsapp-gateway.service` (installed by `infra/install.sh --messaging-channel whatsapp`).

### Dependencies

```bash
pip install requests
```

The gateway is Node/Baileys (`messaging/whatsapp/gateway/`). The Python CLI talks
to it over HTTP only.

## Quick Start

### 1. Confirm binding

Ninja must be linked to exactly one chat. Check status:

```bash
python messaging/whatsapp/interface.py bind --status --json
```

The bound JID (`bound_chat_jid`) is what you use for `--group-jid` (groups) or
`--to` (DMs). Pairing and QR are operator setup (install + dashboard `/whatsapp`);
agents only need `bind --status`.

### 2. Read messages

```bash
# New messages since last cursor (persists cursor in ~/.agent_settings.json)
python messaging/whatsapp/interface.py read --json --limit 20

# Re-read from the start without advancing cursor
python messaging/whatsapp/interface.py read --since 0 --no-save --json
```

### 3. Send messages

```bash
# Reply in the bound group
python messaging/whatsapp/interface.py say "Hello team!" --group-jid 120363xxxxxxxxxx@g.us

# Reply in a DM
python messaging/whatsapp/interface.py say "Hello!" --to 15551112222@s.whatsapp.net

# Monitor adds the visible prefix; manual CLI sends literal text unless:
python messaging/whatsapp/interface.py say "ack" --ninja-prefix --group-jid 120363...@g.us
```

## Configuration

### Settings file

WhatsApp state lives under the `whatsapp` key of `~/.agent_settings.json`:

```json
{
  "mode": "whatsapp",
  "whatsapp": {
    "gateway_url": "http://127.0.0.1:8090",
    "gateway_token": "<secret>",
    "bound_chat_jid": "120363xxxxxxxxxx@g.us",
    "channel_label": "Ops Bridge",
    "last_read_seq": 42,
    "last_read_inbox_epoch": 3
  }
}
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `WHATSAPP_GATEWAY_URL` | Gateway base URL (default `http://127.0.0.1:8090`) |
| `WHATSAPP_GATEWAY_TOKEN` | Bearer token for all gateway HTTP calls |
| `MESSAGING_CHANNEL` | Must be `whatsapp` for the WhatsApp monitor |

CLI flags `--gateway-url` and `--gateway-token` override env/settings for one shot.

## Commands

### `read`

```bash
python messaging/whatsapp/interface.py read [--since N] [--limit N] [--json] [--no-save] [--no-self]
```

Returns inbox items from `GET /messages`. Each item includes `seq`, `text`,
`channel_id`, `from_me`, and optional `media_kind` / `media_id` for attachments.

### `say`

```bash
python messaging/whatsapp/interface.py say "message" [--to E164] [--group-jid JID] [--group last] [--ninja-prefix]
```

Exactly one destination is required. Use the bound group JID from `bind --status`.

### `bind` / `unbind`

```bash
python messaging/whatsapp/interface.py bind --status [--json]
python messaging/whatsapp/interface.py bind --chat-jid 120363...@g.us
python messaging/whatsapp/interface.py unbind
```

### `fetch-media` / `upload`

```bash
python messaging/whatsapp/interface.py fetch-media --media-id <id> --out /tmp/wa_<id>
python messaging/whatsapp/interface.py upload --kind image --file /path.png --caption "caption" \
    --ninja-prefix --group-jid 120363...@g.us
```

Inbound media kinds: `voice`, `image`, `pdf`. Fetch bytes before transcription or
vision. Never paste gateway tokens into messages or prompts.

### `react`

```bash
python messaging/whatsapp/interface.py react --message-key "<key from read --json>" --emoji "👀"
```

### `group`

```bash
python messaging/whatsapp/interface.py group list [--json]
python messaging/whatsapp/interface.py group create "Subject" --participants 1555... 1555...
```

## Media workflow

When `read --json` returns `media_kind` and `media_id`:

1. `fetch-media` to a local file (gateway auth from env/settings).
2. Voice → `python messaging/whatsapp/transcribe.py` (same Whisper path as WhatsApp).
3. Image/PDF → vision/document blocks per `agent-docs/AGENT_PROTOCOL.md` §5a.
4. Reply with `python messaging/whatsapp/interface.py say` or `python messaging/whatsapp/interface.py upload`.

## Python API

```python
from messaging.whatsapp.interface import WhatsAppInterface

wa = WhatsAppInterface()
wa.say("Hello!", channel="120363...@g.us")
history = wa.get_history(limit=20)
health = wa.check_messaging_health()
```

Monitor dispatch uses `services/monitor_whatsapp_service.py`, not
`processes/monitor.py` collect_pending.

## Authentication

All gateway endpoints except `GET /health` require:

```
Authorization: Bearer <WHATSAPP_GATEWAY_TOKEN>
```

The gateway binds to loopback only. Tokens are resolved from env or
`~/.agent_settings.json` — do not echo them in WhatsApp messages.

## Single-chat rule

After bind, only messages from `bound_chat_jid` are delivered to the monitor.
Replies must target that JID (group `@g.us` or DM `@s.whatsapp.net`).

## Safety

Linked-device automation on a personal WhatsApp number can trigger rate limits.
Keep volume low; use a dedicated test number when possible. Every monitor reply
is prefixed with `🥷 Ninja: ` so participants see an automated agent.
