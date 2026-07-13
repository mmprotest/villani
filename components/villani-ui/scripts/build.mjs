import { writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { villaniThemeCss } from "../theme-source.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
await writeFile(resolve(root, "theme.css"), `${villaniThemeCss.trim()}\n`, "utf8");
