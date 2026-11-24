import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import usage  # noqa: E402

import datetime as _dt
T0 = _dt.datetime(2026, 11, 28, 8, 0, tzinfo=_dt.timezone.utc).timestamp()   # an hour start
MIDNIGHT = _dt.datetime(2026, 11, 29, 0, 0, tzinfo=_dt.timezone.utc).timestamp()


def iso(ts):
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def call(mid, ts, model="claude-sonnet-5-5", effort="low", inp=2, read=40000, w5=0, w1h=2000, out=300, session="s1"):
    """One transcript line for a model call, shaped like Claude Code writes it."""
    return json.dumps({"type": "assistant", "timestamp": iso(ts), "sessionId": session, "effort": effort,
                       "message": {"id": mid, "role": "assistant", "model": model,
                                   "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                             "cache_creation_input_tokens": w5 + w1h, "output_tokens": out,
                                             "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1h}}}})


def gw(ts, trigger="user", reuse="reusable", model="claude-opus-5-5"):
    text = f"cli exec: provider=claude-cli model={model} promptChars=400 trigger={trigger} useResume=true session=present resumeSession=abc reuse={reuse} historyPrompt=none"
    # the gateway writes the text twice per line (fields "1" and "message")
    return json.dumps({"0": "{}", "1": text, "_meta": {"date": iso(ts)}, "message": text})


class ParseTest(unittest.TestCase):
    def test_call_fields_and_cache_split(self):
        r = usage.parse_line(call("m1", T0, w5=100, w1h=2000).encode())
        self.assertEqual((r["id"], r["w5"], r["w1h"], r["read"], r["effort"]), ("m1", 100, 2000, 40000, "low"))

    def test_non_calls_are_skipped(self):
        self.assertIsNone(usage.parse_line(b'{"type":"user","message":{"role":"user","content":"hi"}}'))
        self.assertIsNone(usage.parse_line(call("m2", T0, model="<synthetic>").encode()))
        self.assertIsNone(usage.parse_line(b"not json with \"usage\""))

    def test_agent_from_project_dir(self):
        self.assertEqual(usage.agent_of("-home-u--openclaw-workspace-devops"), "devops")
        self.assertEqual(usage.agent_of("-home-u--openclaw-openclaw-workspace"), "other")
        self.assertEqual(usage.agent_of("-tmp"), "other")

    def test_weights_use_published_prices(self):
        r = usage.parse_line(call("m", T0, model="claude-opus-5-5", inp=0, read=1000, w5=0, w1h=1000, out=100).encode())
        total, write, read, out = usage.weights(r)
        self.assertAlmostEqual(write, 4e-6 * 2000)           # Opus 5.5 $4/MTok, 1h write = 2x
        self.assertAlmostEqual(read, 4e-6 * 0.05 * 1000)     # read 0.05x on Opus 5.5
        self.assertAlmostEqual(out, 4e-6 * 500)              # output 5x
        sonnet = usage.parse_line(call("s", T0, model="claude-sonnet-5-5", inp=0, read=0, w1h=1000, out=0).encode())
        self.assertAlmostEqual(usage.weights(sonnet)[1] * 2, write)   # Opus is 2x Sonnet 5.5
        haiku = usage.parse_line(call("n", T0, model="claude-haiku-5-5", inp=0, read=1000, w1h=0, out=0).encode())
        self.assertAlmostEqual(usage.weights(haiku)[2], 0.10e-6 * 0.1 * 1000)
        long = usage.parse_line(call("l", T0, model="claude-haiku-5-5", inp=0, read=150000, w1h=0, out=0).encode())
        self.assertAlmostEqual(usage.weights(long)[2], 0.50e-6 * 0.1 * 150000)   # over 100k tokens: $0.50
        self.assertEqual(usage.price_of("claude-fable-5-2"), (8.0, 0.025))
        self.assertEqual(usage.price_of("claude-opus-4-8"), (5.0, 0.1))

    def test_gateway_line_counts_once(self):
        ev = usage.parse_gateway_line(gw(T0, "session-activity-summary", "none").encode())
        self.assertEqual(ev[0], "exec")
        self.assertEqual((ev[2], ev[3]), ("session-activity-summary", "none"))
        ev = usage.parse_gateway_line(gw(T0, reuse="reusable-drift:system-prompt,prompt-tools").encode())
        self.assertEqual(ev[3], "reusable-drift:system-prompt,prompt-tools")
        self.assertIsNone(usage.parse_gateway_line(b'{"1":"something else","_meta":{"date":"2026-11-28T08:00:00Z"}}'))


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.proj = Path(self.root) / "projects"
        (self.proj / "-home-u--openclaw-workspace-writer").mkdir(parents=True)
        (self.proj / "-home-u--openclaw-workspace-coordinator" / "abc" / "subagents").mkdir(parents=True)
        self.gwdir = Path(self.root) / "gw"
        self.gwdir.mkdir()

    def write(self, rel, lines, mode="a", mtime=None):
        p = self.proj / rel
        with open(p, mode) as f:
            for line in lines:
                f.write(line + "\n")
        t = T0 + 50 if mtime is None else mtime   # files look written at test time, not at the real clock
        os.utime(p, (t, t))
        return p

    def test_dedupe_offsets_partial_lines_and_subagents(self):
        st = {}
        f = self.write("-home-u--openclaw-workspace-writer/s1.jsonl", [call("a", T0 + 10), call("a", T0 + 11), call("b", T0 + 20)])
        self.write("-home-u--openclaw-workspace-coordinator/abc/subagents/agent-x.jsonl", [call("c", T0 + 30, model="claude-opus-5-5")])
        out = usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 60)
        self.assertEqual(out["calls"], 3)
        hk = usage.hour_key(T0)
        self.assertEqual(st["hours"][hk]["writer"]["calls"], 2)
        self.assertEqual(st["hours"][hk]["coordinator"]["calls"], 1)
        # a half-written line is not consumed until its newline arrives
        with open(f, "a") as fh:
            fh.write(call("d", T0 + 40)[:50])
        self.assertEqual(usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 70)["calls"], 0)
        with open(f, "a") as fh:
            fh.write(call("d", T0 + 40)[50:] + "\n" + call("b", T0 + 41) + "\n")
        self.assertEqual(usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 80)["calls"], 1)
        self.assertEqual(st["hours"][hk]["writer"]["calls"], 3)

    def test_later_copy_with_more_output_counts_the_difference(self):
        st = {}
        self.write("-home-u--openclaw-workspace-writer/s1.jsonl", [call("a", T0 + 10, out=10), call("a", T0 + 11, out=310)])
        usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 60)
        b = st["hours"][usage.hour_key(T0)]["writer"]
        self.assertEqual((b["calls"], b["out"]), (1, 310))

    def test_replaced_file_is_read_again_and_old_files_skipped(self):
        st = {}
        f = self.write("-home-u--openclaw-workspace-writer/s1.jsonl", [call("a", T0 + 10)])
        self.write("-home-u--openclaw-workspace-writer/old.jsonl", [call("z", T0 - 5 * 3600)], mtime=T0 - 5 * 3600)
        usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 60)
        self.assertNotIn("z", st["seen"])
        f.unlink()
        self.write("-home-u--openclaw-workspace-writer/s1.jsonl", [call("e", T0 + 100)], mode="w", mtime=T0 + 150)
        self.assertEqual(usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 200)["calls"], 1)

    def test_read_cap_resumes_next_run(self):
        st = {}
        self.write("-home-u--openclaw-workspace-writer/s1.jsonl", [call(f"m{i}", T0 + i) for i in range(50)])
        first = usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 60, read_cap=3000)
        self.assertTrue(first["capped"])
        total = first["calls"]
        for _ in range(40):
            total += usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 60, read_cap=3000)["calls"]
        self.assertEqual(total, 50)

    def test_new_sessions_counted_after_the_first_run(self):
        st = {}
        self.write("-home-u--openclaw-workspace-writer/s1.jsonl", [call("a", T0 + 10, session="old")])
        usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 60)
        self.write("-home-u--openclaw-workspace-writer/s2.jsonl", [call("b", T0 + 70, session="new", w1h=28000)])
        usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 80)
        b = st["hours"][usage.hour_key(T0)]["writer"]
        self.assertEqual((b["new_sessions"], b["big"]), (1, 1))

    def test_gateway_events(self):
        st = {}
        p = self.gwdir / "openclaw-2026-11-28.log"
        p.write_text("\n".join([gw(T0 + 5, "session-activity-summary", "none"), gw(T0 + 6, reuse="reusable-drift:system-prompt"),
                                gw(T0 + 7)]) + "\n")
        os.utime(p, (T0 + 50, T0 + 50))
        usage.ingest(st, str(self.proj), str(self.gwdir), T0 + 60)
        g = st["hours"][usage.hour_key(T0)]["_gw"]
        self.assertEqual((g["starts"], g["triggers"]["session-activity-summary"], g["reuse"]["drift"]), (3, 1, 1))


