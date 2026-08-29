---
title: Document reviewer operability evidence
owner: CodeMesh maintainers
---

# Scope

This task spec documents the operability evidence for the CodeMesh V2 fixed
reviewer in the real local Change Acceptance quickstart. It records the
bounded request contract, the Case API replay boundary, observable evidence,
and the honest stop/rollback boundary. A human decision is still pending.

- [ ] Confirm the fixed reviewer uses `max_retries=0` and sends one provider request per run; the focused test keeps `len(seen) == 1`.
- [ ] Confirm Case API `Idempotency-Key` exclusively owns replay/conflict behavior and provider `response_format` does not change that contract.
- [ ] Confirm observability covers reviewer status/schema/error, prompt/raw/canonical digests, `ExecutionReceipt`, and Case/Passport reads; document that there is no independent runtime kill-switch, so safe stop/rollback means stopping the local service or rolling back the provider option, with failures failing closed.
- [ ] Confirm the focused DeepSeek test and generic exact-payload test pass. Human review/decision remains pending; this evidence does not create approval or close a Case question.
