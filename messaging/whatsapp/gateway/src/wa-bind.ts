// wa-bind.ts — single-chat binding persistence for WhatsApp Ninja mode.
//
// One JSON file lives next to the Baileys auth dir (e.g.
// auth/default/bound.json). It records the chat JID the agent is allowed
// to receive from. When `bound.json` is absent the gateway runs in
// "unbound" mode and behaves exactly like the SN-3408 POC (no inbound
// filter). The Ninja runtime + dashboard pre-seed this file via
// `POST /bind`, or it gets populated automatically by the pairing-code
// flow in wa-inbound.ts.
//
// Format:
//   { "chat_jid": "1234567890@s.whatsapp.net",
//     "bound_at": 1733349900000,
//     "bound_via": "pairing_code" | "api" | "install" }

import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import pino from "pino";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "info" }).child({ mod: "wa-bind" });

export type BindSource = "pairing_code" | "api" | "install" | "auto_group";

export interface BoundState {
  chat_jid: string;
  bound_at: number;
  bound_via: BindSource;
}

function boundFilePath(authDir: string): string {
  return path.join(path.resolve(authDir), "bound.json");
}

async function loadBoundState(authDir: string): Promise<BoundState | null> {
  const file = boundFilePath(authDir);
  try {
    const raw = await readFile(file, "utf-8");
    const parsed = JSON.parse(raw) as Partial<BoundState>;
    const jid = (parsed.chat_jid ?? "").toString().trim();
    if (!jid) return null;
    return {
      chat_jid: jid,
      bound_at: Number(parsed.bound_at) || Date.now(),
      bound_via: (parsed.bound_via as BindSource) || "api",
    };
  } catch (e: any) {
    if (e?.code === "ENOENT") return null;
    logger.warn({ err: String(e), file }, "failed to read bound.json");
    return null;
  }
}

async function saveBoundState(authDir: string, state: BoundState): Promise<void> {
  const file = boundFilePath(authDir);
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, JSON.stringify(state, null, 2), "utf-8");
  logger.info({ event: "bound_saved", bound_via: state.bound_via }, "binding persisted");
}

async function clearBoundState(authDir: string): Promise<void> {
  const file = boundFilePath(authDir);
  try {
    await rm(file, { force: true });
    logger.info({ event: "bound_cleared" }, "binding cleared");
  } catch (e) {
    logger.warn({ err: String(e), file }, "failed to clear bound.json");
  }
}

/** Mutable in-memory binding shared by the inbound handler and HTTP routes. */
export class BindStore {
  private authDir: string;
  private state: BoundState | null = null;

  constructor(authDir: string) {
    this.authDir = authDir;
  }

  async load(): Promise<void> {
    this.state = await loadBoundState(this.authDir);
    if (this.state) {
      logger.info({ chat_jid_kind: jidKind(this.state.chat_jid) }, "loaded existing binding");
    }
  }

  get(): BoundState | null {
    return this.state;
  }

  getChatJid(): string | null {
    return this.state?.chat_jid ?? null;
  }

  async set(chatJid: string, via: BindSource): Promise<BoundState> {
    const next: BoundState = {
      chat_jid: chatJid,
      bound_at: Date.now(),
      bound_via: via,
    };
    this.state = next;
    await saveBoundState(this.authDir, next);
    return next;
  }

  async clear(): Promise<void> {
    this.state = null;
    await clearBoundState(this.authDir);
  }
}

function jidKind(jid: string): "group" | "dm" | "other" {
  if (jid.endsWith("@g.us")) return "group";
  if (jid.endsWith("@s.whatsapp.net")) return "dm";
  return "other";
}
