// wa-db.ts — durable SQLite mirror of the in-memory inbox ring, served by
// /db/messages. Uses built-in `node:sqlite` (Node >= 22.5) to avoid a node-gyp
// build; on older runtimes `openMessageDb` returns null.

import pino from "pino";
import { chmod, mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import type { DatabaseSync, SQLInputValue, SQLOutputValue } from "node:sqlite";
import type { InboxRecord } from "./wa-inbound.js";
import type { MediaKind } from "./wa-media.js";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-db" });

// Pruning scans the table; amortise it.
const PRUNE_EVERY_APPENDS = 200;
const DEFAULT_RETENTION_MS = 14 * 24 * 60 * 60 * 1000;
const DEFAULT_MAX_ROWS = 50_000;

// UNIQUE message_key drops redeliveries across restarts; NULL keys stay distinct.
const SCHEMA = `
CREATE TABLE IF NOT EXISTS messages (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  epoch              INTEGER NOT NULL,
  seq                INTEGER NOT NULL,
  provider           TEXT    NOT NULL,
  workspace_id       TEXT,
  user_id            TEXT,
  channel_id         TEXT    NOT NULL,
  thread_id          TEXT,
  text               TEXT    NOT NULL,
  from_me            INTEGER NOT NULL,
  ts                 INTEGER NOT NULL,
  message_key        TEXT    UNIQUE,
  participant        TEXT,
  media_kind         TEXT,
  media_id           TEXT,
  media_mimetype     TEXT,
  media_seconds      INTEGER,
  media_bytes        INTEGER,
  media_filename     TEXT,
  sender_name        TEXT,
  quoted_message_key TEXT,
  quoted_text        TEXT,
  quoted_sender      TEXT,
  inserted_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_epoch_seq ON messages (epoch, seq);
CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages (channel_id, ts);
`;

const INSERT_SQL = `
INSERT OR IGNORE INTO messages (
  epoch, seq, provider, workspace_id, user_id, channel_id, thread_id, text,
  from_me, ts, message_key, participant, media_kind, media_id, media_mimetype,
  media_seconds, media_bytes, media_filename, sender_name, quoted_message_key,
  quoted_text, quoted_sender, inserted_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
`;

// Exactly InboxRecord, so items match /messages byte for byte.
const SELECT_COLUMNS = `
  seq, provider, workspace_id, user_id, channel_id, thread_id, text, from_me,
  ts, message_key, participant, media_kind, media_id, media_mimetype,
  media_seconds, media_bytes, media_filename, sender_name, quoted_message_key,
  quoted_text, quoted_sender
`;

export interface MessageDbOptions {
  path: string;
  /** Debugging only. Never filter on it: it changes on every reconnect. */
  epoch: () => number;
  retentionMs?: number;
  maxRows?: number;
}

export interface MessageQuery {
  limit: number;
  channelId?: string | null;
  startTs?: number | null;
  endTs?: number | null;
}

export interface MessageDbStats {
  rows: number;
  appended: number;
  errors: number;
}

function asText(v: SQLOutputValue): string | null {
  return v === null || v === undefined ? null : String(v);
}

function asNumber(v: SQLOutputValue): number | null {
  return v === null || v === undefined ? null : Number(v);
}

function toRecord(row: Record<string, SQLOutputValue>): InboxRecord {
  return {
    seq: Number(row.seq),
    provider: "whatsapp",
    workspace_id: asText(row.workspace_id),
    user_id: asText(row.user_id),
    channel_id: String(row.channel_id),
    thread_id: asText(row.thread_id),
    text: String(row.text ?? ""),
    from_me: Number(row.from_me) === 1,
    ts: Number(row.ts),
    message_key: asText(row.message_key),
    participant: asText(row.participant),
    media_kind: asText(row.media_kind) as MediaKind | null,
    media_id: asText(row.media_id),
    media_mimetype: asText(row.media_mimetype),
    media_seconds: asNumber(row.media_seconds),
    media_bytes: asNumber(row.media_bytes),
    media_filename: asText(row.media_filename),
    sender_name: asText(row.sender_name),
    quoted_message_key: asText(row.quoted_message_key),
    quoted_text: asText(row.quoted_text),
    quoted_sender: asText(row.quoted_sender),
  };
}

/** Every method swallows its errors: a cache fault must never stop ingestion. */
export class MessageDb {
  private appended = 0;
  private errors = 0;
  private sinceLastPrune = 0;

  constructor(
    private readonly db: DatabaseSync,
    private readonly opts: MessageDbOptions,
  ) {}

  append(rec: InboxRecord): void {
    try {
      const params: SQLInputValue[] = [
        this.opts.epoch(),
        rec.seq,
        rec.provider,
        rec.workspace_id,
        rec.user_id,
        rec.channel_id,
        rec.thread_id,
        rec.text,
        rec.from_me ? 1 : 0,
        rec.ts,
        rec.message_key,
        rec.participant,
        rec.media_kind,
        rec.media_id,
        rec.media_mimetype,
        rec.media_seconds,
        rec.media_bytes,
        rec.media_filename,
        rec.sender_name,
        rec.quoted_message_key,
        rec.quoted_text,
        rec.quoted_sender,
        Date.now(),
      ];
      this.db.prepare(INSERT_SQL).run(...params);
      this.appended += 1;
      if (++this.sinceLastPrune >= PRUNE_EVERY_APPENDS) this.prune();
    } catch (e) {
      this.errors += 1;
      logger.warn({ err: String(e), event: "db_append_failed" }, "message cache write failed");
    }
  }

  /**
   * Newest `limit` records, oldest-first. No cursor: `seq` restarts per process.
   * Ordered by `id`, not `ts` — history sync delivers old timestamps late.
   */
  query(q: MessageQuery): InboxRecord[] {
    try {
      const where = ["1 = 1"];
      const params: SQLInputValue[] = [];
      if (q.channelId) {
        where.push("channel_id = ?");
        params.push(q.channelId);
      }
      if (q.startTs) {
        where.push("ts >= ?");
        params.push(q.startTs);
      }
      if (q.endTs) {
        where.push("ts <= ?");
        params.push(q.endTs);
      }
      params.push(Math.max(1, q.limit));
      const sql =
        `SELECT ${SELECT_COLUMNS} FROM messages WHERE ${where.join(" AND ")} ` +
        "ORDER BY id DESC LIMIT ?";
      return this.db
        .prepare(sql)
        .all(...params)
        .map(toRecord)
        .reverse();
    } catch (e) {
      this.errors += 1;
      logger.warn({ err: String(e), event: "db_query_failed" }, "message cache read failed");
      return [];
    }
  }

  /** Drop everything; called with the ring flush on bind. */
  purge(): number {
    try {
      const before = this.rowCount();
      this.db.exec("DELETE FROM messages");
      return before;
    } catch (e) {
      this.errors += 1;
      logger.warn({ err: String(e), event: "db_purge_failed" }, "message cache purge failed");
      return 0;
    }
  }

  stats(): MessageDbStats {
    return { rows: this.rowCount(), appended: this.appended, errors: this.errors };
  }

  close(): void {
    try {
      this.db.close();
    } catch {}
  }

  private rowCount(): number {
    try {
      const row = this.db.prepare("SELECT COUNT(*) AS n FROM messages").get();
      return Number(row?.n ?? 0);
    } catch {
      return 0;
    }
  }

  // By age (`inserted_at`, not `ts`), then a hard row cap.
  private prune(): void {
    this.sinceLastPrune = 0;
    try {
      const retentionMs = this.opts.retentionMs ?? DEFAULT_RETENTION_MS;
      const maxRows = this.opts.maxRows ?? DEFAULT_MAX_ROWS;
      this.db
        .prepare("DELETE FROM messages WHERE inserted_at < ?")
        .run(Date.now() - retentionMs);
      this.db
        .prepare(
          "DELETE FROM messages WHERE id <= " +
            "(SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?)",
        )
        .run(maxRows);
    } catch (e) {
      this.errors += 1;
      logger.warn({ err: String(e), event: "db_prune_failed" }, "message cache prune failed");
    }
  }
}

/** Null without `node:sqlite` or on an unwritable path; the ring keeps working. */
export async function openMessageDb(opts: MessageDbOptions): Promise<MessageDb | null> {
  let Database: typeof DatabaseSync;
  try {
    ({ DatabaseSync: Database } = await import("node:sqlite"));
  } catch (e) {
    logger.warn(
      { err: String(e), node: process.version, event: "db_unavailable" },
      "node:sqlite missing — /db/messages will report unavailable",
    );
    return null;
  }
  try {
    await mkdir(dirname(opts.path), { recursive: true, mode: 0o700 });
    const db = new Database(opts.path);
    db.exec(SCHEMA);
    await chmod(opts.path, 0o600);
    logger.info({ path: opts.path, event: "db_open" }, "message cache open");
    return new MessageDb(db, opts);
  } catch (e) {
    logger.error({ err: String(e), event: "db_open_failed" }, "message cache unavailable");
    return null;
  }
}
