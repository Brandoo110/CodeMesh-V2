import { createElement } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function MemoryMarkdownPreview({
  text,
  clamp = true,
}: {
  text: string;
  clamp?: boolean;
}) {
  const className = [
    "prose-msg text-sm leading-relaxed text-fg-muted",
    clamp ? "line-clamp-4" : "whitespace-normal",
  ].join(" ");

  return createElement(
    "div",
    { className },
    createElement(ReactMarkdown, { remarkPlugins: [remarkGfm] }, text),
  );
}
