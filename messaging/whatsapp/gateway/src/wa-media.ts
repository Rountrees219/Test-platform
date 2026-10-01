// wa-media.ts — voice/image/pdf decrypt + storage + extraction.
//
// Inbound media (voice notes, images, PDF documents) live encrypted on
// WhatsApp's servers; Baileys' downloadMediaMessage decrypts the bytes
// using the per-message mediaKey. We persist the decrypted bytes locally
// (0600, sha256-keyed filename), surface a stable `media_id` on the
// inbox record, and serve the bytes via GET /media/:id. The agent
// transcribes voice (Whisper) and reads images/PDFs (Claude native).

import pino from "pino";
import { mkdir, writeFile, unlink, chmod } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import {
  downloadContentFromMessage,
  downloadMediaMessage,
  extractMessageContent,
} from "baileys";
import type { proto } from "baileys";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "warn" }).child({ mod: "wa-media" });

export type MediaKind = "voice" | "image" | "pdf" | "archive" | "text" | "other";

export interface MediaPayload {
  kind: MediaKind;
  mimetype: string;
  seconds: number | null;   // voice only
  caption: string | null;   // image / pdf / video / other
  filename: string | null;  // pdf / other
}

export interface MediaRecord {
  media_id: string;        // sha256 prefix (16 hex chars)
  path: string;
  mimetype: string;
  bytes: number;
  kind: MediaKind;
  ts: number;              // unix ms — for TTL sweep
}

export class MediaTooLargeError extends Error {}
export class MediaDecryptError extends Error {}

const EXT_BY_MIME: Record<string, string> = {
  "audio/ogg": "ogg",
  "audio/ogg; codecs=opus": "ogg",
  "audio/mpeg": "mp3",
  "audio/mp4": "m4a",
  "audio/aac": "aac",
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
  "video/mp4": "mp4",
  "video/webm": "webm",
  "video/quicktime": "mov",
  "application/pdf": "pdf",
  "application/zip": "zip",
  "application/x-zip-compressed": "zip",
  "application/x-zip": "zip",
  "application/json": "json",
  "application/xml": "xml",
  "application/yaml": "yaml",
  "application/x-yaml": "yaml",
  "application/javascript": "js",
  "application/typescript": "ts",
  "application/x-shellscript": "sh",
  "application/x-python": "py",
  "application/sql": "sql",
};

const TEXT_APPLICATION_MIMES = new Set<string>([
  "application/json",
  "application/xml",
  "application/yaml",
  "application/x-yaml",
  "application/javascript",
  "application/typescript",
  "application/x-shellscript",
  "application/x-python",
  "application/sql",
]);

const ARCHIVE_MIMES = new Set<string>([
  "application/zip",
  "application/x-zip-compressed",
  "application/x-zip",
]);

function classifyDocMime(mime: string): MediaKind | null {
  if (mime === "application/pdf") return "pdf";
  if (ARCHIVE_MIMES.has(mime)) return "archive";
  if (mime.startsWith("text/")) return "text";
  if (TEXT_APPLICATION_MIMES.has(mime)) return "text";
  return null;
}

function safeFilenameExt(filename: string | null | undefined): string | null {
  if (!filename) return null;
  const base = path.basename(filename);
  const dot = base.lastIndexOf(".");
  if (dot <= 0 || dot === base.length - 1) return null;
  const ext = base.slice(dot + 1).toLowerCase();
  // Reject path-like or absurd extensions.
  if (!/^[a-z0-9]{1,10}$/.test(ext)) return null;
  return ext;
}

function extFor(mimetype: string, filename?: string | null): string {
  if (EXT_BY_MIME[mimetype]) return EXT_BY_MIME[mimetype]!;
  const base = mimetype.split(";")[0]!.trim();
  if (EXT_BY_MIME[base]) return EXT_BY_MIME[base]!;
  if (base.startsWith("text/")) return "txt";
  // Prefer original filename extension when MIME is empty/octet-stream/unknown.
  const fromName = safeFilenameExt(filename);
  if (fromName) return fromName;
  return "bin";
}

