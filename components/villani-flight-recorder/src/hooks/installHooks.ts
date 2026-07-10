import fs from "node:fs/promises";
import path from "node:path";
import { HOOK_DIR } from "../config.js";
import { safeSegment } from "../utils/paths.js";

export async function appendHook(
  provider: string,
  input: string,
  cwd = process.cwd(),
) {
  const lines = input.split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) throw new Error("No hook JSON was provided on stdin.");
  const dir = path.join(cwd, HOOK_DIR);
  await fs.mkdir(dir, { recursive: true });
  const records = lines.map((line, i) => {
    try {
      const payload = JSON.parse(line);
      if (!payload || typeof payload !== "object" || Array.isArray(payload))
        throw new Error("payload must be a JSON object");
      return payload as Record<string, unknown>;
    } catch (e) {
      throw new Error(
        `Invalid JSON on stdin line ${i + 1}: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  });
  const sid = safeSegment(
    String(
      records.find((r) => r.session_id)?.session_id ??
        new Date().toISOString().slice(0, 10),
    ),
  );
  const file = path.join(dir, `${safeSegment(provider)}-${sid}.jsonl`);
  if (!path.resolve(file).startsWith(path.resolve(dir) + path.sep))
    throw new Error("Unsafe hook output path");
  await fs.appendFile(
    file,
    records
      .map((payload) =>
        JSON.stringify({
          provider,
          receivedAt: new Date().toISOString(),
          payload,
        }),
      )
      .join("\n") + "\n",
  );
  return file;
}

export async function installHooks(cwd = process.cwd()) {
  const dir = path.join(cwd, ".villani-flight-recorder");
  await fs.mkdir(dir, { recursive: true });
  const file = path.join(dir, "hook-snippets.json");
  const snippet = {
    note: "Hook snippets written. Manual installation is required. No Claude, Codex, or Pi config files were modified.",
    claude: { command: "villani-flight-recorder hook claude" },
    codex: { command: "villani-flight-recorder hook codex" },
    pi: {
      note: "Native Pi hook installation is uncertain; pipe JSON payloads to villani-flight-recorder hook pi if supported.",
    },
  };
  await fs.writeFile(file, JSON.stringify(snippet, null, 2));
  return file;
}
