import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
export const defaultIndexDir = () => process.env.VFR_HOME || path.join(os.homedir(), ".villani-flight-recorder");
export const indexPath = (dir) => path.join(dir || defaultIndexDir(), "index.json");
export async function readIndex(dir) {
    try {
        return JSON.parse(await fs.readFile(indexPath(dir), "utf8"));
    }
    catch (e) {
        if (e.code === "ENOENT")
            return null;
        throw e;
    }
}
export async function writeIndex(index, dir) {
    const p = indexPath(dir);
    await fs.mkdir(path.dirname(p), { recursive: true });
    const tmp = `${p}.${process.pid}.tmp`;
    await fs.writeFile(tmp, JSON.stringify(index, null, 2));
    await fs.rename(tmp, p);
    return p;
}
