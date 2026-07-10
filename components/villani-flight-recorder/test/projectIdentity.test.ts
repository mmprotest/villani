import { describe, expect, it } from "vitest";
import {
  deriveProjectIdentity,
  meaningfulProjectName,
} from "../src/index/projectIdentity.js";

describe("project identity", () => {
  it("uses task ids instead of weak repo/sandbox labels for Windows cwd paths", () => {
    const cwd =
      "C:\\Users\\Simon\\OneDrive\\Documents\\Python Scripts\\villanibench\\artifacts\\runs\\claude-code-9b-hard-v0-2-local\\tasks\\VB-HSTATE-003\\sandbox\\repo";
    expect(meaningfulProjectName(cwd)).toBe("VB-HSTATE-003");
  });

  it("decodes Claude project paths and avoids weak labels", () => {
    const sourcePath =
      "C:\\Users\\Simon\\.claude\\projects\\C--Users-Simon-OneDrive-Documents-Python-Scripts-villanibench-artifacts-runs-claude-code-9b-hard-v0-2-local-tasks-VB-HSTATE-003-sandbox-repo\\abc.jsonl";
    const id = deriveProjectIdentity({ sourcePath });
    expect(id.projectDisplayName).toBe("VB-HSTATE-003");
    expect(id.projectDisplayName).not.toMatch(/^(repo|sandbox)$/);
  });
});