class ReportTest(unittest.TestCase):
    def state_with(self, calls, week=None):
        st = {}
        for agent, rec in calls:
            usage.add_call(st, agent, usage.parse_line(rec.encode()))
        for at, w in week or []:
            usage.record_week(st, {"at": at * 1000, "week": w, "five": 10, "weekResetsAt": iso(T0 + 4 * 86400)})
        return st

    def test_hour_report_is_readable_and_complete(self):
        st = self.state_with([("coordinator", call("a", T0 + 10, model="claude-opus-5-5", effort="medium", w1h=30000)),
                              ("devops", call("b", T0 + 20, effort="medium")), ("devops", call("c", T0 + 30, model="claude-opus-5-5", effort="high"))],
                             week=[(T0 - 3600, 40), (T0 + 3500, 41.8)])
        text = usage.render(st, T0, T0 + 3600, "📊 Team usage · 08:00–09:00 UTC", {"jobs": {}})
        for want in ("Weekly limit: 42% used (+1.8 this hour)", "AI calls: 3", "Average size of each call", "1 full rebuild\n",
                     "Who used it", "1. Orchestrator — 1 call ·", "DevOps — 2 calls", "Value used at API list prices: $", "Sonnet medium 1, Opus high 1", "Tasks: 0 running"):
            self.assertIn(want, text)
        self.assertLess(len(text), 4000)

    def test_first_report_uses_a_reading_taken_just_after_the_hour(self):
        st = self.state_with([("writer", call("a", T0 + 10, w1h=25000))], week=[(T0 + 3600 + 300, 37)])
        text = usage.render(st, T0, T0 + 3600, "t")
        self.assertIn("Weekly limit: 37% used", text)
        self.assertIn("1 full rebuild\n", text)

    def test_quiet_hour(self):
        text = usage.render({}, T0, T0 + 3600, "t")
        self.assertIn("No AI calls this hour.", text)

    def test_forecast_and_reset(self):
        hist = [[T0, 40, 0], [T0 + 3600, 45, 0]]
        fc = usage.forecast(hist, iso(T0 + 100 * 3600), T0 + 3600)
        self.assertFalse(fc["ok"])
        self.assertAlmostEqual(fc["out_at"], T0 + 3600 + 10 * 3600)
        st = {}
        for at, w in [(T0, 90), (T0 + 600, 95), (T0 + 1200, 1)]:
            usage.record_week(st, {"at": at * 1000, "week": w, "five": 0})
        self.assertEqual([h[1] for h in st["week"]], [1])   # history starts again after the weekly reset


