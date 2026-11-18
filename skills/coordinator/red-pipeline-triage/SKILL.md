---
name: red-pipeline-triage
description: Red or slow CI pipeline, owner asks to fix pipeline: read logs + step timings myself, find root cause, brief one owner with evidence.
---
1. Read jobs read-only: token from {{private:SERVER_HOME}}/.openclaw/secrets/gitlab-pipeline-watch.token via `curl -K -` stdin; GET pipelines/<id>/jobs and jobs/<id>/trace. Never put the token in argv/URL.
2. Per slow job, take section_start/section_end timestamps from the trace to split time into get_sources, restore_cache, download_artifacts, step_script; inside step_script note install lines (apt-get, chromium, npm ci "added N packages in Xs") vs test time ("PASS ... (N s)").
3. Root cause = repeated work (installs/downloads per job, cache "Not uploading ... due to policy") or the test itself being slow. Never pass timeouts/queues/retries as the problem.
4. Check stale pipelines holding the resource_group slot (waiting_for_resource); have DevOps cancel superseded ones.
5. Record the item in task_inbox (owner, size, done_when) before briefing.
6. Brief one owner (CI -> devops, test bugs -> writer, merge -> reviewer) with Project:, Scope:, Items:, Done when:, Stop if: and the log evidence. Choose the task's model and thinking in sessions_spawn from the table in your rules (a red main or repeated failure: at least Sonnet 5.5 medium).
7. Do not wait or poll. On the report, re-read the pipeline myself; report green link or exact failing job.