export function extractMedia(m: proto.IWebMessageInfo): MediaPayload | null {
  const msg = m.message;
  if (!msg) return null;

  const audio = msg.audioMessage;
  if (audio) {
    return {
      kind: "voice",
      mimetype: audio.mimetype ?? "audio/ogg",
      seconds: typeof audio.seconds === "number" ? audio.seconds : null,
      caption: null,
      filename: null,
    };
  }

  const img = msg.imageMessage;
  if (img) {
    return {
      kind: "image",
      mimetype: img.mimetype ?? "image/jpeg",
      seconds: null,
      caption: img.caption ?? null,
      filename: null,
    };
  }

  const video = msg.videoMessage;
  if (video) {
    return {
      kind: "other",
      mimetype: video.mimetype ?? "video/mp4",
      seconds: typeof video.seconds === "number" ? video.seconds : null,
      caption: video.caption ?? null,
      filename: null,
    };
  }

  // WhatsApp wraps caption-bearing docs in `documentWithCaptionMessage`.
  // Plain (no-caption) docs arrive directly as `documentMessage`. Accept both.
  // Known mimes keep specialized kinds; everything else falls through to other.
  const doc =
    msg.documentMessage ??
    msg.documentWithCaptionMessage?.message?.documentMessage ??
    null;
  if (doc) {
    const mime = (doc.mimetype ?? "").split(";")[0]!.trim();
    const kind = classifyDocMime(mime) ?? "other";
    return {
      kind,
      mimetype: doc.mimetype ?? (mime || "application/octet-stream"),
      seconds: null,
      caption: doc.caption ?? null,
      filename: doc.fileName ?? null,
    };
  }

  return null;
}

export interface QuotedContext {
  quoted_message_key: string | null;
  quoted_text: string | null;
  quoted_sender: string | null;
}

export function extractQuoted(m: proto.IWebMessageInfo): QuotedContext {
  const msg = m.message;
  const empty: QuotedContext = { quoted_message_key: null, quoted_text: null, quoted_sender: null };
  if (!msg) return empty;

  // Baileys puts contextInfo under whichever message type carries the reply.
  // Caption-bearing PDFs are wrapped, so also probe the inner documentMessage.
  const ctx =
    msg.extendedTextMessage?.contextInfo ??
    msg.imageMessage?.contextInfo ??
    msg.documentMessage?.contextInfo ??
    msg.documentWithCaptionMessage?.message?.documentMessage?.contextInfo ??
    msg.audioMessage?.contextInfo ??
    msg.videoMessage?.contextInfo ??
    msg.stickerMessage?.contextInfo ??
    null;
  if (!ctx?.quotedMessage) return empty;

  const remoteJid = m.key?.remoteJid ?? "";
  const stanzaId = ctx.stanzaId ?? "";
  const key = remoteJid && stanzaId ? `${remoteJid}:${stanzaId}` : null;

  const q = ctx.quotedMessage;
  let raw =
    q.conversation ??
    q.extendedTextMessage?.text ??
    q.imageMessage?.caption ??
    q.documentMessage?.caption ??
    "";
  // Override #5: straight truncation at 1000 chars + ellipsis. No pointer.
  if (raw.length > 1000) raw = raw.slice(0, 1000) + "…";

  return {
    quoted_message_key: key,
    quoted_text: raw || null,
    quoted_sender: ctx.participant ?? null,
  };
}

export interface DecryptOpts {
  dir: string;
  maxBytes: number;
}

function resolveDocumentMessage(msg: proto.IMessage | null | undefined) {
  if (!msg) return null;
  const unwrapped = extractMessageContent(msg);
  const fromUnwrapped = unwrapped?.documentMessage ?? null;
  if (fromUnwrapped) return fromUnwrapped;
  return msg.documentMessage ?? msg.documentWithCaptionMessage?.message?.documentMessage ?? null;
}

