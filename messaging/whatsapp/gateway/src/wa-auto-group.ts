// wa-auto-group.ts — auto-bind path: create a self-only group and return its JID.
//
// Baileys requires at least one participant in groupCreate; the self JID
// is the only "self-only" path. Owner is self by construction.

import pino from "pino";
import type { WASocket } from "baileys";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "info" }).child({ mod: "wa-auto-group" });

export interface AutoGroupResult {
  group_jid: string;
}

export async function createSelfGroup(
  sock: WASocket,
  selfJid: string,
  subject: string,
): Promise<AutoGroupResult> {
  if (!selfJid) throw new Error("self_jid_unavailable");
  const meta = await sock.groupCreate(subject, [selfJid]);
  const group_jid = meta?.id ?? "";
  if (!group_jid.endsWith("@g.us")) {
    throw new Error(`unexpected group jid: ${group_jid}`);
  }
  logger.info({ event: "auto_group_created", group_jid, subject_len: subject.length }, "self-group created");
  return { group_jid };
}

/** Fetches the invite code lazily; returns null on any failure. Never throws. */
export async function fetchInviteCode(sock: WASocket, groupJid: string): Promise<string | null> {
  try {
    const code = await sock.groupInviteCode(groupJid);
    return code && code.length > 0 ? code : null;
  } catch (e) {
    logger.warn({ err: String(e), group_jid: groupJid }, "groupInviteCode failed");
    return null;
  }
}
