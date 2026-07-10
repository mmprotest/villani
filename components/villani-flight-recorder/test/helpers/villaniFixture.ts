import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { resolveVillaniRepositoryRoot } from "../../src/providers/villaniSchemaValidation.js";

export const canonicalVillaniFixture = () =>
  path.join(
    resolveVillaniRepositoryRoot(),
    "integration",
    "fixtures",
    "protocol",
    "v1",
    "valid_run",
  );

export async function populateVillaniFixture(): Promise<string> {
  const target = path.resolve(
    "test",
    "fixtures",
    "villani",
    "run_protocol_fixture",
  );
  await fs.rm(target, { recursive: true, force: true });
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.cp(canonicalVillaniFixture(), target, { recursive: true });
  return target;
}

export async function copyVillaniFixture(): Promise<{
  root: string;
  run: string;
}> {
  const generated = await populateVillaniFixture();
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-villani-runs-"));
  const run = path.join(root, "run_protocol_fixture");
  await fs.cp(generated, run, { recursive: true });
  return { root, run };
}

export async function snapshotRunFiles(run: string): Promise<string[]> {
  const rows: string[] = [];
  const visit = async (directory: string) => {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      const child = path.join(directory, entry.name);
      if (entry.isDirectory()) await visit(child);
      else if (entry.isFile()) {
        const stat = await fs.stat(child);
        rows.push(
          `${path.relative(run, child).replaceAll(path.sep, "/")}:${stat.size}:${stat.mtimeMs}`,
        );
      }
    }
  };
  await visit(run);
  return rows.sort();
}
