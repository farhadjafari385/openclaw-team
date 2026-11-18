"""Restart the gateway when OpenClaw has locked an agent's database until the next restart.

On 2026-11-11 a gateway restart starved of memory made one agent-database task time out. OpenClaw then
refused every message to that agent ("worker task timed out ... Sessions remain unavailable. Stop the
Gateway, run openclaw doctor --fix ... and restart"; later the same with "database is locked") until someone restarted the gateway: 8.5 hours the
first time, 45 minutes the second. The database itself was fine (doctor --session-sqlite inspect: no
issues); a restart cleared it both times. This check finds that state in the gateway log and does the
same restart, once nothing is running, at most once per 30 minutes and 3 times in 6 hours.
"""
import datetime
import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path

# The lasting lock, whatever its cause ("worker task timed out", "database is locked"), ends in "Sessions remain
# unavailable. Stop the Gateway, run ... and restart." During every startup OpenClaw also refuses for a few minutes
# with "Sessions remain unavailable until background inspection and preparation finish": that passes by itself.
LOCKED = "Sessions remain unavailable. Stop the Gateway"
STARTING_UP = "has not completed startup inspection"
# Another lasting failure of a starved start: the model runtime is never published for an agent, and every message
# to it fails with "prepared reply dispatch runtime owner was not published for <agent>". Right after a start it can
# be transient, so it counts only from NOT_PUBLISHED_GRACE_S after the start.
NOT_PUBLISHED = "runtime owner was not published for "
NOT_PUBLISHED_GRACE_S = 600
PERSIST_S = 60  # the lock is logged about three times a minute; one line alone is not acted on
STARTED = '"1":"starting..."'
AGENT = re.compile(r"Agent ([\w-]+) \(|agentId\\*\":\\*\"([\w-]+)")
TIME = re.compile(r'"time":"([^"]+)"')

COOLDOWN_MIN = 30
MAX_PER_6H = 3
FRESH_MIN = 15  # a lock is acted on only while it is still being logged
FLAG_HOLD_MIN = 30  # the guard honours the maintenance flag this long after its last touch


def events(lines):
    """(epoch seconds, "start" | "locked", agent) for gateway starts and lock refusals, in log order."""
    out = []
    for line in lines:
        start = STARTED in line
        locked = LOCKED in line and STARTING_UP not in line
        unpublished = NOT_PUBLISHED in line
        if not (start or locked or unpublished):
            continue
        m = TIME.search(line)
        if not m:
            continue
        try:
            t = datetime.datetime.fromisoformat(m.group(1)).timestamp()
        except ValueError:
            continue
        if start:
            out.append((t, "start", ""))
        elif unpublished:
            a = re.search(re.escape(NOT_PUBLISHED) + r"([\w-]+)", line)
            out.append((t, "unpublished", a.group(1) if a else "?"))
        else:
            a = AGENT.search(line)
            out.append((t, "locked", (a.group(1) or a.group(2)) if a else "?"))
    return out


def decide(evs, now, running, state, flag_age_s=None, deploy_running=False):
    """What to do now. running: labels of running tasks; state: this check's own record;
    flag_age_s: age of the maintenance flag in seconds (None = no flag)."""
    last_start = max((t for t, k, _ in evs if k == "start"), default=0)
    locked = [(t, a) for t, k, a in evs if t > last_start and now - t <= FRESH_MIN * 60
              and (k == "locked" or (k == "unpublished" and t - last_start >= NOT_PUBLISHED_GRACE_S))]
    if not locked or max(t for t, _ in locked) - min(t for t, _ in locked) < PERSIST_S:
        return {"action": "clear" if state.get("flagSetAt") else "none"}
    agents = sorted({a for _, a in locked})
    since = min(t for t, _ in locked)
    ours = bool(state.get("flagSetAt"))
    # A deploy restarts only once it has its quiet moment, and it sets the maintenance flag then. A deploy that is
    # still waiting for that moment (it may wait hours) holds no flag and does not touch the gateway, so a lock is
    # healed meanwhile; the deploy then sees a gateway that is still starting and waits that start out.
    if flag_age_s is not None and not ours and (deploy_running or flag_age_s < FLAG_HOLD_MIN * 60):
        return {"action": "none", "why": "a deploy is restarting the gateway", "agents": agents}
    heals = [h for h in state.get("heals", []) if now - h < 6 * 3600]
    if heals and now - max(heals) < COOLDOWN_MIN * 60:
        return {"action": "none", "why": "restarted less than 30 minutes ago", "agents": agents}
    if len(heals) >= MAX_PER_6H:
        return {"action": "escalate", "agents": agents, "since": since}
    if running:
        return {"action": "wait", "agents": agents, "since": since, "running": list(running)}
    return {"action": "restart", "agents": agents, "since": since}


