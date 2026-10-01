// Baileys socket lifecycle: connect, QR, reconnect, logout.

import { createHash } from "node:crypto";
import { rm } from "node:fs/promises";
import path from "node:path";
import pino from "pino";
import {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  type WASocket,
} from "baileys";

type BoomLike = { output?: { statusCode?: number } };

export type ConnState = "starting" | "qr" | "connecting" | "open" | "logged_out" | "closed";

export interface SessionEvents {
  onUpsert?: (m: unknown) => void;
  onHistorySet?: (h: unknown) => void;
  /** connection=open. Ninja mode starts bind flow if no chat bound yet. */
  onOpen?: () => void;
  /** connection=close. Bind flow uses it to cancel pending grace timer. */
  onClose?: () => void;
  /**
   * DisconnectReason.loggedOut. Auth dir already wiped by handleLoggedOut.
   * Server clears BindStore so stale bound_chat_jid doesn't survive a
   * snapshot-leak recovery.
   */
  onLoggedOut?: () => void;
}

export interface SessionOptions {
  /** Baileys `syncFullHistory`. Default false (personal-phone safe). */
  syncFullHistory?: boolean;
  /** Debounce for flipping history_sync_active=false. Default 5s. */
  historySyncQuietMs?: number;
}

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-session" });

const TRANSIENT_CODES = new Set<number>([
  DisconnectReason.connectionClosed,
  DisconnectReason.connectionLost,
  DisconnectReason.restartRequired,
  DisconnectReason.timedOut,
  503, // surfaces via Boom.output.statusCode
]);

const REPLACED_RECONNECT_MS =
  Number(process.env.WHATSAPP_REPLACED_RECONNECT_MS) || 20_000;

// Hold QR back this long after a close so a scan during socket-settling
// doesn't fail ("Check your connection" — see QR-link incident report).
const QR_STABLE_WINDOW_MS = Number(process.env.WHATSAPP_QR_STABLE_WINDOW_MS) || 30_000;

export class WaSession {
  private sock: WASocket | null = null;
  private socketGeneration = 0;
  private connectPromise: Promise<void> | null = null;
  private state: ConnState = "starting";
  private latestQr: string | null = null;
  private selfE164: string | null = null;
  private authDir: string;
  private events: SessionEvents;
  private shuttingDown = false;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private reconnectAttempt = 0;
  private lastCloseCode: number | null = null;
  private lastCloseAtMs = 0;

  private syncFullHistory: boolean;
  private historySyncQuietMs: number;
  private inboxEpoch = Date.now();
  private connectedAtMs = 0;
  private historySyncActive = false;
  private historyQuietTimer: NodeJS.Timeout | null = null;

  constructor(authDir: string, options: SessionOptions = {}, events: SessionEvents = {}) {
    this.authDir = path.resolve(authDir);
    this.events = events;
    this.syncFullHistory = Boolean(options.syncFullHistory);
    this.historySyncQuietMs = options.historySyncQuietMs ?? 5000;
  }

  setEvents(events: SessionEvents): void {
    this.events = events;
  }

  getState(): ConnState {
    return this.state;
  }

  getSelfE164(): string | null {
    return this.selfE164;
  }

  getLatestQr(): string | null {
    return this.latestQr;
  }

  /** Safe to present QR: have one, in qr state, no recent upstream drop. */
  isQrStable(nowMs = Date.now()): boolean {
    if (!this.latestQr || this.state !== "qr") return false;
    if (this.lastCloseAtMs === 0) return true;
    return nowMs - this.lastCloseAtMs > QR_STABLE_WINDOW_MS;
  }

  getQrFingerprint(nowMs = Date.now()): string | null {
    if (!this.isQrStable(nowMs) || !this.latestQr) return null;
    return createHash("sha256").update(this.latestQr).digest("hex");
  }

  getLastClose(): { code: number | null; ageMs: number } {
    return {
      code: this.lastCloseCode,
      ageMs: this.lastCloseAtMs ? Date.now() - this.lastCloseAtMs : -1,
    };
  }

  getSocket(): WASocket | null {
    return this.sock;
  }

  getInboxEpoch(): number {
    return this.inboxEpoch;
  }

