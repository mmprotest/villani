import { describe, expect, it } from "vitest";
import { buildStaticExport } from "../src/staticExport";

describe("offline export", () => {
  it("is self-contained, escaped, and excludes secret artifacts", () => {
    const html = buildStaticExport({
      detail: {
        id: "run",
        status: "FAILED",
        repository_id: "repo",
        first_occurred_at: "2026-01-01T00:00:00Z",
        last_observed_at: "2026-01-01T00:00:01Z",
        attempts: [],
        outcomes: [],
        artifact_count: 2,
      },
      events: [{ id: "e", name: "<script>alert(1)</script>" }],
      spans: [],
      artifacts: [
        {
          artifact_id: "safe",
          logical_role: "patch",
          media_type: "text/plain",
          size_bytes: 1,
          sensitivity: "internal",
        },
        {
          artifact_id: "secret",
          logical_role: "secret-log",
          media_type: "text/plain",
          size_bytes: 1,
          sensitivity: "secret",
        },
      ],
      derived: {
        status: {
          status: "failed",
          label: "Failed",
          tone: "error",
          reason: "failed",
          failedCommands: 0,
          failedTests: 0,
          totalCommands: 0,
          totalTests: 0,
          fileEdits: 0,
        },
        task: "task",
        repository: "repo",
        policy: "p",
        agent: "a",
        model: "m",
        candidates: [],
        metrics: [],
        changedFiles: [],
        patchEvolution: [],
        policyDecisions: [],
      },
      exportedAt: "2026-01-01T00:00:00Z",
    });
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(html).not.toContain("<script>alert(1)</script>");
    expect(html).toContain("safe");
    expect(html).not.toContain("secret-log");
    expect(html).not.toContain('src="http');
    expect(html).toContain('data-villani-theme="shared"');
    expect(html).toContain("--v-bg-root: #f6f6f3");
    expect(html).toContain('class="v-app-shell"');
    expect(html).not.toMatch(/#050505|#090d19|#11182a|#22c55e|#2563eb/i);
  });
});
