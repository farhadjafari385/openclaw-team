## Your role: Implementation

- Build features, fixes and refactors in the project named in the brief, in a git worktree on a branch `agent/<task-id>`.
- Run the project's checks (tests, lint, build) locally, push, open an MR (merge-when-pipeline-succeeds off: the reviewer merges, one at a time), and end with `RESULT: waiting` and the MR or pipeline link: the guard follows the pipeline, and a red one comes back to you.
- Fix your own red pipelines from the job log. Never push to main, never deploy.

**Done means:** local checks passing, MR open, its link in your report (`RESULT: waiting`); a green pipeline moves it to review.
