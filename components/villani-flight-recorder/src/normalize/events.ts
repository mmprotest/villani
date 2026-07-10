import { FlightEvent, FlightEventType, Provider } from "../providers/types.js";

export function event(
  id: string,
  provider: Provider,
  type: FlightEventType,
  title: string,
  raw?: unknown,
  extra: Partial<FlightEvent> = {},
): FlightEvent {
  return { id, provider, type, title, raw, ...extra };
}

export function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
}

export function textOf(v: unknown): string | undefined {
  if (typeof v === "string") return v;
  if (Array.isArray(v)) {
    return (
      v
        .map((x) => textOf(x))
        .filter(Boolean)
        .join("\n") || undefined
    );
  }
  const o = obj(v);
  if (Object.keys(o).length)
    return textOf(o.text ?? o.content ?? o.message ?? o.output ?? o.summary);
  return undefined;
}

export function isTestCommand(command: string): boolean {
  return /(^|\s)(npm (run )?test|pnpm test|yarn test|bun test|pytest|python -m pytest|uv run pytest|go test|cargo test|mvn test|gradle test|dotnet test|vitest|jest|rspec|bundle exec rspec)(\s|$)/i.test(
    command,
  );
}
export const isInstallCommand = (c: string) =>
  /(^|\s)(npm i|npm install|pnpm install|yarn install|bun install|pip install|uv sync|cargo fetch)(\s|$)/i.test(
    c,
  );
export const isBuildCommand = (c: string) =>
  /(^|\s)(npm run build|pnpm build|yarn build|bun run build|cargo build|go build|mvn package|gradle build|tsc)(\s|$)/i.test(
    c,
  );
export const isLintCommand = (c: string) =>
  /(^|\s)(npm run lint|pnpm lint|yarn lint|eslint|ruff|flake8|cargo clippy)(\s|$)/i.test(
    c,
  );

export function extractCommand(input: unknown): string | undefined {
  if (typeof input === "string") return input;
  const o = obj(input);
  const v = o.command ?? o.cmd ?? o.shell ?? o.bash;
  return typeof v === "string" ? v : undefined;
}

export function extractPath(input: unknown): string | undefined {
  const o = obj(input);
  const v = o.file_path ?? o.path ?? o.filename ?? o.file;
  return typeof v === "string" ? v : undefined;
}

export function extractPatchTouchedFiles(patch: string): string[] {
  const files = new Set<string>();
  for (const line of patch.split(/\r?\n/)) {
    const m =
      line.match(/^\*\*\* (?:Update|Add|Delete) File: (.+)$/) ??
      line.match(/^diff --git a\/(.*?) b\/(.*?)$/);
    if (m) files.add(m[2] ?? m[1]);
  }
  return [...files];
}

export function makeHumanEventTitle(e: FlightEvent): string {
  if (e.type === "user_message") return "User prompt";
  if (e.type === "assistant_message") return "Assistant response";
  if (e.type === "bash_command" || e.type === "test_run")
    return e.exitCode && e.exitCode !== 0
      ? `Command failed: ${e.command ?? "shell command"}`
      : `Ran ${e.command ?? "shell command"}`;
  if (e.type === "file_read") return `Read ${e.path ?? "file"}`;
  if (e.type === "file_write") return `Wrote ${e.path ?? "file"}`;
  if (e.type === "file_edit") return `Edited ${e.path ?? "file"}`;
  if (e.type === "approval") return "Approval requested";
  if (e.type === "error")
    return e.command ? `Command failed: ${e.command}` : "Error";
  if (e.type === "unknown")
    return ["generic", "unknown"].includes(String(e.provider))
      ? "Generic replay"
      : `Unknown ${e.provider[0].toUpperCase()}${e.provider.slice(1)} event`;
  return e.type.replaceAll("_", " ");
}
