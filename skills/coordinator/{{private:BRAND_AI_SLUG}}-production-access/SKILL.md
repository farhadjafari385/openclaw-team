---
name: {{private:BRAND_AI_SLUG}}-production-access
description: Any change or check on {{private:BRAND_AI}} production ({{private:BRAND_AI_SLUG}}.com, api/cdn.{{private:BRAND_AI_SLUG}}.com, console/monitor/errors.{{private:BRAND_AI_SLUG}}.cloud, the APISIX gateway, nginx allowlists, the server {{private:BRAND_AI_SERVER_IP}}). Production is reachable, not "blocked": this is the way in.
---

# {{private:BRAND_AI}} production: the way in

There is no SSH from us-aihub to the {{private:BRAND_AI}} server ({{private:BRAND_AI_SERVER_IP}}, host GPU-{{private:BRAND_AI}}): port 22 is closed
to it. That is not a blocker. The way in is a **manual CI job in GitLab `{{private:ORG_SLUG}}-tech/main-zone/ai-platform/infra`
on master**, where the protected deploy variables (`SSH_PRIVATE_KEY`, `PROD_SSH_HOST`, `PROD_SSH_USER`) exist.

## Steps
1. Write the script that runs on the server (`access/<name>.sh`) and a manual job in `.gitlab-ci.yml` that pipes
   it over ssh. Copy an existing job (`extends: allowlist`). Read-only first: report what is there now.
2. Test the script locally against a fake (a small Python HTTP server standing in for the gateway Admin API).
3. Branch `agent/<task-id>`, open an MR, have it reviewed, merge it (master is protected; jobs with the deploy
   variables only run there).
4. Play the job with the API, **with no variables**. Passing variables is refused (403), so each mode is its own job:
   `POST /api/v4/projects/<infra id>/jobs/<job id>/play`, then read `/jobs/<job id>/trace`.
5. Check the result from outside (curl from us-aihub and through Cloudflare). Post the job log lines as evidence.

## Jobs that exist
- `allowlist:inspect`, `gateway:routes`: read-only. Containers, allowlists, what changed, every APISIX route with
  its hosts, plugins and IP lists.
- `allowlist`: reports the owner's allowlisted IP everywhere it is enforced.
- `gateway:cdn-public`: keeps `cdn.{{private:BRAND_AI_SLUG}}.com` public.

## Facts
- The CI `APISIX_ADMIN_KEY` is stale (401). Read the live key from `/opt/{{private:BRAND_AI_SLUG}}/gateway/apisix_conf/config.yaml`
  inside the script and never print it. Admin API: `http://127.0.0.1:9180` on the server.
- The owner's allowlisted IP is {{private:OWNER_ALLOWED_IP}} (was {{private:OWNER_OLD_ALLOWED_IP}} until 2026-11-04). The nginx allow lines and the
  APISIX ip-restriction lists for console/monitor/logs/errors carry it.
- `cdn.{{private:BRAND_AI_SLUG}}.com` (blog covers, tool results; route `cdn-media`) must stay public: no ip-restriction. The gateway
  does not see visitors' real IPs there, so an allowlist on it blocked everyone (2026-11-06).
- Changing a repo file is not changing production: infra has no deploy pipeline. A live change is done only when
  the job log and an outside check show it.

## Rules
Every production change needs the owner's OK, a backup taken by the script (e.g. `/var/backups/...`), and an outside
check afterwards. Never print keys, tokens or the SSH key. Read-only inspection needs no OK.
