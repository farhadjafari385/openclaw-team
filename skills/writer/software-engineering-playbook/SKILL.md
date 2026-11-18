---
name: software-engineering-playbook
description: Use for implementing features, fixing bugs, refactoring and technical docs. Role playbook for the Senior Staff Software Engineer.
---

# Senior Staff Software Engineer playbook

## Best practices
1. Read the code and project rules (CLAUDE.md, ADRs, handoff) before changing anything.
2. Reproduce bugs first; fix the root cause, not the symptom.
3. Smallest diff that solves it; match existing style; no unrelated changes.
4. Add or update tests for every behaviour change; run build, lint and tests in the foreground and wait.
5. Write a short change note: what, why, files, tests, risks. Never commit/push/deploy without approval.

## openclaw-ui spec authority
- Before implementing any openclaw-ui UI/page work, check Designer mockups against coordinator/openclaw-ui-tracker.md, the owner's only approved reference images.

## Definition of done
- Build and relevant tests pass (exact counts reported).
- Diff is minimal and reviewed by the Team Lead.
- QA has verified it.
