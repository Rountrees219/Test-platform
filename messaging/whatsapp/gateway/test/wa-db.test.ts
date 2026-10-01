// Run with: npm test
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, beforeEach, describe, it } from "node:test";
import { Inbox, type InboxRecord } from "../src/wa-inbound.js";
import { openMessageDb, type MessageDb } from "../src/wa-db.js";

let dir: string;
let db: MessageDb;
let epoch = 1;

function record(over: Partial<InboxRecord> = {}): Omit<InboxRecord, "seq"> {
  return {
    provider: "whatsapp",
    workspace_id: "15550001111",
    user_id: "15550002222",
    channel_id: "15550001111:15550002222",
    thread_id: null,
    text: "hello",
    from_me: false,
    ts: 1_700_000_000_000,
    message_key: "15550002222@s.whatsapp.net:AAA",
    participant: null,
    media_kind: null,
    media_id: null,
    media_mimetype: null,
    media_seconds: null,
    media_bytes: null,
    media_filename: null,
    sender_name: "Ken",
    quoted_message_key: null,
    quoted_text: null,
    quoted_sender: null,
    ...over,
  };
}

describe("MessageDb", () => {
  before(async () => {
    dir = await mkdtemp(join(tmpdir(), "wa-db-"));
  });

  after(async () => {
    db?.close();
    await rm(dir, { recursive: true, force: true });
  });

  beforeEach(async () => {
    db?.close();
    epoch = 1;
    const opened = await openMessageDb({
      path: join(dir, `${Date.now()}-${Math.random()}.db`),
      epoch: () => epoch,
    });
    assert.ok(opened, "node:sqlite unavailable — this runtime cannot back the cache");
    db = opened;
  });

  it("round-trips every InboxRecord field", () => {
    const inbox = new Inbox(db);
    const written = inbox.append(
      record({
        from_me: true,
        thread_id: "t-1",
        participant: "15550003333@s.whatsapp.net",
        media_kind: "voice",
        media_id: "m-1",
        media_mimetype: "audio/ogg",
        media_seconds: 12,
        media_bytes: 4096,
        media_filename: null,
        quoted_message_key: "15550002222@s.whatsapp.net:BBB",
        quoted_text: "earlier",
        quoted_sender: "15550002222",
      }),
    );
    assert.deepEqual(db.query({ limit: 10 }), [written]);
  });

  it("dedupes on message_key", () => {
    const inbox = new Inbox(db);
    inbox.append(record());
    inbox.append(record({ text: "redelivered" }));
    const items = db.query({ limit: 10 });
    assert.equal(items.length, 1);
    assert.equal(items[0]!.text, "hello");
  });

  it("keeps records with no message_key", () => {
    const inbox = new Inbox(db);
    inbox.append(record({ message_key: null }));
    inbox.append(record({ message_key: null, text: "second" }));
    assert.equal(db.query({ limit: 10 }).length, 2);
  });

  it("returns the newest limit records, oldest-first", () => {
    const inbox = new Inbox(db);
    for (let i = 0; i < 5; i++) inbox.append(record({ message_key: `k-${i}`, text: `m${i}` }));
    assert.deepEqual(
      db.query({ limit: 2 }).map((r) => r.text),
      ["m3", "m4"],
    );
    assert.deepEqual(
      db.query({ limit: 10 }).map((r) => r.text),
      ["m0", "m1", "m2", "m3", "m4"],
    );
  });

  // Recency is ingestion order, not `ts`.
  it("orders by arrival, not timestamp", () => {
    const inbox = new Inbox(db);
    inbox.append(record({ message_key: "new", text: "new", ts: 3000 }));
    inbox.append(record({ message_key: "backfilled", text: "backfilled", ts: 1000 }));
    assert.deepEqual(
      db.query({ limit: 1 }).map((r) => r.text),
      ["backfilled"],
    );
  });

  it("filters by channel and time window", () => {
    const inbox = new Inbox(db);
    inbox.append(record({ message_key: "a", channel_id: "c1", ts: 1000 }));
    inbox.append(record({ message_key: "b", channel_id: "c2", ts: 2000 }));
    inbox.append(record({ message_key: "c", channel_id: "c1", ts: 3000 }));
    assert.deepEqual(
      db.query({ limit: 10, channelId: "c1" }).map((r) => r.message_key),
      ["a", "c"],
    );
    assert.deepEqual(
      db.query({ limit: 10, startTs: 2000, endTs: 3000 }).map((r) => r.message_key),
      ["b", "c"],
    );
  });

  it("serves records written before a restart", async () => {
    const path = join(dir, "restart.db");
    const first = await openMessageDb({ path, epoch: () => 1 });
    assert.ok(first);
    new Inbox(first).append(record({ message_key: "before-restart", text: "old" }));
    first.close();

    const second = await openMessageDb({ path, epoch: () => 2 });
    assert.ok(second);
    new Inbox(second).append(record({ message_key: "after-restart", text: "new" }));
    assert.deepEqual(
      second.query({ limit: 10 }).map((r) => r.text),
      ["old", "new"],
    );
    // Both rows are seq 1.
    assert.deepEqual(
      second.query({ limit: 10 }).map((r) => r.seq),
      [1, 1],
    );
    second.close();
  });

  it("purges when the ring is flushed", () => {
    const inbox = new Inbox(db);
    inbox.append(record({ message_key: "pre-bind" }));
    inbox.flush();
    assert.deepEqual(db.query({ limit: 10 }), []);
    const next = inbox.append(record({ message_key: "post-bind" }));
    assert.equal(next.seq, 1);
    assert.deepEqual(
      db.query({ limit: 10 }).map((r) => r.message_key),
      ["post-bind"],
    );
  });

  it("returns null when the path cannot be opened", async () => {
    const blocker = join(dir, "blocker");
    await writeFile(blocker, "");
    const opened = await openMessageDb({
      path: join(blocker, "nested.db"), // ENOTDIR — parent is a file
      epoch: () => 1,
    });
    assert.equal(opened, null);
  });
});
