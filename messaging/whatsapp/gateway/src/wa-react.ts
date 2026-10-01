// wa-react.ts — thin Baileys reaction wrapper.
//
// Sends a single reaction by replacing the bot's existing reaction on the
// target message (Baileys allows one reaction per emitter — sending a new
// emoji with the same key replaces the previous one). Empty `emoji` removes
// the reaction; callers that want a "skip" semantic should not call this at
// all rather than passing an empty string.

import type { WASocket } from "baileys";

export async function sendReaction(
  sock: WASocket,
  remoteJid: string,
  messageId: string,
  fromMe: boolean,
  emoji: string,
  participant?: string | null,
): Promise<void> {
  const key: { remoteJid: string; id: string; fromMe: boolean; participant?: string } = {
    remoteJid,
    id: messageId,
    fromMe,
  };
  // Group reactions only deliver when the original sender's participant JID
  // is on the key. DMs ignore the field.
  if (participant) key.participant = participant;
  await sock.sendMessage(remoteJid, { react: { text: emoji, key } });
}
