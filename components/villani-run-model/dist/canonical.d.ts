import type { CanonicalRunSnapshot, RunDetail } from "./types.js";
/**
 * Produce the lossless, presentation-neutral run truth consumed by both browser
 * surfaces. Missing values remain null; the UI must never invent telemetry.
 */
export declare function canonicalRunSnapshot(detail: RunDetail): CanonicalRunSnapshot;
