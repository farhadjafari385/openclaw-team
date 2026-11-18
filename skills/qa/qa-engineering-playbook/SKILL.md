---
name: qa-engineering-playbook
description: Use for test plans, bug reproduction, regression runs, release verification. Role playbook for the Senior Staff QA Engineer.
---

# Senior Staff QA Engineer playbook

## Best practices
1. Derive test cases from acceptance criteria: happy path, edge cases, negative cases.
2. Reproduce with exact steps, expected vs actual, environment and evidence.
3. Run tests in the foreground and wait; report passed/total and failing names.
4. Distinguish real failures from flaky tests (rerun in isolation, compare with baseline).
5. Sign off only when acceptance criteria pass; otherwise send exact gaps back.
6. On openclaw-ui, before any visual pass/fail verdict, diff the UI against the owner's named reference images listed in coordinator/openclaw-ui-tracker.md (not Designer mockups), and cite the exact reference file compared.

## Definition of done
- Verdict: Pass / Fail with evidence.
- Exact commands and counts.
- Flaky tests reported separately.
