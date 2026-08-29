---
title: Bound DeepSeek V4 reviewer output
owner: CodeMesh maintainers
---

# Scope

This task spec documents the CodeMesh V2 fixed-reviewer transport change for the
real local Change Acceptance quickstart. The DeepSeek route must explicitly
disable provider thinking and request provider JSON mode while retaining the
existing one-shot bounded JSON contract; other providers keep their exact
payload. Reviewer provider execution after this fix and real dogfood remain
pending.

- [ ] Confirm `route.provider == "deepseek"` adds `extra_body: {"thinking": {"type": "disabled"}}` and `response_format: {"type": "json_object"}`, keeps `max_tokens: 4096`, and sends exactly one request.
- [ ] Confirm every non-DeepSeek provider retains its exact prior request payload, with no thinking field, tools, fallback, or response-format additions.
- [ ] Confirm the fixed quickstart README wording describes bounded non-thinking transport and provider JSON mode for both `deepseek-v4-flash` and explicitly selected `deepseek-v4-pro`, without claiming a dynamic thinking mode.
- [ ] Confirm the focused DeepSeek transport test and the adjacent fixed-reviewer test file pass, with no real provider or dogfood claim.
