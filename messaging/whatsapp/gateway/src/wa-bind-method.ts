// wa-bind-method.ts — operator-selectable bind method.
//
// Resolution order: in-memory override > WHATSAPP_BIND_METHOD env > default.
// The override is intentionally process-local; a gateway restart resets
// it to the env value. Dashboard dropdown writes the override via
// POST /bind_method.

export type BindMethod = "auto_group" | "pairing_code";
export type BindMethodSource = "override" | "env" | "default";

const VALID: ReadonlySet<BindMethod> = new Set<BindMethod>(["auto_group", "pairing_code"]);

function parseMethod(raw: string | undefined | null): BindMethod | null {
  if (!raw) return null;
  const v = raw.trim().toLowerCase();
  return VALID.has(v as BindMethod) ? (v as BindMethod) : null;
}

export class BindMethodConfig {
  private envValue: BindMethod | null;
  private override: BindMethod | null = null;

  constructor(envRaw: string | undefined = process.env.WHATSAPP_BIND_METHOD) {
    this.envValue = parseMethod(envRaw);
  }

  /** Resolved active method. */
  get(): BindMethod {
    return this.override ?? this.envValue ?? "auto_group";
  }

  source(): BindMethodSource {
    if (this.override) return "override";
    if (this.envValue) return "env";
    return "default";
  }

  setOverride(raw: string | null): BindMethod {
    if (raw === null) {
      this.override = null;
      return this.get();
    }
    const parsed = parseMethod(raw);
    if (!parsed) throw new Error(`invalid bind method: ${raw}`);
    this.override = parsed;
    return parsed;
  }
}
