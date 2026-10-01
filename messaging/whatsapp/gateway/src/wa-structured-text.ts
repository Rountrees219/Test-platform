// wa-structured-text.ts — synthesize inbox text for non-text WhatsApp message types.

import type { proto } from "baileys";

function unwrapMessage(msg: proto.IMessage | null | undefined): proto.IMessage | null {
  if (!msg) return null;
  const inner =
    msg.ephemeralMessage?.message ??
    msg.viewOnceMessage?.message ??
    msg.viewOnceMessageV2?.message ??
    msg.deviceSentMessage?.message ??
    null;
  if (inner) return unwrapMessage(inner);
  return msg;
}

function vcardPhone(vcard: string | null | undefined): string | null {
  if (!vcard) return null;
  const match = vcard.match(/TEL[^:]*:([+\d\s().-]+)/i);
  return match?.[1]?.trim() || null;
}

function formatCoords(
  label: string,
  lat: number | null | undefined,
  lng: number | null | undefined,
  extras: string[] = [],
): string | null {
  if (typeof lat !== "number" || typeof lng !== "number") return null;
  const lines = [`${label}: ${lat}, ${lng}`, `https://maps.google.com/?q=${lat},${lng}`, ...extras];
  return lines.filter(Boolean).join("\n");
}

function formatLocation(loc: proto.Message.ILocationMessage | null | undefined): string | null {
  if (!loc) return null;
  const extras: string[] = [];
  if (loc.name) extras.push(`Name: ${loc.name}`);
  if (loc.address) extras.push(`Address: ${loc.address}`);
  if (loc.url) extras.push(`Link: ${loc.url}`);
  return formatCoords("📍 Location shared", loc.degreesLatitude, loc.degreesLongitude, extras);
}

function formatLiveLocation(
  loc: proto.Message.ILiveLocationMessage | null | undefined,
): string | null {
  if (!loc) return null;
  const extras: string[] = [];
  if (loc.caption) extras.push(`Caption: ${loc.caption}`);
  return formatCoords("📍 Live location shared", loc.degreesLatitude, loc.degreesLongitude, extras);
}

function formatContact(contact: proto.Message.IContactMessage | null | undefined): string | null {
  if (!contact) return null;
  const name = (contact.displayName || "Contact").trim();
  const phone = vcardPhone(contact.vcard);
  return phone ? `👤 Contact shared: ${name} (${phone})` : `👤 Contact shared: ${name}`;
}

function formatContactsArray(
  contacts: proto.Message.IContactsArrayMessage | null | undefined,
): string | null {
  if (!contacts?.contacts?.length) return null;
  const rendered = contacts.contacts
    .map((c) => formatContact(c))
    .filter((line): line is string => Boolean(line));
  if (!rendered.length) return null;
  const header = contacts.displayName ? `👤 Contacts shared: ${contacts.displayName}` : "👤 Contacts shared";
  return [header, ...rendered].join("\n");
}

function formatPoll(poll: proto.Message.IPollCreationMessage | null | undefined): string | null {
  if (!poll) return null;
  const question = (poll.name || "Poll").trim();
  const options = (poll.options ?? [])
    .map((opt) => (opt.optionName || "").trim())
    .filter(Boolean);
  if (!options.length) return `📊 Poll shared: ${question}`;
  return `📊 Poll shared: ${question} — ${options.join(" | ")}`;
}

function formatEvent(event: proto.Message.IEventMessage | null | undefined): string | null {
  if (!event) return null;
  const name = (event.name || "Event").trim();
  const prefix = event.isCanceled ? "📅 Event canceled" : "📅 Event shared";
  const lines = [`${prefix}: ${name}`];
  if (event.description) lines.push(`Description: ${event.description}`);
  if (event.startTime) lines.push(`Starts: ${event.startTime}`);
  if (event.joinLink) lines.push(`Join: ${event.joinLink}`);
  const locLine = formatLocation(event.location);
  if (locLine) lines.push(locLine);
  return lines.join("\n");
}

/** Plain text plus synthesized text for location/contact/poll/event shares. */
export function extractInboundText(m: proto.IWebMessageInfo): string {
  const raw = m.message;
  const plain =
    raw?.conversation ??
    raw?.extendedTextMessage?.text ??
    "";
  if (plain) return plain;

  const msg = unwrapMessage(raw);
  if (!msg) return "";

  return (
    formatLocation(msg.locationMessage) ??
    formatLiveLocation(msg.liveLocationMessage) ??
    formatContactsArray(msg.contactsArrayMessage) ??
    formatContact(msg.contactMessage) ??
    formatPoll(
      msg.pollCreationMessage ??
        msg.pollCreationMessageV2 ??
        msg.pollCreationMessageV3 ??
        msg.pollCreationMessageV5 ??
        null,
    ) ??
    formatEvent(msg.eventMessage) ??
    ""
  );
}
