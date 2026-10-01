// wa-inbound.ts — in-memory ring inbox + allowlist + echo loop prevention.
// Reference behavior (not source): openclaw/extensions/whatsapp/src/auto-reply/monitor/echo.ts

import pino from "pino";
import type { WASocket, proto } from "baileys";
import { proto as protoRuntime } from "baileys";
import {
  conversationIdForDm,
  conversationIdForGroup,
  isGroupJid,
  jidToPhone,
} from "./normalize-jid.js";
import type { BindStore } from "./wa-bind.js";
import type { PairingCode } from "./wa-pairing.js";
import {
  MediaStore,
  MediaTooLargeError,
  decryptAndPersist,
  extractMedia,
  extractQuoted,
  type MediaKind,
  type MediaRecord,
  type DecryptOpts,
} from "./wa-media.js";
import { extractInboundText } from "./wa-structured-text.js";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-inbound" });

export interface InboxRecord {
  seq: number;
  provider: "whatsapp";
  workspace_id: string | null; // self_e164 acts as workspace for POC
  user_id: string | null; // sender e164
  channel_id: string; // conversation_id
  thread_id: string | null;
  text: string;
  from_me: boolean;
  ts: number; // ms
  message_key: string | null; // `${remoteJid}:${messageId}` for dedupe
  participant: string | null; // sender JID inside a group (null for DMs); required for group reactions
  // Media fields — null for plain text-only messages.
  media_kind: MediaKind | null;
  media_id: string | null;        // null when decrypt failed (placeholder text used)
  media_mimetype: string | null;
  media_seconds: number | null;   // voice only
  media_bytes: number | null;
  media_filename: string | null;  // pdf only
  // Sender + quote context.
  sender_name: string | null;     // Baileys pushName; sender-controlled, display-only
  quoted_message_key: string | null;
  quoted_text: string | null;     // truncated at 1000 chars + ellipsis
  quoted_sender: string | null;
}

const INBOX_CAP = 1000;

// Baileys 7.x HistorySyncType: 0=INITIAL_BOOTSTRAP, 1=INITIAL_STATUS_V3,
// 2=FULL, 3=RECENT, 4=PUSH_NAME, 5=NON_BLOCKING_DATA, 6=ON_DEMAND.
// Accept every type that carries actual messages; skip metadata-only types
// (INITIAL_STATUS_V3, PUSH_NAME, NON_BLOCKING_DATA).
const HST = protoRuntime.HistorySync.HistorySyncType;
const ACCEPTED_HISTORY_SYNC_TYPES = new Set<number>([
  HST.INITIAL_BOOTSTRAP,
  HST.RECENT,
  HST.FULL,
  HST.ON_DEMAND,
]);

export function shouldIngestAppend(
  messageTimestampSec: number | undefined | null,
  nowMs: number,
  maxAgeMs: number,
): boolean {
  const sec = Number(messageTimestampSec ?? 0);
  if (!sec || sec <= 0) return false;
  const tsMs = sec * 1000;
  return nowMs - tsMs <= maxAgeMs;
}
const RECENTLY_SENT_CAP = 64;
const RECENTLY_SENT_TTL_MS = 60_000;

/** Durable mirror of the ring (MessageDb). Must not throw. */
export interface InboxSink {
  append(rec: InboxRecord): void;
  purge(): number;
}

export class Inbox {
  private buf: InboxRecord[] = [];
  private nextSeq = 1;

  constructor(private readonly sink: InboxSink | null = null) {}

  append(rec: Omit<InboxRecord, "seq">): InboxRecord {
    const full: InboxRecord = { ...rec, seq: this.nextSeq++ };
    this.buf.push(full);
    if (this.buf.length > INBOX_CAP) this.buf.splice(0, this.buf.length - INBOX_CAP);
    this.sink?.append(full);
    return full;
  }

  since(seq: number, limit: number): InboxRecord[] {
    const out: InboxRecord[] = [];
    for (const r of this.buf) {
      if (r.seq > seq) {
        out.push(r);
        if (out.length >= limit) break;
      }
    }
    return out;
  }

