import fs from "node:fs/promises";
import crypto from "node:crypto";
import path from "node:path";

import {
  resolveVillaniRepositoryRoot,
  VILLANI_SCHEMA_FILE_BY_VERSION,
  VILLANI_V2_SCHEMA_FILE_BY_VERSION,
} from "../../src/providers/villaniSchemaValidation.js";
import { testResources } from "./testResources.js";

export const canonicalVillaniFixture = () =>
  path.join(
    resolveVillaniRepositoryRoot(),
    "integration",
    "fixtures",
    "protocol",
    "v1",
    "valid_run",
  );

interface FixtureFile {
  relativePath: string;
  content: Buffer;
}

let canonicalFixtureFiles: Promise<FixtureFile[]> | undefined;

const supportedProtocolVersions = new Set([
  ...Object.keys(VILLANI_SCHEMA_FILE_BY_VERSION),
  ...Object.keys(VILLANI_V2_SCHEMA_FILE_BY_VERSION),
]);

function isSupportedProtocolDocument(
  value: unknown,
): value is Record<string, unknown> & { schema_version: string } {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    "schema_version" in value &&
    typeof value.schema_version === "string" &&
    supportedProtocolVersions.has(value.schema_version)
  );
}

async function loadCanonicalFixtureFiles(): Promise<FixtureFile[]> {
  const root = canonicalVillaniFixture();
  const files: FixtureFile[] = [];
  const visit = async (directory: string) => {
    const entries = await fs.readdir(directory, { withFileTypes: true });
    for (const entry of entries.sort((left, right) =>
      left.name.localeCompare(right.name),
    )) {
      const child = path.join(directory, entry.name);
      if (entry.isDirectory()) await visit(child);
      else if (entry.isFile())
        files.push({
          relativePath: path.relative(root, child),
          content: await fs.readFile(child),
        });
    }
  };
  await visit(root);
  return files;
}

export function immutableCanonicalFixtureFiles(): Promise<FixtureFile[]> {
  canonicalFixtureFiles ??= loadCanonicalFixtureFiles();
  return canonicalFixtureFiles;
}

export async function materializeVillaniFixture(run: string): Promise<void> {
  for (const file of await immutableCanonicalFixtureFiles()) {
    const destination = path.join(run, file.relativePath);
    await fs.mkdir(path.dirname(destination), { recursive: true });
    await fs.writeFile(destination, file.content);
  }
}

export async function copyVillaniFixture(): Promise<{
  root: string;
  run: string;
}> {
  const root = await testResources.temporaryDirectory("vfr-villani-runs-");
  const run = path.join(root, "run_protocol_fixture");
  await materializeVillaniFixture(run);
  return { root, run };
}

export async function digestRunFiles(run: string): Promise<string[]> {
  const rows: string[] = [];
  const visit = async (directory: string) => {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      const child = path.join(directory, entry.name);
      if (entry.isDirectory()) await visit(child);
      else if (entry.isFile()) {
        const content = await fs.readFile(child);
        rows.push(
          `${path.relative(run, child).replaceAll(path.sep, "/")}:${content.length}:${crypto.createHash("sha256").update(content).digest("hex")}`,
        );
      }
    }
  };
  await visit(run);
  return rows.sort();
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

export async function readProtocolDocuments(run: string): Promise<unknown[]> {
  const documents: unknown[] = [];
  const add = (value: unknown) => {
    if (isSupportedProtocolDocument(value)) documents.push(value);
  };
  const visit = async (directory: string) => {
    const entries = await fs.readdir(directory, { withFileTypes: true });
    for (const entry of entries.sort((left, right) =>
      left.name.localeCompare(right.name),
    )) {
      const child = path.join(directory, entry.name);
      if (entry.isDirectory()) await visit(child);
      else if (entry.isFile() && entry.name.endsWith(".json"))
        add(JSON.parse(await fs.readFile(child, "utf8")) as unknown);
      else if (entry.isFile() && entry.name.endsWith(".jsonl")) {
        const lines = (await fs.readFile(child, "utf8")).split(/\r?\n/);
        for (const line of lines) if (line.trim()) add(JSON.parse(line));
      }
    }
  };
  await visit(run);
  return documents;
}

export async function parseCanonicalProtocolDocuments(): Promise<unknown[]> {
  const documents: unknown[] = [];
  const add = (value: unknown) => {
    if (isSupportedProtocolDocument(value)) documents.push(value);
  };
  for (const file of await immutableCanonicalFixtureFiles()) {
    const text = file.content.toString("utf8");
    if (file.relativePath.endsWith(".json")) add(JSON.parse(text) as unknown);
    else if (file.relativePath.endsWith(".jsonl"))
      for (const line of text.split(/\r?\n/))
        if (line.trim()) add(JSON.parse(line));
  }
  return documents;
}
