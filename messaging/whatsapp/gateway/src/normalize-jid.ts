// normalize-jid.ts — JID <-> E.164 helpers.

const DM_SUFFIX = "@s.whatsapp.net";
const GROUP_SUFFIX = "@g.us";

export function phoneToJid(e164: string): string {
  const digits = e164.replace(/\D/g, "");
  if (!digits) throw new Error("phoneToJid: empty digits");
  return `${digits}${DM_SUFFIX}`;
}

export function jidToPhone(jid: string): string | null {
  if (!jid) return null;
  const at = jid.indexOf("@");
  const head = at >= 0 ? jid.slice(0, at) : jid;
  const digits = head.split(":")[0] ?? "";
  return /^\d+$/.test(digits) ? digits : null;
}

export function isGroupJid(jid: string): boolean {
  return jid.endsWith(GROUP_SUFFIX);
}

export function groupLocalPart(groupJid: string): string {
  if (!isGroupJid(groupJid)) throw new Error("groupLocalPart: not a group JID");
  return groupJid.slice(0, -GROUP_SUFFIX.length);
}

// conversation_id (Baileys-native shape):
//   DM:    {self_e164}:{peer_e164}
//   Group: {self_e164}:g:{group_local_part}
export function conversationIdForDm(selfE164: string, peerE164: string): string {
  return `${selfE164}:${peerE164}`;
}

export function conversationIdForGroup(selfE164: string, groupJid: string): string {
  return `${selfE164}:g:${groupLocalPart(groupJid)}`;
}

/** Accept full group JID or bare local part; reject DM JIDs. */
export function normalizeGroupJid(input: string): string {
  const raw = (input ?? "").trim();
  if (!raw) throw new Error("normalizeGroupJid: empty input");
  if (raw.endsWith(DM_SUFFIX)) throw new Error("normalizeGroupJid: DM JID not allowed");
  if (isGroupJid(raw)) return raw;
  if (raw.includes("@")) throw new Error("normalizeGroupJid: invalid JID");
  const local = raw.replace(/\D/g, "") || raw;
  if (!local) throw new Error("normalizeGroupJid: empty local part");
  return `${local}${GROUP_SUFFIX}`;
}

/** Parse `{self}:g:{local}` → `{local}@g.us`. */
export function conversationIdToGroupJid(conversationId: string): string | null {
  const parts = (conversationId ?? "").split(":");
  if (parts.length !== 3 || parts[1] !== "g") return null;
  const local = parts[2]?.trim();
  if (!local) return null;
  try {
    return normalizeGroupJid(local);
  } catch {
    return null;
  }
}