  latestSeq(): number {
    return this.nextSeq - 1;
  }

  flush(): number {
    const count = this.buf.length;
    this.buf = [];
    this.nextSeq = 1;
    this.sink?.purge();
    return count;
  }

  hasMessageKey(key: string): boolean {
    if (!key) return false;
    for (let i = this.buf.length - 1; i >= 0; i--) {
      if (this.buf[i]!.message_key === key) return true;
    }
    return false;
  }
}

interface RecentlySentEntry {
  text: string;
  to: string;
  ts: number;
}

interface RecentlySentIdEntry {
  message_id: string;
  to: string;
  ts: number;
}

export class RecentlySent {
  private ring: RecentlySentEntry[] = [];
  private ids: RecentlySentIdEntry[] = [];

  add(to: string, text: string): void {
    this.ring.push({ to, text, ts: Date.now() });
    if (this.ring.length > RECENTLY_SENT_CAP) this.ring.shift();
  }

  matches(to: string, text: string): boolean {
    const now = Date.now();
    for (let i = this.ring.length - 1; i >= 0; i--) {
      const e = this.ring[i]!;
      if (now - e.ts > RECENTLY_SENT_TTL_MS) continue;
      if (e.to === to && e.text === text) return true;
    }
    return false;
  }

  // Outbound media has empty / synthetic caption — text-based loopback
  // can't catch it. Index by WA message_id so the fromMe notify upsert
  // for our own send is recognised and dropped.
  addMessageId(to: string, message_id: string): void {
    this.ids.push({ to, message_id, ts: Date.now() });
    if (this.ids.length > RECENTLY_SENT_CAP) this.ids.shift();
  }

  matchesMessageId(to: string, message_id: string): boolean {
    const now = Date.now();
    for (let i = this.ids.length - 1; i >= 0; i--) {
      const e = this.ids[i]!;
      if (now - e.ts > RECENTLY_SENT_TTL_MS) continue;
      if (e.to === to && e.message_id === message_id) return true;
    }
    return false;
  }
}

export interface InboundConfig {
  echo: boolean;
  allowedTo: Set<string>; // e164 allowlist
  selfE164Getter: () => string | null;
}

export interface EventCounters {
  history_set: number;
  history_set_skipped: number;
  history_set_by_sync_type: Record<string, number>;
  upsert_notify: number;
  upsert_append: number;
  upsert_other: number;
  ingested_total: number;
  ingested_by_source: Record<"notify" | "append" | "history", number>;
  skipped_no_ts: number;
  skipped_stale: number;
  skipped_dup: number;
  // Ninja-mode counters (always present; stay 0 in standalone POC).
  dropped_not_bound_chat: number;
  pairing_bind_ok: number;
  // Reaction counters (POST /react).
  react_sent_ok: number;
  react_sent_err: number;
  dropped_react_not_bound_chat: number;
  // Media counters.
  media_ok_by_kind: Record<MediaKind, number>;
  media_decrypt_err: number;
  media_too_large: number;
  media_history_skip: number;
  media_skipped_total: Record<string, number>;  // sticker (and any future unsupported kinds)
  media_sent_ok: number;
  media_sent_err: number;
  dropped_send_media_not_bound_chat: number;
  // Bind-method flow counters.
  auto_group_create_ok: number;
  auto_group_create_err: number;
  bind_method_override_set: number;
  retry_bind_requested: number;
  invite_code_fetch_err: number;
}

export function makeEventCounters(): EventCounters {
  return {
    history_set: 0,
    history_set_skipped: 0,
    history_set_by_sync_type: {},
    upsert_notify: 0,
    upsert_append: 0,
    upsert_other: 0,
    ingested_total: 0,
    ingested_by_source: { notify: 0, append: 0, history: 0 },
    skipped_no_ts: 0,
    skipped_stale: 0,
    skipped_dup: 0,
    dropped_not_bound_chat: 0,
    pairing_bind_ok: 0,
    react_sent_ok: 0,
    react_sent_err: 0,
    dropped_react_not_bound_chat: 0,
    media_ok_by_kind: { voice: 0, image: 0, pdf: 0, archive: 0, text: 0, other: 0 },
    media_decrypt_err: 0,
    media_too_large: 0,
    media_history_skip: 0,
    media_skipped_total: {},
    media_sent_ok: 0,
    media_sent_err: 0,
    dropped_send_media_not_bound_chat: 0,
    auto_group_create_ok: 0,
    auto_group_create_err: 0,
    bind_method_override_set: 0,
    retry_bind_requested: 0,
    invite_code_fetch_err: 0,
  };
}

