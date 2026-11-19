import assert from "node:assert/strict";
import test from "node:test";
import { aboutContact, PROJECT_GITHUB_URL } from "../src/lib/about.ts";

test("the contact address accepts one mailbox and trims surrounding whitespace", () => {
  assert.deepEqual(aboutContact("  demo+support@example.com \n"), {
    text: "demo+support@example.com",
    href: "mailto:demo+support@example.com",
  });
});

test("missing or malformed contact addresses go to project Issues", () => {
  for (const value of [undefined, "", "  ", "invalid", "Demo <demo@example.com>", "demo@example.com?subject=Hi", "demo@example.com,other@example.com"]) {
    assert.deepEqual(aboutContact(value), {
      text: "通过 GitHub Issues 联系",
      href: `${PROJECT_GITHUB_URL}/issues`,
    });
  }
});
