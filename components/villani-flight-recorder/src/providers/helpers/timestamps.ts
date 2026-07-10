import { obj } from "../../normalize/events.js";
export function timestampOf(record: unknown): string | undefined {
  const o = obj(record);
  const v = o.timestamp ?? o.created_at ?? o.time ?? o.ts;
  return typeof v === "string"
    ? v
    : typeof v === "number"
      ? new Date(v).toISOString()
      : undefined;
}