def hhmm(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M")


def _read_logs(log_dir):
    files = sorted(glob.glob(os.path.join(log_dir, "openclaw-*.log")), key=os.path.getmtime)[-2:]
    lines = []
    for f in files:
        with open(f, errors="replace") as fh:
            lines.extend(fh)
    return lines


def _running(jobs_file):
    try:
        jobs = json.loads(Path(jobs_file).read_text()).get("jobs", {})
    except (OSError, ValueError):
        return []
    return [str(j.get("label") or k)[:60] for k, j in jobs.items() if j.get("status") == "running"]


def _deploy_running():
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return any(re.match(r"^(/bin/|/usr/bin/)?bash \S*(team-deploy\.sh|run-deploy)\b", l) for l in out.splitlines())


def run(notify, home=None, log_dir="/tmp/openclaw", now=None, restart=None, sleep=time.sleep, deploy_running=None):
    home = Path(home or Path.home())
    sdir = home / ".openclaw" / "token-guard-state"
    flag, state_file = sdir / "maintenance", sdir / "heal.json"
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    now = now or time.time()
    flag_age = now - flag.stat().st_mtime if flag.exists() else None
    d = decide(events(_read_logs(log_dir)), now, _running(sdir / "jobs.json"), state, flag_age, _deploy_running() if deploy_running is None else deploy_running)
    act = d["action"]
    names = ", ".join(d.get("agents", []))

    def save():
        state_file.write_text(json.dumps(state))

    if act == "clear":
        flag.unlink(missing_ok=True)
        state.pop("flagSetAt", None)
        state.pop("waitNotified", None)
        save()
    elif act == "wait":
        flag.touch()  # no new tasks start; the running ones finish, then the restart
        state["flagSetAt"] = state.get("flagSetAt") or now
        if not state.get("waitNotified"):
            notify(f"⚠️ OpenClaw has locked the database of {names} since {hhmm(d['since'])} UTC (\"Sessions remain unavailable\"): "
                   f"messages to them are refused. No new tasks start now; once the {len(d['running'])} running task(s) "
                   "finish, the gateway restarts by itself (about 10 minutes).")
            state["waitNotified"] = now
        save()
    elif act == "restart":
        flag.touch()
        state["flagSetAt"] = state.get("flagSetAt") or now
        state.setdefault("heals", []).append(now)
        state["heals"] = [h for h in state["heals"] if now - h < 6 * 3600]
        save()
        (restart or (lambda: subprocess.run(["systemctl", "--user", "restart", "openclaw-gateway.service"], timeout=900)))()
        sleep(60)
        flag.unlink(missing_ok=True)
        state.pop("flagSetAt", None)
        state.pop("waitNotified", None)
        save()
        notify(f"🔧 OpenClaw had locked the database of {names} since {hhmm(d['since'])} UTC (\"Sessions remain unavailable\"), so "
               f"messages to them were refused. I restarted the gateway at {hhmm(now)} UTC; it answers again in about 10 minutes, "
               "and the held messages are answered then.")
    elif act == "escalate":
        if now - state.get("escalatedAt", 0) > 6 * 3600:
            notify(f"🚩 OpenClaw keeps locking the database of {names} (\"Sessions remain unavailable\") after {MAX_PER_6H} restarts in 6 hours. "
                   "I stopped restarting. Operator: run openclaw doctor --session-sqlite inspect on the server.")
            state["escalatedAt"] = now
            save()
    print(json.dumps(d))
    return d
