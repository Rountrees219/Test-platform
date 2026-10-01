// wa-send.ts — sendMessage + groupCreate with onWhatsApp validation.

import pino from "pino";
import type { WASocket } from "baileys";
import { phoneToJid } from "./normalize-jid.js";
import type { RecentlySent } from "./wa-inbound.js";
import { rememberSent } from "./wa-edit.js";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-send" });

export interface SendDeps {
  sock: () => WASocket | null;
  recentlySent: RecentlySent;
}

export async function sendText(deps: SendDeps, toE164: string, text: string): Promise<{ jid: string; id: string | null }> {
  const sock = deps.sock();
  if (!sock) throw new Error("socket not ready");
  const jid = phoneToJid(toE164);
  return sendTextToJid(deps, jid, text, { event: "send", to_e164: toE164 });
}

export async function sendTextToJid(
  deps: SendDeps,
  jid: string,
  text: string,
  logMeta: Record<string, unknown> = { event: "send_group" },
): Promise<{ jid: string; id: string | null }> {
  const sock = deps.sock();
  if (!sock) throw new Error("socket not ready");
  deps.recentlySent.add(jid, text);
  const res = await sock.sendMessage(jid, { text });
  rememberSent(res);
  logger.info({ ...logMeta, jid_type: jid.endsWith("@g.us") ? "group" : "dm", len: text.length }, "sent");
  return { jid, id: res?.key?.id ?? null };
}

export interface GroupCreateResult {
  group_jid: string;
  added: string[];
  skipped: string[];
}

export async function groupCreate(
  deps: SendDeps,
  subject: string,
  participantE164s: string[],
  welcome?: string,
): Promise<GroupCreateResult> {
  const sock = deps.sock();
  if (!sock) throw new Error("socket not ready");

  const added: string[] = [];
  const skipped: string[] = [];
  const addedJids: string[] = [];

  for (const e164 of participantE164s) {
    const jid = phoneToJid(e164);
    try {
      const results = await sock.onWhatsApp(jid);
      const hit = results?.find((r) => r.exists);
      if (hit) {
        added.push(e164);
        // Prefer the canonical jid returned by Baileys.
        addedJids.push(hit.jid ?? jid);
      } else {
        skipped.push(e164);
      }
    } catch (e) {
      logger.warn({ err: String(e), e164 }, "onWhatsApp lookup failed");
      skipped.push(e164);
    }
  }

  if (addedJids.length === 0) {
    throw new Error("no valid participants to add");
  }

  const meta = await sock.groupCreate(subject, addedJids);
  const group_jid = meta.id;
  logger.info({ event: "group_create", group_jid, added: added.length, skipped: skipped.length }, "group created");

  if (welcome && welcome.length > 0) {
    try {
      await sendTextToJid(deps, group_jid, welcome);
    } catch (e) {
      logger.warn({ err: String(e) }, "welcome message failed");
    }
  }

  return { group_jid, added, skipped };
}