class AlarmTest(unittest.TestCase):
    def test_agent_calls_alarm_and_repeat_filter(self):
        st = {}
        for i in range(85):
            usage.add_call(st, "devops", usage.parse_line(call(f"m{i}", T0 + i * 10).encode()))
        cfg = usage.config({})
        found = usage.alarms(st, cfg, T0 + 900)
        self.assertIn("agent_calls:devops", [k for k, _ in found])
        due = usage.due_alarms(st, found, cfg, T0 + 900)
        st["alarms_sent"] = {k: T0 + 900 for k, _ in due}
        self.assertEqual(usage.due_alarms(st, found, cfg, T0 + 1200), [])
        self.assertTrue(usage.due_alarms(st, found, cfg, T0 + 900 + 3601))
        self.assertIn("🚨 Usage alarm", usage.alarm_text(due, st, T0 + 900))

    def test_context_and_week_jump(self):
        st = {}
        usage.add_call(st, "writer", usage.parse_line(call("big", T0, read=160000).encode()))
        for at, w in [(T0 - 3600, 10), (T0, 14)]:
            usage.record_week(st, {"at": at * 1000, "week": w, "five": 0, "weekResetsAt": iso(T0 + 3 * 86400)})
        kinds = [k for k, _ in usage.alarms(st, usage.config({}), T0 + 60)]
        self.assertIn("context:writer", kinds)
        self.assertIn("week_jump", kinds)


class ScheduleTest(unittest.TestCase):
    def test_hour_report_due_at_minute_two(self):
        self.assertIsNone(usage.report_hour_due({"last_report_hour": T0 - 3600}, T0 + 60, 2))   # 08:01: 07h done
        self.assertEqual(usage.report_hour_due({"last_report_hour": T0 - 3600}, T0 + 3600 + 130, 2), T0)
        self.assertIsNone(usage.report_hour_due({"last_report_hour": T0}, T0 + 3600 + 700, 2))
        self.assertEqual(usage.report_hour_due({}, T0 + 3600 + 130, 2), T0)

    def test_daily_due_once(self):
        midnight = MIDNIGHT
        self.assertIsNone(usage.daily_due({}, midnight + 60, 0, 2))
        self.assertEqual(usage.daily_due({}, midnight + 130, 0, 2), "2026-11-28")
        self.assertIsNone(usage.daily_due({"last_daily": "2026-11-28"}, midnight + 700, 0, 2))

    def test_overdue_report_for_the_watchdog(self):
        self.assertIsNone(usage.report_overdue({}, T0))
        self.assertIsNone(usage.report_overdue({"last_report_hour": T0}, T0 + 2 * 3600 + 600))
        self.assertIn("has not posted", usage.report_overdue({"last_report_hour": T0}, T0 + 2 * 3600 + 1000))


if __name__ == "__main__":
    unittest.main()
