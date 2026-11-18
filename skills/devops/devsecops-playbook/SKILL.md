---
name: devsecops-playbook
description: Any infra, CI/CD, deploy, server, Docker, backup, network or security task. How a senior DevSecOps engineer works here, step by step.
---

# Senior DevSecOps playbook

## 0. Orient (every task, ~5 commands, before any claim)
```
hostname; hostname -I; whoami
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
ss -tlnp
systemctl --failed --no-pager
df -h / ; free -m
```
We run ON us-aihub ({{private:SERVER_IP}}): gateway, agents, the aihub GitLab runner and production all live here. Work locally. Never SSH to yourself.

## 1. Diagnose a failure
1. Read the real evidence: job log (`GET /projects/:id/jobs/:job/trace`), `docker logs --tail 200 <c>`, `journalctl -u <svc> -n 200`.
2. Get timings: per-job durations, per-step time in the log. Slow = find the step eating time.
3. Look for waste first: apt-get, npm ci, browser downloads, image pulls repeated per job. Fix with prebuilt pinned images and caches.
4. State the root cause in one sentence with the log line that proves it. If you can't, you're not done diagnosing.
5. Forbidden "fixes": retries, longer timeouts, more shards, sleeps, skipped tests, allow_failure.

## 2. Network / "can't reach X"
- `curl -sS -o /dev/null -w '%{http_code}\n' <url>`, `ss -tlnp | grep <port>`, `ufw status` / `iptables -S | head`, `docker port <c>`.
- Refused = nothing listening or a firewall reject. Timeout = drop/route. Know which before writing a word.

## 3. Deploy production (owner OK required per release)
1. Confirm main pipeline green for the exact commit.
2. Back up the DB; verify the dump is non-empty and readable.
3. Record the current version (rollback target).
4. Deploy (pipeline job on the aihub runner preferred, so there's a job ID). Once the job runs, end with `RESULT: waiting` and its link: do not wait in the session; the guard follows it.
5. Verify (in the follow-up when the guard reports it green): site 200, `/api/health` 200, container healthy, logs clean for 2 min.
6. Report: commit, job ID, backup path, rollback command.

## 4. Backups
A backup is real only after a test restore into a throwaway container. Report age, size and last restore test.

## 5. Security review
Exposed ports vs needed, admin surfaces (never public), TLS, headers/CSP, secrets (paths only, never values), patch level, runner isolation. Each finding: evidence, risk, fix.

## 6. Secrets
Tokens live in {{private:SERVER_HOME}}/.openclaw/secrets/. Pass them via `curl -K -` stdin or an env file. A token in argv or a URL is exposed and must be rotated.

## Report (short)
Done / Proof (commands + key output) / Root cause / Rollback / What's left and who.
Check what the team already has before declaring anything blocked.
