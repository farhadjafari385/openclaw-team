"""Hourly usage report and alarms (owner 2026-11-24: "report to the team group every hour: how many calls, how much
context per call, how many tokens, which model and effort, 24 times a day, fully automated, not by AI").

The Max plan's weekly limit was used up in 2-3 days for five weeks running, and nobody saw it until it was gone.
This module reads what already exists on the host and never calls a model:
  - the Claude CLI transcripts (~/.claude/projects/<dir>/**/*.jsonl): one record per model call, with model,
    effort and exact token counts (fresh input, cache read, 5-minute and 1-hour cache writes, output);
  - the gateway log (/tmp/openclaw/openclaw-<date>.log): Claude process starts, why a session was rebuilt, and the
    hidden Haiku "activity summary" calls that leave no transcript;
  - the guard's state (~/.openclaw/token-guard-state/jobs.json): the real weekly and 5-hour % read from Anthropic
    every 10 minutes, and the tasks.
Every file is read incrementally: the byte offset of each file is kept in the state file, so a run reads only what
was written since the last run (3.4 GB of transcripts on the host; a full read would load the box for minutes).

Cost shares use Anthropic's published API list prices (PRICES below: Opus 5.5 $4, Sonnet 5.5 $2, Haiku 5.5 $0.10 per
million input tokens; cache write 1h = 2x input, 5m = 1.25x, cache read 0.05x on Opus/Sonnet 5.5; output 5x). Anthropic
does not publish how the subscription meter weighs them, so the report shows shares and an API-price yardstick,
never a limit percentage. The limit
percentage itself comes from Anthropic (the guard's paceUsage)."""
import datetime as dt
import json
import os
import re
from pathlib import Path

HOUR_S = 3600
KEEP_HOURS = 72            # hour buckets kept in the state (the daily summary needs 24)
SEEN_IDS_S = 6 * HOUR_S    # a transcript repeats one model call's record several times, close together
SESSIONS_S = 48 * HOUR_S
RECENT_S = 75 * 60         # alarms look back 30-60 minutes
BIG_WRITE = 20000          # a call that writes this much cache rebuilt the conversation (or started it)
OLD_FILE_S = 3 * HOUR_S    # a file not written for this long is skipped on its first sight (old sessions)
BOOT_TAIL = 30 * 2**20     # on first sight of a recent file, read at most its last 30 MB
RUN_READ_CAP = 200 * 2**20  # bytes read per run at most; the rest waits for the next run (10 minutes later)

NAMES = {"coordinator": "Orchestrator", "devops": "DevOps", "writer": "Writer", "reviewer": "Reviewer", "qa": "QA",
         "growth": "Growth", "designer": "Designer", "researcher": "Researcher", "strategy": "Strategy",
         "social": "Social", "product": "Product", "other": "Other"}

DEFAULTS = {
    "enabled": True,
    "topic": 0,                       # 0: the team's system topic
    "report_minute": 2,               # the hour's report is posted at HH:02 (late transcript lines get 2 minutes)
    "daily_hour_utc": 0,              # yesterday's summary at 00:0x UTC
    "stop_percent": 95,               # the guard's weekly stop (pace.reserve_percent): the forecast aims at it
    "alarm_agent_calls_30m": 80,      # one agent's model calls in 30 minutes
    "alarm_total_calls_hour": 500,    # all model calls in 60 minutes
    "alarm_context_tokens": 150000,   # one call carrying more than this (a runaway conversation)
    "alarm_rebuilds_hour": 30,        # calls that wrote >= 20k tokens of cache in 60 minutes
    "alarm_week_jump_hour": 2.5,      # weekly % gained in the last hour
    "alarm_forecast_margin_hours": 6,  # forecast runs out this many hours before the reset
    "alarm_recaps_hour": 40,          # hidden Haiku activity summaries in 60 minutes
    "alarm_repeat_minutes": 60,       # the same alarm is not repeated within this time
}


# ---------------------------------------------------------------- pure helpers

def config(team):
    c = dict(DEFAULTS)
    c.update((team or {}).get("usage", {}) or {})
    return c


