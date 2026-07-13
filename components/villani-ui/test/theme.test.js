import test from "node:test";
import assert from "node:assert/strict";
import { villaniThemeCss, villaniTokens } from "../index.js";

test("shared theme is monochrome and dark", () => {
  assert.equal(villaniTokens.backgroundDeepest, "#050505");
  assert.equal(villaniTokens.textPrimary, "#f2f2f2");
  assert.match(villaniThemeCss, /color-scheme:dark/);
  assert.doesNotMatch(villaniThemeCss, /green|#0f0|#00ff00/i);
});
