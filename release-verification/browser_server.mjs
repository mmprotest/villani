import { createServer } from "node:http";
import { readFile, stat, writeFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { Readable } from "node:stream";

import { renderConnectedReplay } from "../components/villani-flight-recorder/dist/render/connectedReplay.js";

const entries = process.argv.slice(2).reduce((result, item, index, values) => {
  if (item.startsWith("--")) result[item.slice(2)] = values[index + 1];
  return result;
}, {});

const webRoot = resolve(entries["web-root"]);
const controlPlane = String(entries["control-plane"]).replace(/\/$/, "");
const token = String(entries.token);
const port = Number(entries.port || 0);
const endpointFile = resolve(entries["endpoint-file"]);

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

async function api(pathname) {
  const response = await fetch(`${controlPlane}${pathname}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`Control Plane ${pathname} returned ${response.status}`);
  return response.json();
}

async function proxy(request, response, target) {
  const headers = { ...request.headers, authorization: `Bearer ${token}` };
  delete headers.host;
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body: chunks.length ? Buffer.concat(chunks) : undefined,
    duplex: chunks.length ? "half" : undefined,
  });
  response.statusCode = upstream.status;
  upstream.headers.forEach((value, name) => {
    if (!["content-encoding", "content-length", "transfer-encoding"].includes(name.toLowerCase())) {
      response.setHeader(name, value);
    }
  });
  if (!upstream.body) return response.end();
  Readable.fromWeb(upstream.body).pipe(response);
}

async function staticFile(pathname, response) {
  const requested = pathname === "/" ? "index.html" : decodeURIComponent(pathname.slice(1));
  const candidate = resolve(webRoot, requested);
  const contained = candidate === webRoot || candidate.startsWith(`${webRoot}${sep}`);
  let file = contained ? candidate : resolve(webRoot, "index.html");
  try {
    if (!(await stat(file)).isFile()) file = resolve(webRoot, "index.html");
  } catch {
    file = resolve(webRoot, "index.html");
  }
  const body = await readFile(file);
  response.writeHead(200, {
    "Content-Type": contentTypes[extname(file)] || "application/octet-stream",
    "Cache-Control": "no-store",
  });
  response.end(body);
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url || "/", "http://127.0.0.1");
    if (url.pathname === "/__release/health") {
      response.writeHead(200, { "Content-Type": "application/json" });
      return response.end('{"status":"ok"}');
    }
    if (url.pathname.startsWith("/v1/")) {
      return await proxy(request, response, `${controlPlane}${url.pathname}${url.search}`);
    }
    const flight = url.pathname.match(/^\/flight\/runs\/([^/]+)$/);
    if (flight) {
      const runId = decodeURIComponent(flight[1]);
      const [detail, eventPage, artifactPage] = await Promise.all([
        api(`/v1/runs/${encodeURIComponent(runId)}`),
        api(`/v1/runs/${encodeURIComponent(runId)}/events?limit=1000`),
        api(`/v1/runs/${encodeURIComponent(runId)}/artifacts?limit=250`),
      ]);
      const html = renderConnectedReplay(
        detail,
        Array.isArray(eventPage.events) ? eventPage.events : [],
        Array.isArray(artifactPage.artifacts) ? artifactPage.artifacts : [],
      );
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
      return response.end(html);
    }
    return await staticFile(url.pathname, response);
  } catch (error) {
    response.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
    response.end(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }));
  }
});

server.listen(port, "127.0.0.1", async () => {
  const address = server.address();
  const actualPort = typeof address === "object" && address ? address.port : port;
  await writeFile(endpointFile, JSON.stringify({ base_url: `http://127.0.0.1:${actualPort}` }), "utf8");
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
