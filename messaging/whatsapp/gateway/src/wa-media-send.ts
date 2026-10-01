// wa-media-send.ts — outbound image / document via Baileys sendMessage.
//
// Returns the WA message_id so the caller can register it in
// recentlySent to defeat the fromMe-notify loopback that would
// otherwise re-dispatch our own outbound media (the text-based
// loopback check doesn't fire for caption-less uploads).

import pino from "pino";
import type { WASocket } from "baileys";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-media-send" });

export type OutboundMediaKind = "image" | "document";

export interface OutboundMedia {
  kind: OutboundMediaKind;
  bytes: Buffer;
  mimetype: string;
  caption?: string;
  filename?: string;       // required for documents
}

export async function sendMedia(
  sock: WASocket,
  remoteJid: string,
  media: OutboundMedia,
): Promise<{ message_id: string | null }> {
  const caption = media.caption && media.caption.length > 0 ? media.caption : undefined;
  let payload: Parameters<WASocket["sendMessage"]>[1];
  if (media.kind === "image") {
    payload = { image: media.bytes, caption, mimetype: media.mimetype };
  } else {
    if (!media.filename) throw new Error("filename required for document upload");
    payload = {
      document: media.bytes,
      caption,
      mimetype: media.mimetype,
      fileName: media.filename,
    };
  }
  const res = await sock.sendMessage(remoteJid, payload);
  const message_id = res?.key?.id ?? null;
  logger.info(
    {
      event: "send_media",
      kind: media.kind,
      jid_type: remoteJid.endsWith("@g.us") ? "group" : "dm",
      bytes: media.bytes.length,
      has_caption: Boolean(caption),
    },
    "media sent",
  );
  return { message_id };
}
