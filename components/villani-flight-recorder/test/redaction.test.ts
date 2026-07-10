import { describe, expect, it } from "vitest";
import { redactString } from "../src/redaction/redact.js";

describe("redaction", () => {
  it("redacts required secret categories", () => {
    const secret =
      "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz1234567890 Bearer abcdefghijklmnopqrstuvwxyz.abcdefghijklmnopqrstuvwxyz.abcdefghijklmnopqrstuvwxyz ghp_abcdefghijklmnopqrstuvwxyz123456 DATABASE_URL=postgres://u:p@example/db PASSWORD=hunter2 npm_abcdefghijklmnopqrstuvwxyz123456 AKIAABCDEFGHIJKLMNOP AIzaabcdefghijklmnopqrstuvwxyz -----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----";
    const red = redactString(secret);
    expect(red).toContain("[REDACTED_ENV_VALUE]");
    expect(red).toContain("[REDACTED_TOKEN]");
    expect(red).toContain("[REDACTED_PRIVATE_KEY]");
    expect(red).toContain("[REDACTED_CONNECTION_STRING]");
    expect(red).toContain("[REDACTED_SECRET]");
  });
});
