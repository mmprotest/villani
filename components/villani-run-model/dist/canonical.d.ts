import type { CanonicalRunSnapshot, RunDetail } from "./types.js";
/**
 * Produce the lossless, presentation-neutral run truth consumed by both browser
 * surfaces. Missing values remain null; the UI must never invent telemetry.
 */
export declare function canonicalRunSnapshot(detail: RunDetail): CanonicalRunSnapshot;
/**
 * Named consumer adapters keep the two application boundaries explicit while
 * guaranteeing that both surfaces use the same presentation-neutral model.
 */
export declare function deriveVillaniWebRunModel(detail: RunDetail): CanonicalRunSnapshot;
export declare function deriveFlightRecorderRunModel(detail: RunDetail): CanonicalRunSnapshot;
