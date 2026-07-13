import {
  canonicalRunSnapshot,
  type CanonicalRunSnapshot,
  type RunDetail,
} from "@villani/run-model";

export function deriveFlightRecorderRunModel(
  detail: RunDetail,
): CanonicalRunSnapshot {
  return canonicalRunSnapshot(detail);
}
