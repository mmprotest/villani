import { readFileSync } from "node:fs";
export function readVillaniV2Json(path) {
    return JSON.parse(readFileSync(path, "utf8"));
}
