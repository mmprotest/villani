import {
  canonicalRunSnapshot,
  type CanonicalRunSnapshot,
  type RunDetail,
} from "@villani/run-model";

export function deriveVillaniWebRunModel(detail: RunDetail): CanonicalRunSnapshot {
  return canonicalRunSnapshot(detail);
}
