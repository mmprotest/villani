import { spawn } from "node:child_process";
export function openBrowser(file: string) {
  const cmd =
    process.platform === "win32"
      ? "cmd"
      : process.platform === "darwin"
        ? "open"
        : "xdg-open";
  const args =
    process.platform === "win32" ? ["/c", "start", "", file] : [file];
  spawn(cmd, args, { detached: true, stdio: "ignore" }).unref();
}
