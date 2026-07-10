import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { SessionIndex } from "./sessionTypes.js";
export const defaultIndexDir = () =>
  process.env.VFR_HOME || path.join(os.homedir(), ".villani-flight-recorder");
export const indexPath = (dir?: string) =>
  path.join(dir || defaultIndexDir(), "index.json");
export async function readIndex(dir?: string): Promise<SessionIndex | null> {
  try {
    return JSON.parse(
      await fs.readFile(indexPath(dir), "utf8"),
    ) as SessionIndex;
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw e;
  }
}
export async function writeIndex(index: SessionIndex, dir?: string) {
  const p = indexPath(dir);
  await fs.mkdir(path.dirname(p), { recursive: true });
  const tmp = `${p}.${process.pid}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(index, null, 2));
  await fs.rename(tmp, p);
  return p;
}
