"""team watch: the owner-asks list, the live progress page and the team watchdog (5-minute timer).

Owner, 2026-11-21: "Make sure they finish tasks ... your observing is temporary. Make them live!"
Everything here reads the board and the guard's state; it never moves an item or starts a task.
"""
import datetime as dt
import html
import json
import os
import subprocess
from collections import Counter
from pathlib import Path

HOME = Path.home()
OPEN = ("todo", "in_progress", "review", "qa", "backlog")
OWNER_PREFIXES = ("owner", "external wait")
ALERT_EVERY_S = 2 * 3600


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def last_status_ts(item, status):
    """When the item last entered `status`, from its history (0 if never)."""
    ts = 0.0
    for h in item.get("history") or []:
        if (h.get("to") or h.get("status")) == status:  # oc-inbox history rows say "to"
            ts = max(ts, parse_ts(h.get("ts")))
    return ts


def last_note(item):
    for h in reversed(item.get("history") or []):
        if h.get("note"):
            return str(h["note"])
    return str(item.get("note") or "")


def owner_ask(item):
    """The exact owner step of a blocked item, or None when the note is not an owner step."""
    note = (item.get("note") or last_note(item)).strip()
    low = note.lower()
    if any(low.startswith(p) for p in OWNER_PREFIXES):
        return note
    return None


def owner_asks(items, held=()):
    rows = []
    for i in items:
        if i.get("status") != "blocked" or i.get("project") in held:
            continue
        ask = owner_ask(i)
        rows.append((i.get("priority") or "normal", i["id"], i.get("project", ""), i.get("title", ""), ask or "(no owner step written: the orchestrator must write one or unblock it)"))
    order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    rows.sort(key=lambda r: (order.get(r[0], 4), r[2], r[1]))
    return rows


def asks_markdown(rows, now):
    out = [f"# Owner asks ({dt.datetime.fromtimestamp(now, dt.timezone.utc):%Y-%m-%d %H:%M} UTC)", "",
           "One line per blocked item: the exact step only the owner can do.", ""]
    for prio, iid, proj, title, ask in rows:
        out.append(f"- [{prio}] {iid} ({proj}) {title}: {ask}")
    return "\n".join(out) + "\n"


def unblocked_todo(items, held=()):
    return [i for i in items if i.get("status") == "todo" and i.get("project") not in held]


def recent_spawns(jobs_state, since_ms):
    return [j for j in (jobs_state.get("jobs") or {}).values() if (j.get("spawnedAt") or 0) >= since_ms]


def running(jobs_state):
    return [j for j in (jobs_state.get("jobs") or {}).values() if j.get("status") == "running"]


def watchdog(items, jobs_state, now, gateway_ok, disk_pct, held=()):
    """Alerts for the owner: (kind, text). Pure: the caller rate-limits and sends."""
    alerts = []
    limited = (jobs_state.get("claudeLimitedUntil") or 0) > now * 1000
    todo = unblocked_todo(items, held)
    spawned = recent_spawns(jobs_state, (now - 2 * 3600) * 1000)
    if not gateway_ok:
        alerts.append(("gateway", "The team gateway is not running on us: no agent can work. Check `systemctl --user status openclaw-gateway`."))
    if disk_pct is not None and disk_pct >= 90:
        alerts.append(("disk", f"The disk on us is {disk_pct:.0f}% full: the team stops at 100%."))
    if limited:
        until = dt.datetime.fromtimestamp(jobs_state["claudeLimitedUntil"] / 1000, dt.timezone.utc)
        alerts.append(("limit", f"The team is out of Claude usage until {until:%m-%d %H:%M} UTC; no task can start before then."))
    elif gateway_ok and todo and not spawned and not running(jobs_state):
        ids = ", ".join(i["id"] for i in todo[:6])
        alerts.append(("idle", f"The team started no task in 2 hours while {len(todo)} unblocked item(s) wait in To do ({ids}). The orchestrator is not dispatching."))
    stale = []
    for i in items:
        if i.get("status") == "in_progress" and i.get("project") not in held:
            t = max(parse_ts(i.get("updated_ts")), last_status_ts(i, "in_progress"))
            if t and now - t > 3 * 3600:
                stale.append(i["id"])
    if stale and not limited:
        alerts.append(("stale", f"{len(stale)} item(s) In progress with no board change for 3 hours: {', '.join(stale[:8])}."))
    return alerts


def due(state, kind, now, every=ALERT_EVERY_S):
    return now - (state.get("sent", {}).get(kind) or 0) >= every


def digest_due(state, now, hour_utc=7):
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    return d.hour >= hour_utc and state.get("digest_day") != d.strftime("%Y-%m-%d")


def done_since(items, since):
    return sorted((i for i in items if i.get("status") == "done" and last_status_ts(i, "done") >= since),
                  key=lambda i: -last_status_ts(i, "done"))