  bumpInboxEpoch(): number {
    this.inboxEpoch = Date.now();
    return this.inboxEpoch;
  }

  getHistorySyncActive(): boolean {
    return this.historySyncActive;
  }

  getConnectedAtMs(): number {
    return this.connectedAtMs;
  }

  isSyncFullHistory(): boolean {
    return this.syncFullHistory;
  }

  /** Re-arm history-sync quiet timer on each inbound append/history batch. */
  noteHistoryActivity(): void {
    if (!this.syncFullHistory) return;
    this.historySyncActive = true;
    this.armHistoryQuietTimer();
  }

  private armHistoryQuietTimer(): void {
    if (this.historyQuietTimer) clearTimeout(this.historyQuietTimer);
    this.historyQuietTimer = setTimeout(() => {
      this.historySyncActive = false;
      this.historyQuietTimer = null;
      logger.info({ event: "history_sync_idle" }, "history sync quiet");
    }, this.historySyncQuietMs);
  }

  async start(): Promise<void> {
    await this.connect();
  }

  async close(): Promise<void> {
    this.shuttingDown = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.historyQuietTimer) {
      clearTimeout(this.historyQuietTimer);
      this.historyQuietTimer = null;
    }
    this.teardownSocket();
    this.state = "closed";
  }

  /** POST /unlink: logout, wipe auth dir, reconnect for fresh QR (no process restart). */
  async unlink(): Promise<void> {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    try {
      await this.sock?.logout();
    } catch (e) {
      logger.warn({ err: String(e) }, "sock.logout() failed; wiping auth anyway");
    }
    this.teardownSocket();
    try {
      await rm(this.authDir, { recursive: true, force: true });
    } catch (e) {
      logger.error({ err: String(e) }, "failed to wipe auth dir on unlink");
    }
    this.latestQr = null;
    this.selfE164 = null;
    this.state = "starting";
    this.reconnectAttempt = 0;
    this.connect().catch((e) => {
      logger.error({ err: String(e) }, "reconnect after unlink failed");
      this.scheduleReconnect({ longDelay: true });
    });
  }

  private teardownSocket(): void {
    this.socketGeneration += 1;
    const sock = this.sock;
    this.sock = null;
    if (!sock) return;
    try {
      sock.end(undefined);
    } catch {}
  }

  private connect(): Promise<void> {
    if (this.connectPromise) return this.connectPromise;
    this.connectPromise = this.doConnect().finally(() => {
      this.connectPromise = null;
    });
    return this.connectPromise;
  }

  private async doConnect(): Promise<void> {
    this.teardownSocket();
    const generation = this.socketGeneration;

    const { state, saveCreds } = await useMultiFileAuthState(this.authDir);
    const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined as any }));

    if (generation !== this.socketGeneration) return;

    this.state = "connecting";
    this.sock = makeWASocket({
      auth: state,
      logger: logger as any,
      printQRInTerminal: false,
      version,
      browser: ["Ninja App", "Chrome", "1.0"],
      syncFullHistory: this.syncFullHistory,
      markOnlineOnConnect: false,
    });

    this.sock.ev.on("creds.update", saveCreds);

    this.sock.ev.on("connection.update", (u) => {
      if (generation !== this.socketGeneration) return;

      const { connection, lastDisconnect, qr } = u;
      if (qr) {
        this.latestQr = qr;
        this.state = "qr";
        logger.info({ event: "qr" }, "QR available");
      }
      if (connection === "open") {
        this.latestQr = null;
        this.state = "open";
        this.reconnectAttempt = 0;
        this.lastCloseCode = null;
        this.lastCloseAtMs = 0;
        const id = this.sock?.user?.id ?? null;
        this.selfE164 = parseSelfE164(id);
        // Bump epoch so CLI cursors invalidate against the fresh inbox.
        this.inboxEpoch = Date.now();
        this.connectedAtMs = this.inboxEpoch;
        if (this.syncFullHistory) {
          this.historySyncActive = true;
          this.armHistoryQuietTimer();
        } else {
          this.historySyncActive = false;
        }
        logger.info(
          {
            event: "open",
            selfE164: this.selfE164,
            inbox_epoch: this.inboxEpoch,
            sync_full_history: this.syncFullHistory,
          },
          "linked",
        );
        if (this.events.onOpen) {
          try {
            this.events.onOpen();
          } catch (e) {
            logger.warn({ err: String(e) }, "onOpen handler failed");
          }
        }
      } else if (connection === "close") {
        const err = (lastDisconnect?.error as BoomLike | undefined)?.output?.statusCode;
        this.lastCloseCode = err ?? null;
        // Deliberate logout (401) → fresh QR is safe to serve immediately. Only
        // network/keepalive closes need the QR_STABLE_WINDOW_MS settling wait.
        this.lastCloseAtMs = err === DisconnectReason.loggedOut ? 0 : Date.now();
        logger.warn({ event: "close", code: err }, "disconnected");
        if (this.events.onClose) {
          try {
            this.events.onClose();
          } catch (e) {
            logger.warn({ err: String(e) }, "onClose handler failed");
          }
        }
        if (err === DisconnectReason.loggedOut) {
          this.state = "logged_out";
          this.latestQr = null;
          this.selfE164 = null;
          void this.handleLoggedOut();
          return;
        }
        if (this.shuttingDown) return;

        if (err === DisconnectReason.connectionReplaced) {
          this.teardownSocket();
          this.scheduleReconnect({ code: err });
          return;
        }

        const longDelay =
          err === DisconnectReason.timedOut || err === DisconnectReason.restartRequired;
        if (err === undefined || TRANSIENT_CODES.has(err)) {
          this.scheduleReconnect({ longDelay, code: err });
        } else {
          this.scheduleReconnect({ longDelay: true, code: err });
        }
      }
    });

    if (this.events.onUpsert) {
      this.sock.ev.on("messages.upsert", this.events.onUpsert);
    }
    if (this.events.onHistorySet) {
      this.sock.ev.on("messaging-history.set", this.events.onHistorySet as any);
    }
  }

  private async handleLoggedOut(): Promise<void> {
    try {
      await rm(this.authDir, { recursive: true, force: true });
      logger.warn({ authDir: this.authDir }, "auth dir cleared after logout; auto-restarting for fresh QR");
    } catch (e) {
      logger.error({ err: String(e) }, "failed to clear auth dir");
    }
    // Clears BindStore so dashboard doesn't show stale bound_chat_jid after
    // a snapshot-leak recovery.
    if (this.events.onLoggedOut) {
      try {
        this.events.onLoggedOut();
      } catch (e) {
        logger.warn({ err: String(e) }, "onLoggedOut handler failed");
      }
    }
    // Self-heal: restart so Baileys emits a fresh QR (otherwise logged_out
    // is terminal until systemctl restart).
    if (this.shuttingDown) return;
    this.teardownSocket();
    this.latestQr = null;
    this.selfE164 = null;
    this.state = "starting";
    this.reconnectAttempt = 0;
    this.connect().catch((e) => {
      logger.error({ err: String(e) }, "reconnect after logout failed");
      this.scheduleReconnect({ longDelay: true });
    });
  }

  private scheduleReconnect(opts: { longDelay?: boolean; code?: number | null } = {}): void {
    if (this.reconnectTimer) return;
    this.reconnectAttempt += 1;
    const delay =
      opts.code === DisconnectReason.connectionReplaced
        ? REPLACED_RECONNECT_MS
        : Math.min(
            30_000,
            (opts.longDelay ? 5000 : 1000) *
              Math.pow(2, Math.min(this.reconnectAttempt - 1, 5)),
          );
    logger.info(
      { attempt: this.reconnectAttempt, delay, code: opts.code ?? null },
      "scheduling reconnect",
    );
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect().catch((e) => {
        logger.error({ err: String(e) }, "reconnect failed");
        if (!this.shuttingDown) this.scheduleReconnect({ longDelay: true });
      });
    }, delay);
  }
}

function parseSelfE164(id: string | null): string | null {
  if (!id) return null;
  // "15551234567:42@s.whatsapp.net" or "15551234567@s.whatsapp.net"
  const head = id.split("@")[0] ?? "";
  const digits = head.split(":")[0] ?? "";
  return /^\d+$/.test(digits) ? digits : null;
}
