// Fastify HTTP gateway. Bearer auth, 127.0.0.1 bind by default.

import Fastify, { type FastifyReply, type FastifyRequest } from "fastify";
import type { WASocket } from "baileys";
import fastifyMultipart from "@fastify/multipart";
import QRCode from "qrcode";
import pino from "pino";
import { createReadStream } from "node:fs";
import { stat, readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { WaSession } from "./wa-session.js";
import {
  Inbox,
  RecentlySent,
  makeUpsertHandler,
  makeHistorySetHandler,
  makeEventCounters,
} from "./wa-inbound.js";
import { sendText, sendTextToJid, groupCreate } from "./wa-send.js";
import { sendReaction } from "./wa-react.js";
import { sendMedia, type OutboundMediaKind } from "./wa-media-send.js";
import { MediaStore, type DecryptOpts } from "./wa-media.js";
import { openMessageDb } from "./wa-db.js";
import { listParticipatingGroups } from "./wa-groups.js";
import { conversationIdForDm, conversationIdForGroup, normalizeGroupJid } from "./normalize-jid.js";
import { BindStore } from "./wa-bind.js";
import { PairingCode } from "./wa-pairing.js";
import { BindMethodConfig } from "./wa-bind-method.js";
import { BindFlow } from "./wa-bind-flow.js";
import { fetchInviteCode } from "./wa-auto-group.js";
import { deleteSent, editSent } from "./wa-edit.js";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "info" }).child({ mod: "server" });

function envBool(name: string, def: boolean): boolean {
  const v = process.env[name];
  if (v === undefined) return def;
  return v === "1" || v.toLowerCase() === "true";
}

function envCSV(name: string): string[] {
  const v = process.env[name];
  if (!v) return [];
  return v.split(",").map((s) => s.trim()).filter(Boolean);
}

// Precedence: WHATSAPP_AUTO_GROUP_NAME env > ~/.agent_settings.json
// whatsapp.channel_label (written by install.sh --channel) > "🥷 Ninja".
async function resolveAutoGroupName(): Promise<string> {
  const envVal = (process.env.WHATSAPP_AUTO_GROUP_NAME ?? "").trim();
  if (envVal) return envVal;
  try {
    const raw = await readFile(`${homedir()}/.agent_settings.json`, "utf-8");
    const settings = JSON.parse(raw) as { whatsapp?: { channel_label?: unknown } };
    const label = (settings?.whatsapp?.channel_label ?? "").toString().trim();
    if (label) return label;
  } catch {}
  return "🥷 Ninja";
}