/** Baileys #681: some clients encrypt an audio documentMessage with Audio HKDF. */
function isAudioDocument(m: proto.IWebMessageInfo, payload: MediaPayload): boolean {
  const mime = payload.mimetype.split(";")[0]!.trim();
  if (!mime.startsWith("audio/")) return false;
  return resolveDocumentMessage(m.message) !== null;
}

function normalizeMediaKey(media: Record<string, unknown>): Record<string, unknown> {
  const mk = media.mediaKey;
  if (!mk) return media;
  if (Buffer.isBuffer(mk)) return media;
  if (mk instanceof Uint8Array) return { ...media, mediaKey: Buffer.from(mk) };
  if (typeof mk === "object" && mk !== null && "data" in mk && Array.isArray((mk as { data: number[] }).data)) {
    return { ...media, mediaKey: Buffer.from((mk as { data: number[] }).data) };
  }
  if (typeof mk === "object" && mk !== null) {
    const vals = Object.values(mk as Record<string, number>);
    if (vals.length > 0 && vals.every((v) => typeof v === "number")) {
      return { ...media, mediaKey: Buffer.from(vals) };
    }
  }
  return media;
}

async function bufferFromDownloadStream(
  stream: AsyncIterable<Uint8Array | Buffer>,
): Promise<Buffer> {
  const parts: Buffer[] = [];
  for await (const chunk of stream) {
    parts.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(parts);
}

async function downloadDocumentAudio(msg: proto.IWebMessageInfo): Promise<Buffer> {
  const doc = resolveDocumentMessage(msg.message);
  if (!doc || !doc.url) throw new MediaDecryptError("documentMessage missing url");
  const media = normalizeMediaKey(doc as Record<string, unknown>);
  const stream = await downloadContentFromMessage(
    media as Parameters<typeof downloadContentFromMessage>[0],
    "audio",
  );
  return bufferFromDownloadStream(stream);
}

export async function decryptAndPersist(
  m: proto.IWebMessageInfo,
  payload: MediaPayload,
  opts: DecryptOpts,
): Promise<MediaRecord> {
  let buf: Buffer;
  try {
    buf = await downloadMediaMessage(
      m as Parameters<typeof downloadMediaMessage>[0],
      "buffer",
      {},
    );
  } catch (e) {
    if (!isAudioDocument(m, payload)) throw new MediaDecryptError(String(e));
    try {
      buf = await downloadDocumentAudio(m);
    } catch (audioErr) {
      throw new MediaDecryptError(`${String(e)} | audio-hkdf retry: ${String(audioErr)}`);
    }
  }

  if (buf.length > opts.maxBytes) {
    throw new MediaTooLargeError(`${buf.length} > ${opts.maxBytes}`);
  }

  const sha = createHash("sha256").update(buf).digest("hex").slice(0, 16);
  const ext = extFor(payload.mimetype, payload.filename);
  const filename = `${payload.kind}_${sha}.${ext}`;
  const fullPath = path.join(opts.dir, filename);

  await mkdir(opts.dir, { recursive: true });
  await writeFile(fullPath, buf, { mode: 0o600 });
  try { await chmod(fullPath, 0o600); } catch {}

  return {
    media_id: sha,
    path: fullPath,
    mimetype: payload.mimetype,
    bytes: buf.length,
    kind: payload.kind,
    ts: Date.now(),
  };
}

export class MediaStore {
  private map = new Map<string, MediaRecord>();

  register(rec: MediaRecord): void {
    this.map.set(rec.media_id, rec);
  }

  get(media_id: string): MediaRecord | undefined {
    return this.map.get(media_id);
  }

  async sweep(ttlMs: number): Promise<void> {
    const cutoff = Date.now() - ttlMs;
    for (const [id, rec] of this.map.entries()) {
      if (rec.ts < cutoff) {
        this.map.delete(id);
        try {
          await unlink(rec.path);
        } catch (e) {
          logger.debug({ err: String(e), path: rec.path }, "sweep unlink failed");
        }
      }
    }
  }
}
