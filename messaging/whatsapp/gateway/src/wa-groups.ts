// wa-groups.ts — list participating WhatsApp groups via Baileys.

import pino from "pino";
import type { WASocket } from "baileys";
import { conversationIdForGroup } from "./normalize-jid.js";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-groups" });

export interface GroupListItem {
  group_jid: string;
  subject: string;
  participant_count: number;
  conversation_id: string | null;
}

export async function listParticipatingGroups(
  sock: WASocket | null,
  selfE164: string | null,
): Promise<GroupListItem[]> {
  if (!sock) throw new Error("socket not ready");
  const raw = await sock.groupFetchAllParticipating();
  const items: GroupListItem[] = [];
  for (const [jid, meta] of Object.entries(raw ?? {})) {
    const groupJid = (meta as { id?: string } | undefined)?.id ?? jid;
    const subject = (meta as { subject?: string } | undefined)?.subject ?? groupJid;
    const participants = (meta as { participants?: unknown[] } | undefined)?.participants;
    const participant_count = Array.isArray(participants) ? participants.length : 0;
    const conversation_id = selfE164 ? conversationIdForGroup(selfE164, groupJid) : null;
    items.push({ group_jid: groupJid, subject, participant_count, conversation_id });
  }
  items.sort((a, b) => a.subject.localeCompare(b.subject));
  logger.info({ event: "group_list", count: items.length }, "listed groups");
  return items;
}
