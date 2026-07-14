import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const endpointPath = () =>
  path.join(
    process.env.VILLANI_HOME || path.join(os.homedir(), ".villani"),
    "agentd",
    "endpoint.json",
  );

const loopback = (hostname: string) => {
  const normalized = hostname.toLowerCase();
  return (
    normalized === "localhost" ||
    normalized === "::1" ||
    /^127(?:\.\d{1,3}){3}$/.test(normalized)
  );
};

export async function villaniConsoleUrl(
  relative = "/console",
): Promise<string> {
  let document: unknown;
  try {
    document = JSON.parse(await fs.readFile(endpointPath(), "utf8"));
  } catch {
    throw new Error("Villani Service is stopped. Run: villani service start");
  }
  const endpoint =
    document && typeof document === "object" && "endpoint" in document
      ? String((document as { endpoint: unknown }).endpoint)
      : "";
  let url: URL;
  try {
    url = new URL(endpoint);
  } catch {
    throw new Error(
      "Villani Service endpoint is invalid. Run: villani service restart",
    );
  }
  if (
    url.protocol !== "http:" ||
    !loopback(url.hostname) ||
    url.username ||
    url.password
  )
    throw new Error("Villani Service endpoint is not a safe loopback URL");
  url.pathname = relative.startsWith("/") ? relative : `/${relative}`;
  url.search = "";
  url.hash = "";
  return url.toString().replace(/\/$/, "");
}

export function consoleRedirectHtml(url: string): string {
  const safe = url
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=${safe}"><title>Villani Console compatibility link</title></head><body><p>This Flight Recorder link now opens <a href="${safe}">Villani Console</a>.</p></body></html>`;
}
