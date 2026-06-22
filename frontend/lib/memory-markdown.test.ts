import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { MemoryMarkdownPreview } from "./memory-markdown.ts";

test("memory preview renders markdown instead of raw syntax", () => {
  const html = renderToStaticMarkup(
    createElement(MemoryMarkdownPreview, {
      text: "**Why:** useful context\n\n**How to apply:** load it before planning",
    }),
  );

  assert.match(html, /<strong>Why:<\/strong>/);
  assert.match(html, /<strong>How to apply:<\/strong>/);
  assert.doesNotMatch(html, /\*\*Why:\*\*/);
});