def render_page(items, jobs_state, now, usage="", held=()):
    esc = html.escape
    live = [i for i in items if i.get("project") not in held]
    counts = Counter(i.get("status") for i in live)
    done24 = done_since(live, now - 86400)
    doing = [i for i in live if i.get("status") in ("in_progress", "review", "qa")]
    todo = [i for i in live if i.get("status") in ("todo", "backlog")]
    asks = owner_asks(items, held)
    run = running(jobs_state)
    stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def table(rows, cols):
        h = "<table><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
        for r in rows:
            h += "<tr>" + "".join(f"<td>{esc(str(c))}</td>" for c in r) + "</tr>"
        return h + "</table>"

    parts = [f"<h1>Team progress</h1><p class=meta>Updated {stamp} · {esc(usage)} · refreshes every 5 min</p>",
             "<div class=kpi>" + "".join(f"<div><b>{n}</b> {k}</div>" for k, n in (
                 ("done in 24 h", len(done24)), ("in progress", len(doing)), ("to do", len(todo)),
                 ("blocked", counts.get("blocked", 0)), ("tasks running", len(run)),
                 ("M tokens per done item (24 h)", f"{cost_24h(jobs_state, items, now, held)[2] / 1e6:.1f}" if cost_24h(jobs_state, items, now, held)[2] else "-"))) + "</div>",
             "<h2>Running now</h2>" + table([(j.get("agentId", ""), j.get("label", "")) for j in run], ("Agent", "Task")),
             "<h2>Done in the last 24 h</h2>" + table([(i["id"], i.get("project", ""), i.get("title", ""), last_note(i)[:160]) for i in done24], ("ID", "Project", "Item", "Proof")),
             "<h2>In progress, review, QA</h2>" + table([(i["id"], i.get("status"), i.get("project", ""), i.get("title", ""), last_note(i)[:160]) for i in doing], ("ID", "Column", "Project", "Item", "Latest note")),
             "<h2>To do</h2>" + table([(i["id"], i.get("priority", ""), i.get("project", ""), i.get("title", "")) for i in todo], ("ID", "Priority", "Project", "Item")),
             "<h2>Waiting on the owner</h2>" + table([(r[1], r[0], r[2], r[3], r[4]) for r in asks], ("ID", "Priority", "Project", "Item", "Owner step"))]
    if held:
        parts.append(f"<p class=meta>On owner hold, not shown: {esc(', '.join(sorted(held)))}.</p>")
    css = ("body{font:14px/1.45 system-ui,sans-serif;margin:16px;background:#fff;color:#111}"
           "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}th{background:#222!important}}"
           "table{border-collapse:collapse;width:100%;margin-bottom:18px}th,td{border:1px solid #8884;padding:4px 6px;text-align:left;vertical-align:top}"
           "th{background:#f1f1f1}.kpi{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0}.kpi div{padding:6px 10px;border:1px solid #8884;border-radius:8px}"
           ".meta{color:#888}")
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv=refresh content=120><title>Team progress</title><style>{css}</style></head><body>{''.join(parts)}</body></html>")


def cost_24h(jobs_state, items, now, held=()):
    """Worker tokens of tasks that ran in the last 24 h, and tokens per item that reached Done (owner 2026-11-21: cost)."""
    since = (now - 86400) * 1000
    toks = sum(int(j.get("cliTokens") or j.get("usedTokens") or 0) for j in (jobs_state.get("jobs") or {}).values()
               if max(j.get("endedAt") or 0, j.get("spawnedAt") or 0) >= since)
    done = len(done_since([i for i in items if i.get("project") not in held], now - 86400))
    return toks, done, (toks // done if done else None)


def digest_text(items, asks, now, held=(), jobs_state=None):
    live = [i for i in items if i.get("project") not in held]
    done24 = done_since(live, now - 86400)
    lines = [f"Daily team digest: {len(done24)} item(s) done in 24 h, {sum(1 for i in live if i.get('status') == 'todo')} to do, {len(asks)} waiting on you."]
    if jobs_state is not None:
        toks, done, per = cost_24h(jobs_state, items, now, held)
        lines.append(f"Cost: {toks / 1e6:.1f}M worker tokens in 24 h, " + (f"{per / 1e6:.1f}M per item done." if per else "no item done (all of it was spend without a result)."))
    for i in done24[:10]:
        lines.append(f"✅ {i['id']} {i.get('title', '')[:70]}")
    if asks:
        lines.append("")
        lines.append("Only you can do these:")
        for prio, iid, proj, title, ask in asks[:15]:
            lines.append(f"• {iid} ({proj}): {ask[:160]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- side effects (cmd_watch)

def oc_inbox_items():
    env = dict(os.environ, PATH=f"{HOME}/.openclaw/bin:{os.environ.get('PATH', '')}")
    out = subprocess.run(["oc-inbox", "list", "--json"], capture_output=True, text=True, timeout=120, env=env)
    return json.loads(out.stdout or "[]")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def gateway_active():
    r = subprocess.run(["systemctl", "--user", "is-active", "openclaw-gateway"], capture_output=True, text=True)
    return r.stdout.strip() == "active"


def disk_used_pct(path="/"):
    st = os.statvfs(path)
    return 100.0 * (1 - st.f_bavail / st.f_blocks) if st.f_blocks else None


def write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
