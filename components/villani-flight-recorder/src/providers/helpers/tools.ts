import {
  extractCommand,
  extractPath,
  isTestCommand,
} from "../../normalize/events.js";
import { FlightEventType } from "../types.js";

export function classifyTool(
  name: string,
  input: unknown,
): { type: FlightEventType; command?: string; path?: string; title?: string } {
  const lower = name.toLowerCase();
  const command = extractCommand(input);
  const path = extractPath(input);
  if (command || ["bash", "shell", "terminal"].includes(lower))
    return {
      type: command && isTestCommand(command) ? "test_run" : "bash_command",
      command,
    };
  if (["read", "view"].includes(lower)) return { type: "file_read", path };
  if (["write"].includes(lower)) return { type: "file_write", path };
  if (
    ["edit", "multiedit", "notebookedit", "apply_patch", "patch"].includes(
      lower,
    )
  )
    return { type: "file_edit", path };
  if (["grep"].includes(lower))
    return { type: "tool_call", path, title: "Searched with Grep" };
  if (["glob"].includes(lower))
    return { type: "tool_call", path, title: "Matched files with Glob" };
  return { type: "tool_call", path };
}
