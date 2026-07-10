import { createReadStream } from "node:fs";
import { createInterface } from "node:readline";
export interface JsonlRecord {
  line: number;
  value?: unknown;
  raw: string;
  error?: string;
}
export async function readJsonl(file: string): Promise<JsonlRecord[]> {
  const out: JsonlRecord[] = [];
  const rl = createInterface({
    input: createReadStream(file, { encoding: "utf8" }),
    crlfDelay: Infinity,
  });
  let n = 0;
  for await (const line of rl) {
    n++;
    if (!line.trim()) continue;
    try {
      out.push({ line: n, value: JSON.parse(line), raw: line });
    } catch (e) {
      out.push({
        line: n,
        raw: line,
        error: e instanceof Error ? e.message : String(e),
      });
    }
  }
  return out;
}
