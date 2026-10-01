// wa-pairing.ts — single-use pairing code for Ninja mode binding.
//
// Flow:
//   1. Gateway reaches connection==open and has no current binding.
//   2. PairingCode.issue() generates a fresh 6-char alphanumeric code
//      (excluding ambiguous chars like 0/O/1/I) with a 10-min expiry.
//   3. /pairing_code returns the code.
//   4. Operator sends the code as a normal WhatsApp text from any chat.
//   5. wa-inbound.handleOne() calls match(text) — on a hit it binds
//      that remoteJid via BindStore and clears the code.
//
// Codes are case-insensitive on input; the canonical form is uppercase.
// We never log the code body — only its length and {issued, matched}.

import { randomInt } from "node:crypto";
import pino from "pino";

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL ?? "info" }).child({ mod: "wa-pairing" });

const ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // no 0/O/1/I
const CODE_LEN = 6;
const TTL_MS = 10 * 60 * 1000; // 10 minutes

export interface PairingCodeSnapshot {
  code: string | null;
  expires_at: number | null;
  issued_at: number | null;
}

export class PairingCode {
  private code: string | null = null;
  private issuedAt = 0;

  /** Generate a fresh code if none active. Returns the active code. */
  issue(): string {
    if (this.code && !this.isExpired()) return this.code;
    const buf: string[] = [];
    for (let i = 0; i < CODE_LEN; i++) {
      buf.push(ALPHABET[randomInt(ALPHABET.length)]!);
    }
    this.code = buf.join("");
    this.issuedAt = Date.now();
    logger.info({ event: "pairing_code_issued", len: this.code.length }, "issued");
    return this.code;
  }

  /** Drop the current code (after bind succeeds, or on /unbind). */
  clear(): void {
    if (this.code) {
      logger.info({ event: "pairing_code_cleared" }, "cleared");
    }
    this.code = null;
    this.issuedAt = 0;
  }

  /** True iff text matches the active, non-expired code. Case-insensitive, trims whitespace. */
  match(text: string): boolean {
    if (!this.code) return false;
    if (this.isExpired()) {
      this.clear();
      return false;
    }
    const cleaned = (text ?? "").toString().trim().toUpperCase();
    return cleaned === this.code;
  }

  snapshot(): PairingCodeSnapshot {
    if (!this.code || this.isExpired()) {
      return { code: null, expires_at: null, issued_at: null };
    }
    return {
      code: this.code,
      expires_at: this.issuedAt + TTL_MS,
      issued_at: this.issuedAt,
    };
  }

  private isExpired(): boolean {
    if (!this.code) return true;
    return Date.now() - this.issuedAt > TTL_MS;
  }
}