export interface InboundDeps {
  sock: () => WASocket | null;
  inbox: Inbox;
  recentlySent: RecentlySent;
  cfg: InboundConfig;
  historyMaxAgeMs: number;
  noteHistoryActivity?: () => void;
  counters: EventCounters;
  // Ninja-mode wiring (optional — when both are absent the gateway runs
  // in POC mode and inbound is unfiltered).
  bind?: BindStore;
  pairing?: PairingCode;
  /** Optional hook: fired after a successful bind from inbound (pairing-code). */
  onBound?: (via: "pairing_code") => void;
  // Media decrypt + persist. Absent in pure-POC mode → media is ignored.
  media?: {
    store: MediaStore;
    opts: DecryptOpts;
  };
}

type IngestSource = "notify" | "append" | "history";

function bumpSkipped(deps: InboundDeps, kind: string): void {
  deps.counters.media_skipped_total[kind] =
    (deps.counters.media_skipped_total[kind] ?? 0) + 1;
}

export function makeUpsertHandler(deps: InboundDeps) {
  return async (ev: { messages: proto.IWebMessageInfo[]; type: string }) => {
    const self = deps.cfg.selfE164Getter();
    let source: IngestSource;
    if (ev.type === "append") {
      source = "append";
      deps.counters.upsert_append += 1;
      if (deps.noteHistoryActivity) deps.noteHistoryActivity();
    } else if (ev.type === "notify") {
      source = "notify";
      deps.counters.upsert_notify += 1;
    } else {
      source = "notify"; // treat unknown as live; rare
      deps.counters.upsert_other += 1;
    }
    for (const m of ev.messages ?? []) {
      try {
        await handleOne(m, deps, self, source);
      } catch (e) {
        logger.error({ err: String(e) }, "inbound handler error");
      }
    }
  };
}

export function makeHistorySetHandler(deps: InboundDeps) {
  return async (h: {
    messages?: proto.IWebMessageInfo[];
    syncType?: number;
    isLatest?: boolean;
  }) => {
    const syncType = Number(h?.syncType ?? -1);
    const count = Array.isArray(h?.messages) ? h!.messages!.length : 0;
    deps.counters.history_set += 1;
    const key = String(syncType);
    deps.counters.history_set_by_sync_type[key] =
      (deps.counters.history_set_by_sync_type[key] ?? 0) + 1;
    logger.info({ event: "history_set_received", syncType, count }, "messaging-history.set");
    if (!ACCEPTED_HISTORY_SYNC_TYPES.has(syncType)) {
      deps.counters.history_set_skipped += 1;
      return;
    }
    if (deps.noteHistoryActivity) deps.noteHistoryActivity();
    const msgs = (h.messages ?? []).slice();
    // Sort ascending by messageTimestamp so the ring keeps newest after eviction.
    msgs.sort((a, b) => Number(a.messageTimestamp ?? 0) - Number(b.messageTimestamp ?? 0));
    const self = deps.cfg.selfE164Getter();
    for (const m of msgs) {
      try {
        await handleOne(m, deps, self, "history");
      } catch (e) {
        logger.error({ err: String(e) }, "history-set handler error");
      }
    }
  };
}