def parse_iso(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def hour_key(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H")


def hour_start(key):
    return dt.datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=dt.timezone.utc).timestamp()


def agent_of(project_dir):
    """The team agent a Claude project directory belongs to (OpenClaw runs each agent in its workspace)."""
    m = re.search(r"openclaw-workspace-([a-z0-9_]+)$", project_dir)
    return m.group(1) if m else "other"


def family(model):
    m = re.search(r"(opus|sonnet|haiku|fable|mythos)", str(model).lower())
    return m.group(1) if m else "other"


# Anthropic list prices, USD per million base input tokens, and the cache-read multiplier
# (https://platform.claude.com/docs/en/about-claude/pricing, read 2026-11-24). Output is 5x input on every model,
# a 5-minute cache write 1.25x, a 1-hour cache write 2x. First match wins.
PRICES = [
    (r"opus-5-5", 4.0, 0.05), (r"sonnet-5-5", 2.0, 0.05),
    (r"(fable|mythos)-5-2", 8.0, 0.025), (r"(fable|mythos)-5-1", 10.0, 0.025), (r"(fable|mythos)-5", 10.0, 0.1),
    (r"haiku-5-5", 0.10, 0.1),                       # 0.50 when the prompt is over 100k tokens (below)
    (r"opus-4-1|opus-4-2025|opus-4$|opus-4-0", 15.0, 0.1), (r"opus-(5|4)", 5.0, 0.1),
    (r"sonnet-5", 2.0, 0.1), (r"sonnet-4", 3.0, 0.1), (r"haiku-4", 1.0, 0.1),
]


def price_of(model, prompt_tokens=0):
    """(USD per million input tokens, cache-read multiplier) for a model id; unknown models are priced like Sonnet."""
    m = str(model).lower()
    for rx, usd, read in PRICES:
        if re.search(rx, m):
            if rx == r"haiku-5-5" and prompt_tokens > 100000:
                return 0.50, read
            return usd, read
    return 2.0, 0.1


def weights(rec):
    """(total, write, read, output) cost of one call in US dollars at Anthropic's API list prices. The Max plan is
    not billed per token; this is the common scale that makes agents, models and hours comparable."""
    prompt = rec["in"] + rec["read"] + rec["w5"] + rec["w1h"]
    usd, read_rate = price_of(rec.get("model", ""), prompt)
    per = usd / 1e6
    write = per * (1.25 * rec["w5"] + 2.0 * rec["w1h"])
    read = per * read_rate * rec["read"]
    out = per * 5 * rec["out"]
    return per * rec["in"] + write + read + out, write, read, out


def parse_line(line):
    """One model call from a transcript line, or None. A call's record repeats (one per content block): the caller
    dedupes by message id."""
    marker = b'"usage"' if isinstance(line, bytes) else '"usage"'
    if marker not in line:
        return None
    try:
        x = json.loads(line)
    except ValueError:
        return None
    if not isinstance(x, dict):
        return None
    m = x.get("message")
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return None
    u = m.get("usage")
    model = str(m.get("model") or "")
    mid = m.get("id") or x.get("requestId")
    ts = parse_iso(x.get("timestamp"))
    if not isinstance(u, dict) or not mid or ts is None or model.startswith("<"):   # "<synthetic>" is no API call
        return None
    cw = int(u.get("cache_creation_input_tokens") or 0)
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5 = int(cc["ephemeral_5m_input_tokens"]) if cc.get("ephemeral_5m_input_tokens") is not None else max(0, cw - w1h)
    effort = x.get("effort")
    # The advisor (a server-side Opus second opinion) is recorded only inside usage.iterations as
    # type "advisor_message"; the top-level numbers are the sum of the "message" iterations alone (checked on the
    # host 2026-11-24: a devops call showed 55,560 Opus input + 9,277 output tokens there and none at the top).
    advisor = []
    for it in u.get("iterations") or []:
        if isinstance(it, dict) and it.get("type") == "advisor_message":
            icc = it.get("cache_creation") if isinstance(it.get("cache_creation"), dict) else {}
            icw = int(it.get("cache_creation_input_tokens") or 0)
            i1h = int(icc.get("ephemeral_1h_input_tokens") or 0)
            advisor.append({"model": str(it.get("model") or "advisor"), "in": int(it.get("input_tokens") or 0),
                            "read": int(it.get("cache_read_input_tokens") or 0), "w1h": i1h, "w5": max(0, icw - i1h),
                            "out": int(it.get("output_tokens") or 0)})
    return {"id": str(mid), "ts": ts, "session": str(x.get("sessionId") or ""), "model": model,
            "effort": effort if isinstance(effort, str) and effort else "?",
            "in": int(u.get("input_tokens") or 0), "read": int(u.get("cache_read_input_tokens") or 0),
            "w5": w5, "w1h": w1h, "out": int(u.get("output_tokens") or 0), "advisor": advisor}


GW_DATE = re.compile(rb'"date":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"')
GW_EXEC = re.compile(rb"cli exec: provider=\S+ model=(\S+) promptChars=\d+ trigger=(\S+) useResume=(\w+) session=\S+ resumeSession=\S+ reuse=([A-Za-z:,_-]+)")
GW_FAILED = re.compile(rb"message processed: channel=telegram .*?outcome=error")


def parse_gateway_line(line):
    """('exec', ts, trigger, reuse, model) for a Claude process start, ('failed', ts) for a message the gateway could
    not process, else None. Each JSON line carries the text twice (fields "1" and "message"): one line, one event."""
    if b"cli exec: provider=" not in line and b"message processed" not in line:
        return None
    d = GW_DATE.search(line)
    ts = parse_iso(d.group(1).decode()) if d else None
    if ts is None:
        return None
    m = GW_EXEC.search(line)
    if m:
        return ("exec", ts, m.group(2).decode(), m.group(4).decode(), m.group(1).decode())
    if GW_FAILED.search(line):
        return ("failed", ts)
    return None


def new_bucket():
    return {"calls": 0, "ctx": 0, "max_ctx": 0, "in": 0, "read": 0, "w5": 0, "w1h": 0, "out": 0,
            "wc": 0.0, "wc_write": 0.0, "wc_read": 0.0, "wc_out": 0.0, "big": 0, "big_tok": 0, "new_sessions": 0,
            "start_ctx": 0, "start_write": 0, "advisor": 0, "advisor_tok": 0, "advisor_wc": 0.0, "mix": {}}


def short_model(model):
    fam = family(model)
    return fam.capitalize() if fam != "other" else (model or "?")[:18]


def add_advisor_calls(state, agent, rec):
    """Each advisor iteration is a separate Opus call: counted once per (message id, position), into the hour of the
    call that asked for it."""
    seen = state.setdefault("seen", {})
    n = 0
    for i, it in enumerate(rec.get("advisor") or []):
        key = f"{rec['id']}#adv{i}"
        if key in seen:
            continue
        seen[key] = [rec["ts"], it["out"], agent]
        b = state.setdefault("hours", {}).setdefault(hour_key(rec["ts"]), {}).setdefault(agent, new_bucket())
        ctx = it["in"] + it["read"] + it["w5"] + it["w1h"]
        total, write, read, out = weights(dict(it, ts=rec["ts"]))
        b["calls"] += 1
        b["ctx"] += ctx
        b["max_ctx"] = max(b["max_ctx"], ctx)
        for k in ("in", "read", "w5", "w1h", "out"):
            b[k] += it[k]
        b["wc"] += total
        b["wc_write"] += write
        b["wc_read"] += read
        b["wc_out"] += out
        b["advisor"] += 1
        b["advisor_tok"] += ctx + it["out"]
        b["advisor_wc"] += total
        label = f"{short_model(it['model'])} advisor"
        b["mix"][label] = b["mix"].get(label, 0) + 1
        state.setdefault("recent", []).append([rec["ts"], agent, ctx, 0, rec["session"][:8]])
        n += 1
    return n


def add_call(state, agent, rec):
    """Count one deduplicated call into its hour bucket and the recent list (for alarms). False if seen before.
    A call's record repeats once per content block; a later copy can carry the final output count, so the larger
    output wins (the difference is added to the bucket the first copy went to)."""
    seen = state.setdefault("seen", {})
    hours = state.setdefault("hours", {})
    prev = seen.get(rec["id"])
    if prev is not None:
        if rec["out"] > prev[1]:
            b = hours.get(hour_key(prev[0]), {}).get(prev[2])
            if b is not None:
                extra = dict(rec, **{"in": 0, "read": 0, "w5": 0, "w1h": 0, "out": rec["out"] - prev[1]})
                total, _, _, out = weights(extra)
                b["out"] += extra["out"]
                b["wc"] += total
                b["wc_out"] += out
            prev[1] = rec["out"]
        return False
    seen[rec["id"]] = [rec["ts"], rec["out"], agent]
    hk = hour_key(rec["ts"])
    b = hours.setdefault(hk, {}).setdefault(agent, new_bucket())
    ctx = rec["in"] + rec["read"] + rec["w5"] + rec["w1h"]
    total, write, read, out = weights(rec)
    b["calls"] += 1
    b["ctx"] += ctx
    b["max_ctx"] = max(b["max_ctx"], ctx)
    for k in ("in", "read", "w5", "w1h", "out"):
        b[k] += rec[k]
    b["wc"] += total
    b["wc_write"] += write
    b["wc_read"] += read
    b["wc_out"] += out
    big = rec["w5"] + rec["w1h"] >= BIG_WRITE
    if big:
        b["big"] += 1
        b["big_tok"] += rec["w5"] + rec["w1h"]
    label = f"{short_model(rec['model'])} {rec['effort']}"
    b["mix"][label] = b["mix"].get(label, 0) + 1
    sessions = state.setdefault("sessions", {})
    if rec["session"] and rec["session"] not in sessions:
        sessions[rec["session"]] = rec["ts"]
        if not state.get("booting"):          # the first call of a session: what every new session starts with
            b["new_sessions"] += 1
            b["start_ctx"] += ctx
            b["start_write"] += rec["w5"] + rec["w1h"]
    state.setdefault("recent", []).append([rec["ts"], agent, ctx, 1 if big else 0, rec["session"][:8]])
    return True


def add_gateway_event(state, ev):
    hours = state.setdefault("hours", {})
    g = hours.setdefault(hour_key(ev[1]), {}).setdefault("_gw", {"starts": 0, "triggers": {}, "reuse": {}, "failed": 0})
    if ev[0] == "exec":
        _, ts, trigger, reuse, _model = ev
        g["starts"] += 1
        g["triggers"][trigger] = g["triggers"].get(trigger, 0) + 1
        kind = "drift" if reuse.startswith("reusable-drift") else "invalidated" if reuse.startswith("invalidated") else reuse.split(":")[0]
        g["reuse"][kind] = g["reuse"].get(kind, 0) + 1
        if trigger == "session-activity-summary":
            state.setdefault("recent_recaps", []).append(ts)
    elif ev[0] == "failed":
        g["failed"] += 1


def prune(state, now):
    state["seen"] = {k: v for k, v in state.get("seen", {}).items() if now - v[0] < SEEN_IDS_S}
    state["sessions"] = {k: v for k, v in state.get("sessions", {}).items() if now - v < SESSIONS_S}
    state["recent"] = [r for r in state.get("recent", []) if now - r[0] < RECENT_S]
    state["recent_recaps"] = [t for t in state.get("recent_recaps", []) if now - t < RECENT_S]
    cutoff = hour_key(now - KEEP_HOURS * HOUR_S)
    state["hours"] = {k: v for k, v in state.get("hours", {}).items() if k >= cutoff}


def record_week(state, pace):
    """Keep the history of Anthropic's weekly % (the guard writes it every ~10 minutes) for the forecast."""
    if not isinstance(pace, dict) or pace.get("week") is None or not pace.get("at"):
        return
    hist = state.setdefault("week", [])
    at = float(pace["at"]) / 1000
    if hist and hist[-1][0] >= at:
        return
    hist.append([at, float(pace["week"]), float(pace.get("five") or 0)])
    # after the weekly reset the % drops: only the current week's points count
    for i in range(len(hist) - 1, 0, -1):
        if hist[i][1] < hist[i - 1][1] - 0.5:
            del hist[:i]
            break
    cutoff = at - 8 * 86400
    state["week"] = [h for h in hist if h[0] >= cutoff]
    state["week_resets_at"] = pace.get("weekResetsAt")


def week_at(hist, ts):
    """The weekly % at or just before ts (None when no point is that old)."""
    best = None
    for t, w, _ in hist:
        if t <= ts:
            best = w
    return best


def forecast(hist, resets_at, now, stop=95.0, window_h=6):
    """{'rate': points per hour, 'out_at': ts or None, 'ok': bool} from the last `window_h` hours, or None."""
    pts = [h for h in hist if now - h[0] <= window_h * HOUR_S]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 0.75 * HOUR_S:
        return None
    rate = (pts[-1][1] - pts[0][1]) / ((pts[-1][0] - pts[0][0]) / HOUR_S)
    reset = parse_iso(resets_at) if resets_at else None
    if pts[-1][1] >= stop:
        return {"rate": rate, "out_at": pts[-1][0], "ok": False, "at_stop": True, "reset": reset}
    if rate <= 0:
        return {"rate": 0.0, "out_at": None, "ok": True, "reset": reset}
    out_at = pts[-1][0] + (stop - pts[-1][1]) / rate * HOUR_S
    return {"rate": rate, "out_at": out_at, "ok": reset is None or out_at >= reset, "reset": reset}


# ---------------------------------------------------------------- text

def fmt_tok(n):
    n = float(n)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


def fmt_when(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%a %d %b %H:%M UTC")


def mix_text(mix):
    items = sorted(mix.items(), key=lambda kv: -kv[1])
    if len(items) == 1:
        return items[0][0].replace(" ", " · ")
    return ", ".join(f"{k} {v}" for k, v in items[:3]) + (", …" if len(items) > 3 else "")


def week_lines(state, start, end, stop):
    hist = state.get("week", [])
    now_w = week_at(hist, end)
    five = next((h[2] for h in reversed(hist) if h[0] <= end), None)
    if now_w is None:
        # the first report after a start can have only a reading taken just after the hour ended
        after = [h for h in hist if end < h[0] <= end + 20 * 60]
        if not after:
            return ["Weekly limit: not read yet (the guard reads it every 10 minutes)"]
        now_w, five = after[0][1], after[0][2]
    prev = week_at(hist, start)
    delta = f" ({now_w - prev:+.1f} this hour)" if prev is not None and end - start <= HOUR_S else (f" ({now_w - prev:+.1f} today)" if prev is not None else "")
    lines = [f"Weekly limit: {now_w:.0f}% used{delta}" + (f" · 5-hour: {five:.0f}%" if five is not None else "")]
    fc = forecast([h for h in hist if h[0] <= end], state.get("week_resets_at"), end, stop)
    reset_txt = fmt_when(fc["reset"]) if fc and fc.get("reset") else "the reset"
    if fc is None:
        lines.append("Speed: not enough readings yet")
    elif fc.get("at_stop"):
        lines.append(f"⛔ At the {stop:.0f}% stop: only your own chats run until {reset_txt}")
    elif fc["ok"]:
        lines.append(f"✅ At this speed it lasts until the reset ({reset_txt})")
    else:
        early = (fc["reset"] - fc["out_at"]) / HOUR_S if fc.get("reset") else 0
        lines.append(f"⚠️ At this speed it reaches {stop:.0f}% on {fmt_when(fc['out_at'])}, {early:.0f} h before the reset")
    return lines


def merge(buckets):
    t = new_bucket()
    for b in buckets:
        for k in ("calls", "ctx", "in", "read", "w5", "w1h", "out", "wc", "wc_write", "wc_read", "wc_out", "big", "big_tok",
                  "new_sessions", "start_ctx", "start_write", "advisor", "advisor_tok", "advisor_wc"):
            t[k] += b.get(k, 0)
        t["max_ctx"] = max(t["max_ctx"], b.get("max_ctx", 0))
        for k, v in b.get("mix", {}).items():
            t["mix"][k] = t["mix"].get(k, 0) + v
    return t


def gw_merge(gws):
    t = {"starts": 0, "triggers": {}, "reuse": {}, "failed": 0}
    for g in gws:
        t["starts"] += g.get("starts", 0)
        t["failed"] += g.get("failed", 0)
        for k in ("triggers", "reuse"):
            for kk, v in g.get(k, {}).items():
                t[k][kk] = t[k].get(kk, 0) + v
    return t


def task_counts(jobs_state, start, end):
    jobs = (jobs_state or {}).get("jobs") or {}
    running = sum(1 for j in jobs.values() if j.get("status") == "running")
    ended = [j for j in jobs.values() if j.get("endedAt") and start * 1000 <= j["endedAt"] < end * 1000]
    v = lambda j: str(j.get("verdict") or j.get("outcome") or "")
    done = sum(1 for j in ended if v(j) == "done")
    blocked = sum(1 for j in ended if v(j) == "blocked")
    failed = sum(1 for j in ended if v(j) in ("failed", "killed", "error", "timeout", "lost"))
    return running, done, blocked, failed, len(ended)


def render(state, start, end, title, jobs_state=None, stop=95.0):
    """The human-readable report for [start, end) from the hour buckets."""
    keys = [hour_key(t) for t in range(int(start), int(end), HOUR_S)]
    per_agent = {}
    gws = []
    for k in keys:
        for agent, b in state.get("hours", {}).get(k, {}).items():
            if agent == "_gw":
                gws.append(b)
            else:
                per_agent.setdefault(agent, []).append(b)
    agents = {a: merge(bs) for a, bs in per_agent.items()}
    tot = merge(agents.values())
    gw = gw_merge(gws)
    span = "hour" if end - start <= HOUR_S else "day"
    lines = [title, ""] + week_lines(state, start, end, stop) + [""]
    if tot["calls"] == 0:
        lines.append(f"No AI calls this {span}.")
    else:
        writes = tot["w5"] + tot["w1h"]
        lines += [f"This {span}",
                  f"• AI calls: {tot['calls']:,}",
                  f"• Average size of each call: {fmt_tok(tot['ctx'] / tot['calls'])} tokens (largest {fmt_tok(tot['max_ctx'])})",
                  f"• Tokens: {fmt_tok(tot['ctx'] + tot['out'])} in total",
                  f"   – re-read memory (cheap): {fmt_tok(tot['read'])}",
                  f"   – rebuilt memory (expensive): {fmt_tok(writes)} · {tot['big']} full rebuild{'' if tot['big'] == 1 else 's'}",
                  f"   – new text in: {fmt_tok(tot['in'])}",
                  f"   – written answers: {fmt_tok(tot['out'])}"]
        drift = gw["reuse"].get("drift", 0) + gw["reuse"].get("invalidated", 0)
        start = (f" · each started at {fmt_tok(tot['start_ctx'] / tot['new_sessions'])} tokens ({fmt_tok(tot['start_write'] / tot['new_sessions'])} written fresh)"
                 if tot["new_sessions"] else "")
        lines.append(f"• New sessions: {tot['new_sessions']}{start} · rebuilt after a prompt change: {drift}")
        if tot["advisor"]:
            lines.append(f"• Advisor (hidden Opus second opinions): {tot['advisor']} call{'' if tot['advisor'] == 1 else 's'} · "
                         f"{fmt_tok(tot['advisor_tok'])} tokens · ${tot['advisor_wc']:,.2f}")
        if tot["wc"] > 0:
            lines.append(f"• Where the cost went: rebuilding {100 * tot['wc_write'] / tot['wc']:.0f}% · re-reading {100 * tot['wc_read'] / tot['wc']:.0f}% · answers {100 * tot['wc_out'] / tot['wc']:.0f}%")
            lines.append(f"• Value used at API list prices: ${tot['wc']:,.2f} (the plan is not charged per token; this is the yardstick)")
    recaps = gw["triggers"].get("session-activity-summary", 0)
    if recaps:
        lines.append(f"• Hidden activity summaries (Haiku, not in the counts above): {recaps}")
    if gw["failed"]:
        lines.append(f"• Telegram messages the gateway failed to process: {gw['failed']}")
    if tot["calls"]:
        lines += ["", "Who used it (share of the estimated cost)"]
        ranked = sorted(agents.items(), key=lambda kv: -kv[1]["wc"])
        for i, (a, b) in enumerate(ranked, 1):
            share = 100 * b["wc"] / tot["wc"] if tot["wc"] else 0
            starts = f" · {b['new_sessions']} new, start {fmt_tok(b['start_ctx'] / b['new_sessions'])}" if b["new_sessions"] else ""
            lines.append(f"{i}. {NAMES.get(a, a)} — {b['calls']:,} call{'' if b['calls'] == 1 else 's'} · {fmt_tok(b['ctx'] / b['calls'])} avg{starts} · {mix_text(b['mix'])} · {share:.0f}%")
    if jobs_state is not None:
        running, done, blocked, failed, ended = task_counts(jobs_state, start, end)
        lines += ["", f"Tasks: {running} running now · this {span}: {done} done ✅ · {blocked} blocked · {failed} failed"
                  + (f" · {ended - done - blocked - failed} other" if ended - done - blocked - failed > 0 else "")]
    return "\n".join(lines)


def alarms(state, cfg, now):
    """[(kind, text)] for the alarm conditions met now (before the repeat filter)."""
    out = []
    recent = state.get("recent", [])
    last30 = [r for r in recent if now - r[0] <= 30 * 60]
    last60 = [r for r in recent if now - r[0] <= 60 * 60]
    per_agent = {}
    for r in last30:
        per_agent[r[1]] = per_agent.get(r[1], 0) + 1
    for a, n in sorted(per_agent.items(), key=lambda kv: -kv[1]):
        if n > cfg["alarm_agent_calls_30m"]:
            out.append((f"agent_calls:{a}", f"{NAMES.get(a, a)} made {n} AI calls in the last 30 minutes (alarm level {cfg['alarm_agent_calls_30m']})."))
    if len(last60) > cfg["alarm_total_calls_hour"]:
        out.append(("total_calls", f"The team made {len(last60)} AI calls in the last hour (alarm level {cfg['alarm_total_calls_hour']})."))
    big_ctx = {}
    for r in last60:
        if r[2] > cfg["alarm_context_tokens"] and r[2] > big_ctx.get(r[1], (0, ""))[0]:
            big_ctx[r[1]] = (r[2], r[4])
    for a, (c, sid) in big_ctx.items():
        out.append((f"context:{a}", f"{NAMES.get(a, a)} sent a call of {fmt_tok(c)} tokens (session {sid}; alarm level {fmt_tok(cfg['alarm_context_tokens'])})."))
    rebuilds = sum(r[3] for r in last60)
    if rebuilds > cfg["alarm_rebuilds_hour"]:
        out.append(("rebuilds", f"{rebuilds} calls rebuilt their memory (wrote 20k+ tokens of cache) in the last hour (alarm level {cfg['alarm_rebuilds_hour']})."))
    recaps = len([t for t in state.get("recent_recaps", []) if now - t <= 60 * 60])
    if recaps > cfg["alarm_recaps_hour"]:
        out.append(("recaps", f"{recaps} hidden Haiku activity summaries in the last hour (alarm level {cfg['alarm_recaps_hour']}). Something keeps the Control UI activity view open."))
    hist = state.get("week", [])
    now_w, prev_w = week_at(hist, now), week_at(hist, now - HOUR_S)
    if now_w is not None and prev_w is not None and now_w - prev_w > cfg["alarm_week_jump_hour"]:
        out.append(("week_jump", f"The weekly limit went up {now_w - prev_w:.1f} points in the last hour (now {now_w:.0f}%; alarm level {cfg['alarm_week_jump_hour']})."))
    fc = forecast(hist, state.get("week_resets_at"), now, float(cfg["stop_percent"]))
    if fc and not fc["ok"] and not fc.get("at_stop") and fc.get("reset") and now_w is not None and now_w >= 5:
        early = (fc["reset"] - fc["out_at"]) / HOUR_S
        if early > cfg["alarm_forecast_margin_hours"]:
            out.append(("forecast", f"At this speed the weekly limit reaches {cfg['stop_percent']}% on {fmt_when(fc['out_at'])}, {early:.0f} h before the reset."))
    return out


def due_alarms(state, found, cfg, now):
    sent = state.setdefault("alarms_sent", {})
    repeat = float(cfg["alarm_repeat_minutes"]) * 60
    due = [(k, t) for k, t in found if now - sent.get(k, 0) >= (3 * repeat if k == "forecast" else repeat)]
    return due


def alarm_text(due, state, now):
    hist = state.get("week", [])
    now_w, prev_w = week_at(hist, now), week_at(hist, now - HOUR_S)
    lines = [f"🚨 Usage alarm · {dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%H:%M')} UTC"]
    lines += [f"• {t}" for _, t in due]
    if now_w is not None:
        lines.append(f"Weekly limit now {now_w:.0f}%" + (f" ({now_w - prev_w:+.1f} in the last hour)" if prev_w is not None else ""))
    return "\n".join(lines)


def report_hour_due(state, now, minute):
    """The start of the last full hour if its report is due and not posted yet, else None."""
    last_full = int(now // HOUR_S) * HOUR_S - HOUR_S
    if now < last_full + HOUR_S + minute * 60:
        last_full -= HOUR_S
    done = state.get("last_report_hour")
    if done is not None and done >= last_full:
        return None
    return last_full


def daily_due(state, now, hour_utc, minute):
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if now < today.timestamp() + hour_utc * HOUR_S + minute * 60:
        return None
    day = (today - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return None if state.get("last_daily") == day else day


def report_overdue(state, now, minutes=75):
    """For the watchdog: text when the hourly report stopped (None when fine or never installed)."""
    last = (state or {}).get("last_report_hour")
    if last is None:
        return None
    late = now - (last + 2 * HOUR_S)
    if late > (minutes - 60) * 60:
        return f"the hourly usage report has not posted since the {fmt_when(last)} hour (timer openclaw-team-usage)."
    return None


# ---------------------------------------------------------------- incremental reading

def read_new(path, entry, now, budget, first_sight_tail=BOOT_TAIL):
    """(lines, new_entry, bytes_read, cut_short) for the complete lines written to `path` since `entry` =
    [inode, offset, mtime]. A file seen for the first time is read from its last `first_sight_tail` bytes if it was
    written recently, else skipped to its end. A replaced or truncated file (other inode, smaller, or rewritten at
    the same size) starts again from 0. cut_short: the read stopped at the run's byte budget."""
    try:
        st = os.stat(path)
    except OSError:
        return [], None, 0, False
    if entry and (entry[0] != st.st_ino or st.st_size < entry[1]
                  or (st.st_size == entry[1] and len(entry) > 2 and st.st_mtime > entry[2] + 1)):
        entry = [st.st_ino, 0, 0]
    skip_partial = False
    if not entry:
        if now - st.st_mtime > OLD_FILE_S:
            return [], [st.st_ino, st.st_size, st.st_mtime], 0, False
        entry = [st.st_ino, max(0, st.st_size - first_sight_tail), 0]
        skip_partial = entry[1] > 0
    want = st.st_size - entry[1]
    if want <= 0:
        return [], [entry[0], entry[1], st.st_mtime], 0, False
    if budget <= 0:
        return [], entry, 0, True
    with open(path, "rb") as f:
        f.seek(entry[1])
        data = f.read(min(want, budget))
    cut = len(data) < want
    start = 0
    if skip_partial:
        nl = data.find(b"\n")
        if nl < 0:
            return [], entry, 0, cut
        start = nl + 1
    end = data.rfind(b"\n")
    if end < start:
        return [], entry, 0, cut
    lines = data[start:end].split(b"\n")
    new_off = entry[1] + end + 1
    return lines, [entry[0], new_off, st.st_mtime if new_off >= st.st_size else 0], end + 1, cut


def transcript_files(root):
    """(path, agent) for every transcript under ~/.claude/projects, including Claude Code subagent transcripts."""
    out = []
    try:
        dirs = sorted(os.listdir(root))
    except OSError:
        return out
    for d in dirs:
        agent = agent_of(d)
        base = os.path.join(root, d)
        for dirpath, dirnames, filenames in os.walk(base):
            if dirpath.count(os.sep) - base.count(os.sep) >= 3:
                dirnames[:] = []
            for name in filenames:
                if name.endswith(".jsonl"):
                    out.append((os.path.join(dirpath, name), agent))
    return out


def ingest(state, projects_root, gateway_dir, now, read_cap=RUN_READ_CAP):
    """Read what was written since the last run. Returns {'calls': n, 'events': n, 'bytes': n, 'capped': bool}."""
    files = state.setdefault("files", {})
    state["booting"] = not files
    budget = read_cap
    calls = events = 0
    capped = False
    present = set()
    for path, agent in transcript_files(projects_root):
        present.add(path)
        lines, entry, nbytes, cut = read_new(path, files.get(path), now, budget)
        budget -= nbytes
        capped = capped or cut
        if entry is not None:
            files[path] = entry
        for line in lines:
            rec = parse_line(line)
            if rec and now - rec["ts"] < KEEP_HOURS * HOUR_S:
                calls += add_call(state, agent, rec) + add_advisor_calls(state, agent, rec)
    gw_files = sorted(Path(gateway_dir).glob("openclaw-20*.log")) if gateway_dir and Path(gateway_dir).exists() else []
    for p in gw_files[-2:]:
        present.add(str(p))
        lines, entry, nbytes, cut = read_new(str(p), files.get(str(p)), now, budget, first_sight_tail=10 * 2**20)
        budget -= nbytes
        capped = capped or cut
        if entry is not None:
            files[str(p)] = entry
        for line in lines:
            ev = parse_gateway_line(line)
            if ev and now - ev[1] < KEEP_HOURS * HOUR_S:
                add_gateway_event(state, ev)
                events += 1
    state["files"] = {k: v for k, v in files.items() if k in present}   # deleted sessions and old logs are forgotten
    state["booting"] = False
    prune(state, now)
    return {"calls": calls, "events": events, "bytes": read_cap - budget, "capped": capped}
