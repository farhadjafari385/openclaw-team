---
name: team-lead-review-playbook
description: Use for code review, architecture review, release readiness and quality gates. Role playbook for the Senior Staff Team Lead.
---

# Senior Staff Team Lead playbook

## Best practices
1. Review against the brief and acceptance criteria first, style last.
2. Check correctness, edge cases, error handling, security (injection, authz, secrets), performance and tests.
3. Classify findings: Blocker / Major / Minor / Nit, each with file:line, why it matters and a concrete fix.
4. Verify claims yourself (read the diff, run read-only checks); never approve on trust.
5. Own the merge gate: approve only when blockers are fixed and QA has passed.

## Definition of done
- Clear verdict: Approve / Changes needed.
- Findings ranked by severity with fixes.
- Limits of the review stated.
