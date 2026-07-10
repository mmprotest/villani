import fs from "node:fs/promises";
import path from "node:path";
import { getGitInfo } from "../git/gitInfo.js";
import { renderDashboard } from "./dashboard.js";
import { replayRoot, safeSegment } from "../utils/paths.js";
import { redactDeep } from "../redaction/redact.js";
export async function renderReplay(session, opts = {}) {
    const cwd = opts.cwd ?? process.cwd();
    const defaultName = `${new Date().toISOString().replace(/[:.]/g, "-")}-${safeSegment(session.provider)}`;
    const requestedOut = opts.out ? path.resolve(cwd, opts.out) : undefined;
    const htmlOut = requestedOut?.toLowerCase().endsWith(".html");
    const out = requestedOut
        ? htmlOut
            ? path.dirname(requestedOut)
            : requestedOut
        : path.join(replayRoot(cwd), defaultName);
    await fs.mkdir(out, { recursive: true });
    const s = opts.redact === false ? session : redactDeep(session);
    const git = await getGitInfo(cwd);
    const file = requestedOut && htmlOut ? requestedOut : path.join(out, "index.html");
    const html = renderDashboard(s, opts.redact === false ? git : redactDeep(git), { returnHref: opts.returnHref, returnLabel: opts.returnLabel });
    await fs.writeFile(file, html, "utf8");
    return file;
}