async function handleOne(
  m: proto.IWebMessageInfo,
  deps: InboundDeps,
  self: string | null,
  source: IngestSource,
): Promise<void> {
  const remoteJid = m.key?.remoteJid ?? "";
  if (!remoteJid) return;
  const fromMe = Boolean(m.key?.fromMe);

  const text = extractInboundText(m);

  // Ninja-mode: pairing-code → bind. Only consider live `notify` upserts.
  // We deliberately accept `fromMe` here — the natural operator workflow is
  // to link Ninja to their own number and then send the code from the same
  // phone into the chat they want bound. The pairing message itself is
  // consumed and never appended to the inbox.
  if (
    source === "notify" &&
    deps.pairing &&
    deps.bind &&
    !deps.bind.getChatJid() &&
    text &&
    deps.pairing.match(text)
  ) {
    try {
      await deps.bind.set(remoteJid, "pairing_code");
      deps.pairing.clear();
      deps.counters.pairing_bind_ok += 1;
      deps.onBound?.("pairing_code");
      logger.info(
        {
          event: "pairing_bind_ok",
          jid_type: isGroupJid(remoteJid) ? "group" : "dm",
        },
        "bound via pairing code",
      );
    } catch (e) {
      logger.error({ err: String(e) }, "pairing bind failed");
    }
    return;
  }

  // Ninja-mode hard filter: drop anything not from the bound chat.
  if (deps.bind) {
    const allowed = deps.bind.getChatJid();
    if (allowed && remoteJid !== allowed) {
      deps.counters.dropped_not_bound_chat += 1;
      return;
    }
  }

  // Age + timestamp filter for non-live sources.
  if (source !== "notify") {
    const tsSec = Number(m.messageTimestamp ?? 0);
    if (!tsSec || tsSec <= 0) {
      deps.counters.skipped_no_ts += 1;
      logger.info({ event: "history_append_skip_no_ts", source }, "skip");
      return;
    }
    if (!shouldIngestAppend(tsSec, Date.now(), deps.historyMaxAgeMs)) {
      deps.counters.skipped_stale += 1;
      logger.info({ event: "history_append_skip_stale", source }, "skip");
      return;
    }
  }

  // Detect supported media kinds before logging so we capture the kind
  // tag in the upsert log line.
  const mediaPayload = deps.media ? extractMedia(m) : null;

  // Body intentionally not logged — JID/type/length only.
  logger.info(
    {
      event: "upsert",
      source,
      jid_type: isGroupJid(remoteJid) ? "group" : "dm",
      from_me: fromMe,
      len: text.length,
      media: mediaPayload?.kind ?? null,
    },
    "inbound",
  );

  // Stickers remain unsupported; video/docs now extract as media_kind=other.
  if (!text && !mediaPayload && m.message?.stickerMessage) {
    bumpSkipped(deps, "sticker");
  }

  // Nothing actionable to ingest.
  if (!text && !mediaPayload) return;

  // Override #1: history/append media is dropped entirely. Live monitor
  // already cold-start-skips the backlog; ingesting placeholders here
  // would just confuse downstream dedupe with media_id=null records.
  if (!text && mediaPayload && source !== "notify") {
    deps.counters.media_history_skip += 1;
    logger.info({ event: "media_history_skip", kind: mediaPayload.kind, source }, "drop");
    return;
  }

  // Loopback prevention. Match by message_id first (catches caption-less
  // image/document sends where text is empty), then by text (preserves
  // existing text-reply behaviour). Runs on *any* source: Baileys
  // sometimes delivers our own outbound as `append` instead of `notify`,
  // so gating on notify-only let the bot re-ingest its own replies and —
  // with --include-from-me — react to and dispatch them.
  if (fromMe) {
    const mid = m.key?.id ?? "";
    if (mid && deps.recentlySent.matchesMessageId(remoteJid, mid)) {
      deps.counters.skipped_dup += 1;
      logger.info({ event: "loopback_skip", by: "id", source }, "skip own outbound");
      return;
    }
    if (text && deps.recentlySent.matches(remoteJid, text)) {
      deps.counters.skipped_dup += 1;
      logger.info({ event: "loopback_skip", by: "text", source }, "skip own outbound");
      return;
    }
  }

  // Dedupe by Baileys message_id across all sources. Baileys 7.x has been
  // observed re-delivering the same documentMessage notify multiple times.
  const messageId = m.key?.id ?? "";
  const messageKey = messageId ? `${remoteJid}:${messageId}` : "";
  if (messageKey && deps.inbox.hasMessageKey(messageKey)) {
    deps.counters.skipped_dup += 1;
    logger.info({ event: "dedupe_by_message_key", source }, "skip duplicate");
    return;
  }

  // Decrypt + persist media on notify only (history media was dropped above).
  let mediaRecord: MediaRecord | null = null;
  let placeholderText = "";
  if (mediaPayload && deps.media) {
    try {
      mediaRecord = await decryptAndPersist(m, mediaPayload, deps.media.opts);
      deps.media.store.register(mediaRecord);
      deps.counters.media_ok_by_kind[mediaPayload.kind] += 1;
    } catch (e) {
      if (e instanceof MediaTooLargeError) {
        deps.counters.media_too_large += 1;
        placeholderText = `[${mediaPayload.kind} too large]`;
      } else {
        deps.counters.media_decrypt_err += 1;
        placeholderText = `[${mediaPayload.kind} — decryption failed]`;
      }
      logger.warn(
        { event: "media_decrypt_fail", kind: mediaPayload.kind, err: String(e) },
        "media",
      );
    }
  }

  // Inbox `text`: real text wins; otherwise caption (image/pdf) becomes
  // the message text — matches Slack's `text + files[]` separation; then
  // the placeholder for failed decrypts.
  const captionAsText = mediaPayload?.caption ?? "";
  const bodyText = text || captionAsText;
  const effectiveText = placeholderText
    ? [bodyText, placeholderText].filter(Boolean).join(" ")
    : bodyText;

  const senderJid = isGroupJid(remoteJid) ? m.key?.participant ?? remoteJid : remoteJid;
  const senderE164 = jidToPhone(senderJid) ?? null;

  const channel_id = isGroupJid(remoteJid)
    ? self
      ? conversationIdForGroup(self, remoteJid)
      : remoteJid
    : self && senderE164
      ? conversationIdForDm(self, senderE164)
      : remoteJid;

  const quoted = extractQuoted(m);

  deps.inbox.append({
    provider: "whatsapp",
    workspace_id: self,
    user_id: fromMe ? self : senderE164,
    channel_id,
    thread_id: null,
    text: effectiveText,
    from_me: fromMe,
    ts: Number(m.messageTimestamp) ? Number(m.messageTimestamp) * 1000 : Date.now(),
    message_key: messageKey || null,
    participant: isGroupJid(remoteJid) ? (m.key?.participant ?? null) : null,
    media_kind: mediaPayload?.kind ?? null,
    media_id: mediaRecord?.media_id ?? null,
    media_mimetype: mediaRecord?.mimetype ?? mediaPayload?.mimetype ?? null,
    media_seconds: mediaPayload?.seconds ?? null,
    media_bytes: mediaRecord?.bytes ?? null,
    media_filename: mediaPayload?.filename ?? null,
    sender_name: m.pushName ?? null,
    quoted_message_key: quoted.quoted_message_key,
    quoted_text: quoted.quoted_text,
    quoted_sender: quoted.quoted_sender,
  });
  deps.counters.ingested_total += 1;
  deps.counters.ingested_by_source[source] += 1;

  if (source !== "notify") return; // never echo from history/append
  if (!deps.cfg.echo) return;
  if (fromMe) return; // primary fromMe guard
  if (deps.recentlySent.matches(remoteJid, text)) {
    logger.info({ event: "echo_skip", reason: "recently_sent" }, "skip echo");
    return;
  }
  if (!senderE164 || !deps.cfg.allowedTo.has(senderE164)) {
    logger.info({ event: "echo_skip", reason: "not_allowlisted" }, "skip echo");
    return;
  }

  const reply = `POC: ${text}`;
  const sock = deps.sock();
  if (!sock) return;
  try {
    deps.recentlySent.add(remoteJid, reply);
    await sock.sendMessage(remoteJid, { text: reply });
    logger.info({ event: "echo_sent" }, "echoed");
  } catch (e) {
    logger.error({ err: String(e) }, "echo send failed");
  }
}