async function main() {
  const bind = process.env.WHATSAPP_BIND ?? "127.0.0.1";
  const port = Number(process.env.PORT ?? 8090);
  const token = process.env.WHATSAPP_GATEWAY_TOKEN ?? "";
  const authDir = process.env.WHATSAPP_AUTH_DIR ?? "./auth/default";
  const echo = envBool("WHATSAPP_ECHO", false);
  const allowGroupCreate = envBool("WHATSAPP_ALLOW_GROUP_CREATE", false);
  const allowedTo = new Set(envCSV("WHATSAPP_ALLOWED_TO").map((s) => s.replace(/\D/g, "")));
  const forceSingleTo = envBool("WHATSAPP_FORCE_SINGLE_TO", true);
  const defaultTo = (process.env.WHATSAPP_TO ?? "").replace(/\D/g, "");
  const syncFullHistory = envBool("WHATSAPP_SYNC_FULL_HISTORY", false);
  const historyMaxAgeMs = Math.max(
    0,
    Number(process.env.WHATSAPP_HISTORY_SYNC_MAX_AGE_MS ?? 604_800_000) || 604_800_000,
  );

  // Media bytes live in mediaDir under 0600 sha-keyed filenames; TTL sweeper evicts.
  const mediaDir = process.env.WHATSAPP_MEDIA_DIR ?? "/root/.ninja_whatsapp/media";
  const mediaTtlMs = Math.max(
    60_000,
    (Number(process.env.WHATSAPP_MEDIA_TTL_SECONDS ?? 3600) || 3600) * 1000,
  );
  const maxMediaBytes = Math.max(
    1024,
    Number(process.env.WHATSAPP_MAX_MEDIA_BYTES ?? 104_857_600) || 104_857_600,
  );

  const dbPath = process.env.WHATSAPP_DB_PATH ?? "/root/.ninja_whatsapp/messages.db";
  const dbRetentionMs = Math.max(
    60_000,
    (Number(process.env.WHATSAPP_DB_RETENTION_DAYS ?? 14) || 14) * 86_400_000,
  );

  if (bind !== "127.0.0.1" && bind !== "localhost" && !token) {
    throw new Error("WHATSAPP_GATEWAY_TOKEN required when binding to non-localhost");
  }

  if (echo && allowedTo.size === 0) {
    throw new Error("WHATSAPP_ECHO=1 requires WHATSAPP_ALLOWED_TO to be set (never empty)");
  }

  const recentlySent = new RecentlySent();
  const counters = makeEventCounters();
  const session = new WaSession(authDir, { syncFullHistory });

  // Under /root/.ninja_whatsapp, which wipe-state.sh removes before a snapshot.
  const messageDb = await openMessageDb({
    path: dbPath,
    epoch: () => session.getInboxEpoch(),
    retentionMs: dbRetentionMs,
  });
  const inbox = new Inbox(messageDb);

  const mediaStore = new MediaStore();
  const decryptOpts: DecryptOpts = {
    dir: mediaDir,
    maxBytes: maxMediaBytes,
  };
  // 60s sweep cadence bounds tmpfs at TTL + 1m.
  const mediaSweeper = setInterval(() => {
    void mediaStore.sweep(mediaTtlMs);
  }, 60_000);
  mediaSweeper.unref();

  // BindStore is always created so /bind works without env. bound.json next to authDir survives restarts.
  const bindStore = new BindStore(authDir);
  await bindStore.load();
  const envBound = (process.env.WHATSAPP_ALLOWED_CHAT_JID ?? "").trim();
  if (envBound && !bindStore.getChatJid()) {
    await bindStore.set(envBound, "install");
  }
  const pairing = new PairingCode();
  const bindMethod = new BindMethodConfig();
  const autoGroupName = await resolveAutoGroupName();
  const bindFlow = new BindFlow({
    sock: () => session.getSocket(),
    selfE164: () => session.getSelfE164(),
    bindStore,
    bindMethod,
    pairing,
    counters,
    subject: () => autoGroupName,
    recentlySent,
    inbox,
    bumpInboxEpoch: () => session.bumpInboxEpoch(),
  });

  const inboundDeps = {
    sock: () => session.getSocket(),
    inbox,
    recentlySent,
    cfg: {
      echo,
      allowedTo,
      selfE164Getter: () => session.getSelfE164(),
    },
    historyMaxAgeMs,
    noteHistoryActivity: () => session.noteHistoryActivity(),
    counters,
    bind: bindStore,
    pairing,
    onBound: (via: "pairing_code") => bindFlow.onBound(via),
    media: { store: mediaStore, opts: decryptOpts },
  };
  const upsertHandler = makeUpsertHandler(inboundDeps);
  const historySetHandler = makeHistorySetHandler(inboundDeps);
  session.setEvents({
    onUpsert: upsertHandler as unknown as (m: unknown) => void,
    onHistorySet: historySetHandler as unknown as (h: unknown) => void,
    onOpen: () => bindFlow.onOpen(),
    onClose: () => bindFlow.onClose(),
    // Clear in-memory bind state on logout. bound.json was already wiped
    // (lives inside authDir) but BindStore caches it — without this,
    // /status keeps returning stale bound_chat_jid on snapshot-leaked installs.
    onLoggedOut: () => {
      void bindStore.clear();
    },
  });
  await session.start();

  const app = Fastify({ logger: false });
  await app.register(fastifyMultipart, {
    limits: { fileSize: maxMediaBytes, files: 1 },
  });

  function requireAuth(req: FastifyRequest, reply: FastifyReply): boolean {
    if (!token) return true; // localhost-only
    const h = req.headers.authorization ?? "";
    if (h === `Bearer ${token}`) return true;
    reply.code(401).send({ error: "unauthorized" });
    return false;
  }

  // Bind-flow phases take precedence over generic states when open + unbound.
  function ninjaState(): string {
    const conn = session.getState();
    // qr/logged_out override bindStore — saved chat is stale until re-link.
    // "bound" must win for transient connecting → open reconnects (otherwise
    // dashboard flashes "waiting_for_qr" during every keepalive cycle).
    if (conn === "logged_out") return "logged_out";
    if (conn === "qr") return "waiting_for_qr";
    if (bindStore.getChatJid()) return "bound";
    if (conn === "connecting") return "reconnecting";
    if (conn === "starting") return "starting";
    if (conn === "open") {
      const phase = bindFlow.getPhase();
      if (phase === "creating_group") return "creating_group";
      if (phase === "auto_group_failed") return "auto_group_failed";
      const snap = pairing.snapshot();
      if (snap.code) return "waiting_for_chat_pairing";
      if (phase === "awaiting_bind_method") return "awaiting_bind_method";
      return "connected";
    }
    return conn;
  }

  // Lazy fetch on /status; refresh only after a failure resolves.
  let inviteCodeCache: { code: string; group_jid: string } | null = null;
  async function maybeFetchInviteCode(): Promise<{ code: string | null; err: string | null }> {
    const st = bindStore.get();
    if (!st || st.bound_via !== "auto_group") return { code: null, err: null };
    if (inviteCodeCache && inviteCodeCache.group_jid === st.chat_jid) {
      return { code: inviteCodeCache.code, err: null };
    }
    const sock = session.getSocket();
    if (!sock) return { code: null, err: null };
    const code = await fetchInviteCode(sock, st.chat_jid);
    if (code) {
      inviteCodeCache = { code, group_jid: st.chat_jid };
      return { code, err: null };
    }
    counters.invite_code_fetch_err += 1;
    return { code: null, err: "invite_code_unavailable" };
  }

  app.get("/health", async () => ({ ok: true, state: session.getState() }));

  app.get("/status", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    const pair = pairing.snapshot();
    const invite = await maybeFetchInviteCode();
    return {
      connection: session.getState(),
      linked: session.getState() === "open",
      qr_fingerprint: session.getQrFingerprint(),
      self_e164: session.getSelfE164(),
      inbox_epoch: session.getInboxEpoch(),
      history_sync_active: session.getHistorySyncActive(),
      sync_full_history: session.isSyncFullHistory(),
      events_seen: counters,
      message_cache: messageDb ? messageDb.stats() : null,
      bound_chat_jid: bindStore.getChatJid(),
      bound_via: bindStore.get()?.bound_via ?? null,
      bound_at: bindStore.get()?.bound_at ?? null,
      ninja_state: ninjaState(),
      pairing_code_active: Boolean(pair.code),
      pairing_code_expires_at: pair.expires_at,
      bind_method: bindMethod.get(),
      bind_method_source: bindMethod.source(),
      last_active_method: bindFlow.getLastActiveMethod(),
      last_error: bindFlow.getLastError(),
      grace_remaining_ms: bindFlow.graceRemainingMs(),
      invite_code: invite.code,
      invite_code_error: invite.err,
      auto_group_name: autoGroupName,
    };
  });

  app.post("/bind", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    const body = (req.body ?? {}) as { chat_jid?: string };
    const jid = (body.chat_jid ?? "").toString().trim();
    if (!jid) {
      reply.code(400).send({ error: "chat_jid required" });
      return;
    }
    if (!jid.endsWith("@g.us") && !jid.endsWith("@s.whatsapp.net")) {
      reply.code(400).send({ error: "chat_jid must end with @g.us or @s.whatsapp.net" });
      return;
    }
    const state = await bindStore.set(jid, "api");
    pairing.clear();
    bindFlow.onBound("api");
    inviteCodeCache = null;
    return { ok: true, chat_jid: state.chat_jid, bound_via: state.bound_via };
  });

  app.post("/unbind", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    const prev = bindStore.get()?.bound_via ?? null;
    await bindStore.clear();
    inviteCodeCache = null;
    if (session.getState() === "open") {
      bindFlow.onUnbound(prev);
    }
    return { ok: true };
  });

  app.get("/pairing_code", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (bindStore.getChatJid()) {
      return { code: null, expires_at: null, bound_chat_jid: bindStore.getChatJid() };
    }
    // Read-only snapshot — bind flow owns when a code exists.
    const snap = pairing.snapshot();
    return {
      code: snap.code,
      expires_at: snap.expires_at,
      bound_chat_jid: null,
    };
  });

  app.post("/bind_method", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (bindStore.getChatJid()) {
      reply.code(409).send({ error: "already_bound", chat_jid: bindStore.getChatJid() });
      return;
    }
    if (bindFlow.getPhase() === "creating_group") {
      reply.code(409).send({ error: "bind_in_progress" });
      return;
    }
    const body = (req.body ?? {}) as { method?: string };
    const raw = (body.method ?? "").toString().trim();
    if (!raw) {
      reply.code(400).send({ error: "method required" });
      return;
    }
    try {
      const active = bindFlow.setOverride(raw);
      return {
        ok: true,
        method: active,
        source: bindMethod.source(),
        phase: bindFlow.getPhase(),
        grace_remaining_ms: bindFlow.graceRemainingMs(),
      };
    } catch (e) {
      reply.code(400).send({ error: "invalid_method", detail: String(e) });
    }
  });

  app.post("/unlink", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    await bindStore.clear();
    pairing.clear();
    inviteCodeCache = null;
    bindFlow.reset();
    // Fire-and-forget: awaiting sock.logout() (network round-trip) can exceed
    // the sandbox proxy POST timeout → 302 /app/unavailable. Don't `await` here.
    void session.unlink().catch((e) => {
      reply.log?.error({ err: String(e) }, "unlink failed");
    });
    return { ok: true, state: session.getState() };
  });

  app.post("/bind_now", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (bindStore.getChatJid()) {
      reply.code(409).send({ error: "already_bound", chat_jid: bindStore.getChatJid() });
      return;
    }
    if (session.getState() !== "open") {
      reply.code(409).send({ error: "not_linked", state: session.getState() });
      return;
    }
    try {
      await bindFlow.triggerNow();
      return {
        ok: true,
        method: bindMethod.get(),
        phase: bindFlow.getPhase(),
        bound_chat_jid: bindStore.getChatJid(),
        last_error: bindFlow.getLastError(),
      };
    } catch (e) {
      const msg = String((e as Error).message ?? e);
      if (msg === "already_bound" || msg === "bind_in_progress") {
        reply.code(409).send({ error: msg });
        return;
      }
      reply.code(500).send({ error: "bind_now_failed", detail: msg });
    }
  });

  app.post("/retry_bind", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (bindFlow.getPhase() !== "auto_group_failed") {
      reply.code(409).send({ error: "not_in_failed_state", phase: bindFlow.getPhase() });
      return;
    }
    try {
      await bindFlow.retry();
      return {
        ok: true,
        phase: bindFlow.getPhase(),
        last_error: bindFlow.getLastError(),
      };
    } catch (e) {
      reply.code(500).send({ error: "retry_failed", detail: String(e) });
    }
  });

  app.get("/qr", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    reply.header("Cache-Control", "no-store");
    const qr = session.getLatestQr();
    if (!qr) {
      reply.code(404).send({ error: "no_qr", state: session.getState() });
      return;
    }
    // Hold QR back until socket is stable: a scan during settling (recent 408)
    // can't finish handshake → phone shows "Check your connection, try again".
    if (!session.isQrStable()) {
      reply.code(503).send({
        error: "connecting",
        state: session.getState(),
        last_close: session.getLastClose(),
      });
      return;
    }
    const fmt = (req.query as any)?.format ?? "png";
    if (fmt === "text") {
      reply.header("Content-Type", "text/plain; charset=utf-8");
      return qr;
    }
    const buf = await QRCode.toBuffer(qr, { type: "png", margin: 1, scale: 6 });
    reply.header("Content-Type", "image/png");
    return buf;
  });

  app.post("/send", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (session.getState() !== "open") {
      reply.code(409).send({ error: "not_linked", state: session.getState() });
      return;
    }
    const body = (req.body ?? {}) as { to?: string; group_jid?: string; text?: string };
    const text = (body.text ?? "").toString();
    if (!text) {
      reply.code(400).send({ error: "text required" });
      return;
    }
    const rawGroupJid = (body.group_jid ?? "").toString().trim();
    const rawTo = (body.to ?? "").toString().trim();
    if (rawGroupJid && rawTo) {
      reply.code(400).send({ error: "provide either to or group_jid, not both" });
      return;
    }
    if (rawGroupJid) {
      let groupJid: string;
      try {
        groupJid = normalizeGroupJid(rawGroupJid);
      } catch (e) {
        reply.code(400).send({ error: "invalid group_jid", detail: String(e) });
        return;
      }
      try {
        const res = await sendTextToJid(
          { sock: () => session.getSocket(), recentlySent },
          groupJid,
          text,
        );
        const self = session.getSelfE164();
        const conversation_id = self ? conversationIdForGroup(self, groupJid) : null;
        return { ok: true, group_jid: groupJid, jid: res.jid, id: res.id, conversation_id };
      } catch (e) {
        reply.code(500).send({ error: "send_failed", detail: String(e) });
      }
      return;
    }
    let to = rawTo.replace(/\D/g, "");
    if (forceSingleTo) {
      if (!defaultTo) {
        reply.code(400).send({ error: "WHATSAPP_FORCE_SINGLE_TO=1 but WHATSAPP_TO is not set" });
        return;
      }
      to = defaultTo;
    }
    if (!to) {
      reply.code(400).send({ error: "to or group_jid required" });
      return;
    }
    if (allowedTo.size > 0 && !allowedTo.has(to)) {
      reply.code(403).send({ error: "destination not in WHATSAPP_ALLOWED_TO" });
      return;
    }
    try {
      const res = await sendText({ sock: () => session.getSocket(), recentlySent }, to, text);
      const self = session.getSelfE164();
      const conversation_id = self ? conversationIdForDm(self, to) : null;
      return { ok: true, to, jid: res.jid, id: res.id, conversation_id };
    } catch (e) {
      reply.code(500).send({ error: "send_failed", detail: String(e) });
    }
  });

  app.get("/groups", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (session.getState() !== "open") {
      reply.code(409).send({ error: "not_linked", state: session.getState() });
      return;
    }
    try {
      const groups = await listParticipatingGroups(session.getSocket(), session.getSelfE164());
      return { ok: true, groups };
    } catch (e) {
      reply.code(500).send({ error: "group_list_failed", detail: String(e) });
    }
  });

  app.post("/groups/create", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (!allowGroupCreate) {
      reply.code(403).send({ error: "group_create_disabled", hint: "set WHATSAPP_ALLOW_GROUP_CREATE=1" });
      return;
    }
    if (session.getState() !== "open") {
      reply.code(409).send({ error: "not_linked", state: session.getState() });
      return;
    }
    const body = (req.body ?? {}) as { subject?: string; participants?: string[]; welcome?: string };
    const subject = (body.subject ?? "").toString().trim();
    const participants = Array.isArray(body.participants)
      ? body.participants.map((p) => String(p).replace(/\D/g, "")).filter(Boolean)
      : [];
    if (!subject) {
      reply.code(400).send({ error: "subject required" });
      return;
    }
    if (participants.length === 0) {
      reply.code(400).send({ error: "participants required" });
      return;
    }
    try {
      const res = await groupCreate(
        { sock: () => session.getSocket(), recentlySent },
        subject,
        participants,
        body.welcome,
      );
      const self = session.getSelfE164();
      const conversation_id = self ? conversationIdForGroup(self, res.group_jid) : null;
      return { ok: true, ...res, conversation_id };
    } catch (e) {
      reply.code(500).send({ error: "group_create_failed", detail: String(e) });
    }
  });

  // Live progress bar: rewrite or remove a message sent through /send, by id.
  const sentMessageRoute =
    (run: (sock: WASocket, id: string, text: string) => Promise<boolean>) =>
    async (req: FastifyRequest, reply: FastifyReply) => {
      if (!requireAuth(req, reply)) return;
      const sock = session.getSocket();
      if (session.getState() !== "open" || !sock) {
        reply.code(409).send({ error: "not_linked", state: session.getState() });
        return;
      }
      const body = (req.body ?? {}) as { id?: string; text?: string };
      if (!body.id) {
        reply.code(400).send({ error: "id required" });
        return;
      }
      try {
        if (await run(sock, body.id, body.text ?? "")) return { ok: true };
        reply.code(404).send({ error: "unknown_id" });
      } catch (e) {
        reply.code(500).send({ error: "failed", detail: String(e) });
      }
    };
  app.post("/edit", sentMessageRoute(editSent));
  app.post("/delete", sentMessageRoute((sock, id) => deleteSent(sock, id)));

  app.post("/react", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (session.getState() !== "open") {
      reply.code(409).send({ error: "not_linked", state: session.getState() });
      return;
    }
    const body = (req.body ?? {}) as {
      message_key?: string;
      emoji?: string;
      from_me?: boolean;
      participant?: string;
    };
    const messageKey = (body.message_key ?? "").toString();
    const emoji = (body.emoji ?? "").toString();
    if (!messageKey || !emoji) {
      reply.code(400).send({ error: "message_key and emoji required" });
      return;
    }
    // Split on last ':' — JIDs use '@' as user/domain separator, so key is `${remoteJid}:${id}`.
    const sep = messageKey.lastIndexOf(":");
    if (sep <= 0 || sep === messageKey.length - 1) {
      reply.code(400).send({ error: "invalid message_key" });
      return;
    }
    const remoteJid = messageKey.slice(0, sep);
    const messageId = messageKey.slice(sep + 1);
    const bound = bindStore.getChatJid();
    if (bound && remoteJid !== bound) {
      counters.dropped_react_not_bound_chat += 1;
      reply.code(403).send({ error: "react_not_bound_chat" });
      return;
    }
    const sock = session.getSocket();
    if (!sock) {
      reply.code(409).send({ error: "not_linked" });
      return;
    }
    try {
      await sendReaction(
        sock,
        remoteJid,
        messageId,
        Boolean(body.from_me),
        emoji,
        body.participant ?? null,
      );
      counters.react_sent_ok += 1;
      return { ok: true };
    } catch (e) {
      counters.react_sent_err += 1;
      reply.code(500).send({ error: "react_failed", detail: String(e) });
    }
  });

  // Bytes live on local tmpfs (0600); bearer-authed. ID is sha256[:16] prefix from MediaRecord.
  app.get("/media/:id", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    const id = ((req.params as { id?: string })?.id ?? "").trim();
    if (!id) {
      reply.code(400).send({ error: "id required" });
      return;
    }
    const rec = mediaStore.get(id);
    if (!rec) {
      reply.code(404).send({ error: "not_found" });
      return;
    }
    try {
      const st = await stat(rec.path);
      reply.header("Content-Type", rec.mimetype);
      reply.header("Content-Length", String(st.size));
      reply.header("Cache-Control", "no-store");
      return reply.send(createReadStream(rec.path));
    } catch (e) {
      reply.code(500).send({ error: "read_failed", detail: String(e) });
    }
  });

  // Multipart media send. Same bound-chat/allowlist semantics as /send; returns message_id for loopback correlation.
  app.post("/send_media", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (session.getState() !== "open") {
      reply.code(409).send({ error: "not_linked", state: session.getState() });
      return;
    }
    let kind: OutboundMediaKind | null = null;
    let caption = "";
    let filename = "";
    let mimetype = "";
    let to = "";
    let groupJid = "";
    let bytes: Buffer | null = null;
    try {
      const parts = req.parts();
      for await (const part of parts) {
        if (part.type === "file") {
          // Capture filename/mimetype from the part; form fields override.
          if (!filename) filename = part.filename ?? "";
          if (!mimetype) mimetype = part.mimetype ?? "";
          bytes = await part.toBuffer();
        } else {
          const v = String(part.value ?? "");
          if (part.fieldname === "kind") kind = v === "document" ? "document" : v === "image" ? "image" : null;
          else if (part.fieldname === "caption") caption = v;
          else if (part.fieldname === "filename") filename = v;
          else if (part.fieldname === "mimetype") mimetype = v;
          else if (part.fieldname === "to") to = v.replace(/\D/g, "");
          else if (part.fieldname === "group_jid") groupJid = v.trim();
        }
      }
    } catch (e) {
      reply.code(400).send({ error: "multipart_parse_failed", detail: String(e) });
      return;
    }
    if (!kind) {
      reply.code(400).send({ error: "kind must be 'image' or 'document'" });
      return;
    }
    if (!bytes || bytes.length === 0) {
      reply.code(400).send({ error: "file required" });
      return;
    }
    if (kind === "document" && !filename) {
      reply.code(400).send({ error: "filename required for documents" });
      return;
    }
    if (!mimetype) {
      mimetype = kind === "image" ? "image/jpeg" : "application/octet-stream";
    }
    let remoteJid = "";
    if (groupJid && to) {
      reply.code(400).send({ error: "provide either to or group_jid, not both" });
      return;
    }
    if (groupJid) {
      try {
        remoteJid = normalizeGroupJid(groupJid);
      } catch (e) {
        reply.code(400).send({ error: "invalid group_jid", detail: String(e) });
        return;
      }
    } else {
      let destTo = to;
      if (forceSingleTo) {
        if (!defaultTo) {
          reply.code(400).send({ error: "WHATSAPP_FORCE_SINGLE_TO=1 but WHATSAPP_TO is not set" });
          return;
        }
        destTo = defaultTo;
      }
      if (!destTo) {
        reply.code(400).send({ error: "to or group_jid required" });
        return;
      }
      if (allowedTo.size > 0 && !allowedTo.has(destTo)) {
        reply.code(403).send({ error: "destination not in WHATSAPP_ALLOWED_TO" });
        return;
      }
      remoteJid = `${destTo}@s.whatsapp.net`;
    }
    const bound = bindStore.getChatJid();
    if (bound && remoteJid !== bound) {
      counters.dropped_send_media_not_bound_chat += 1;
      reply.code(403).send({ error: "send_media_not_bound_chat" });
      return;
    }
    const sock = session.getSocket();
    if (!sock) {
      reply.code(409).send({ error: "not_linked" });
      return;
    }
    try {
      const res = await sendMedia(sock, remoteJid, {
        kind,
        bytes,
        mimetype,
        caption: caption || undefined,
        filename: filename || undefined,
      });
      if (res.message_id) {
        recentlySent.addMessageId(remoteJid, res.message_id);
        // Seed text ring with caption: fromMe notify upsert carries caption as text,
        // letting the existing text-based loopback check drop it.
        if (caption) recentlySent.add(remoteJid, caption);
      }
      counters.media_sent_ok += 1;
      return { ok: true, jid: remoteJid, id: res.message_id, kind };
    } catch (e) {
      counters.media_sent_err += 1;
      reply.code(500).send({ error: "send_media_failed", detail: String(e) });
    }
  });

  app.get("/messages", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    const q = (req.query ?? {}) as { since?: string; limit?: string };
    const since = Number(q.since ?? 0) || 0;
    const limit = Math.min(500, Math.max(1, Number(q.limit ?? 100) || 100));
    const items = inbox.since(since, limit);
    return {
      items,
      latest_seq: inbox.latestSeq(),
      inbox_epoch: session.getInboxEpoch(),
    };
  });

  // /messages from the SQLite mirror; cursor-free. 503 = no cache, fall back.
  app.get("/db/messages", async (req, reply) => {
    if (!requireAuth(req, reply)) return;
    if (!messageDb) {
      reply.code(503).send({ error: "message_cache_unavailable" });
      return;
    }
    const q = (req.query ?? {}) as {
      limit?: string;
      channel_id?: string;
      start_ts?: string;
      end_ts?: string;
    };
    const items = messageDb.query({
      limit: Math.min(500, Math.max(1, Number(q.limit ?? 100) || 100)),
      channelId: (q.channel_id ?? "").trim() || null,
      startTs: Number(q.start_ts ?? 0) || null,
      endTs: Number(q.end_ts ?? 0) || null,
    });
    return {
      items,
      count: items.length,
      inbox_epoch: session.getInboxEpoch(),
    };
  });

  const addr = await app.listen({ host: bind, port });
  logger.info(
    {
      addr,
      bind,
      port,
      echo,
      allow_group_create: allowGroupCreate,
      sync_full_history: syncFullHistory,
      history_max_age_ms: historyMaxAgeMs,
      message_cache: Boolean(messageDb),
    },
    "gateway listening",
  );

  const shutdown = async (sig: string) => {
    logger.info({ sig }, "shutting down");
    try {
      await app.close();
    } catch {}
    await session.close();
    messageDb?.close();
    process.exit(0);
  };
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
}

main().catch((e) => {
  // eslint-disable-next-line no-console
  console.error("fatal:", e);
  process.exit(1);
});
