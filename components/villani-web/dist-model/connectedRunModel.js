import { canonicalRunSnapshot, } from "@villani/run-model";
export function deriveVillaniWebRunModel(detail) {
    return canonicalRunSnapshot(detail);
}
