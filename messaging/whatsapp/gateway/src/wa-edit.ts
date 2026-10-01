// wa-edit.ts — edit or delete a message this gateway sent (the live progress bar).
//
// Callers address a message by id alone; the key and timestamp Baileys needs
// are remembered here at send time.

import { areJidsSameUser, type WAMessage, type WAMessageKey, type WASocket } from "baileys";

const SENT_CAP = 50;
const sent = new Map<string, { key: WAMessageKey; timestamp: number }>();

export function rememberSent(msg: WAMessage | undefined): void {
  const id = msg?.key?.id;
  if (!id) return;
  sent.set(id, { key: msg.key, timestamp: Number(msg.messageTimestamp ?? 0) });
  if (sent.size > SENT_CAP) sent.delete(sent.keys().next().value!);
}

export async function editSent(sock: WASocket, id: string, text: string): Promise<boolean> {
  const rec = sent.get(id);
  if (!rec) return false;
  await sock.sendMessage(rec.key.remoteJid!, { text, edit: rec.key });
  return true;
}

// Delete for me first, then for everyone. The other way round leaves a
// "You deleted this message" tombstone that a later delete-for-me won't clear.
export async function deleteSent(sock: WASocket, id: string): Promise<boolean> {
  const rec = sent.get(id);
  if (!rec) return false;
  const jid = rec.key.remoteJid!;
  await sock.chatModify(
    { deleteForMe: { deleteMedia: false, key: rec.key, timestamp: rec.timestamp } },
    jid,
  );
  if (await othersCanSee(sock, jid)) await sock.sendMessage(jid, { delete: rec.key });
  sent.delete(id);
  return true;
}

async function othersCanSee(sock: WASocket, jid: string): Promise<boolean> {
  if (jid.endsWith("@g.us")) return (await sock.groupMetadata(jid)).participants.length > 1;
  return !areJidsSameUser(jid, sock.user?.id) && !areJidsSameUser(jid, sock.user?.lid);
}
