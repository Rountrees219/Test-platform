// Run with: npm test
//
// WhatsApp derives the media key from an HKDF info string that differs per
// media type, so decrypting a documentMessage with Audio keys cannot work.
// decryptAndPersist must therefore try Document first and only then Audio.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { describe, it } from "node:test";

const INFO = { document: "WhatsApp Document Keys", audio: "WhatsApp Audio Keys" } as const;

function hkdf(key: Buffer, length: number, info: string): Buffer {
  const prk = crypto.createHmac("sha256", Buffer.alloc(32)).update(key).digest();
  let prev = Buffer.alloc(0);
  let out = Buffer.alloc(0);
  let i = 1;
  while (out.length < length) {
    prev = crypto
      .createHmac("sha256", prk)
      .update(Buffer.concat([prev, Buffer.from(info, "utf-8"), Buffer.from([i++])]))
      .digest();
    out = Buffer.concat([out, prev]);
  }
  return out.subarray(0, length);
}

function mediaKeys(mediaKey: Buffer, type: keyof typeof INFO) {
  const expanded = hkdf(mediaKey, 112, INFO[type]);
  return { iv: expanded.subarray(0, 16), cipherKey: expanded.subarray(16, 48) };
}

function encryptAs(type: keyof typeof INFO, mediaKey: Buffer, plain: Buffer): Buffer {
  const { iv, cipherKey } = mediaKeys(mediaKey, type);
  const c = crypto.createCipheriv("aes-256-cbc", cipherKey, iv);
  return Buffer.concat([c.update(plain), c.final()]);
}

function decryptAs(type: keyof typeof INFO, mediaKey: Buffer, enc: Buffer): Buffer {
  const { iv, cipherKey } = mediaKeys(mediaKey, type);
  const d = crypto.createDecipheriv("aes-256-cbc", cipherKey, iv);
  return Buffer.concat([d.update(enc), d.final()]);
}

describe("media HKDF key types", () => {
  const mediaKey = crypto.randomBytes(32);
  const plain = crypto.randomBytes(64 * 1024);

  it("document bytes decrypt with Document keys", () => {
    const enc = encryptAs("document", mediaKey, plain);
    assert.deepEqual(decryptAs("document", mediaKey, enc), plain);
  });

  it("document bytes fail with Audio keys — the mp3-as-document regression", () => {
    const enc = encryptAs("document", mediaKey, plain);
    assert.throws(() => decryptAs("audio", mediaKey, enc), /bad decrypt/);
  });

  it("audio bytes fail with Document keys — why the Audio fallback is kept", () => {
    const enc = encryptAs("audio", mediaKey, plain);
    assert.throws(() => decryptAs("document", mediaKey, enc), /bad decrypt/);
  });
});
