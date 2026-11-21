import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import watch  # noqa: E402

NOW = 1795240000.0  # 2026-11-21


def iso(ts):
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def item(i, status, project="ai", note="", hist=(), prio="normal", updated=None):
    return {"id": i, "status": status, "project": project, "title": f"title {i}", "priority": prio, "note": note,
            "history": [{"to": s, "ts": iso(t), "note": n} for s, t, n in hist],
            "updated_ts": iso(updated if updated is not None else NOW - 60)}


class OwnerAsks(unittest.TestCase):
    def test_blocked_owner_notes_listed_and_held_projects_skipped(self):
        items = [item("a1", "blocked", note="OWNER: set the payments profile", prio="urgent"),
                 item("b2", "blocked", note="owner money: pay $5"),
                 item("c3", "blocked", note="waiting on CI"),
                 item("d4", "blocked", project="openclaw-ui", note="owner hold: village"),
                 item("e5", "todo", note="OWNER: not blocked")]
        rows = watch.owner_asks(items, held=("openclaw-ui",))
        self.assertEqual([r[1] for r in rows], ["a1", "b2", "c3"])
        self.assertIn("payments profile", rows[0][4])
        self.assertIn("no owner step written", rows[2][4])
        md = watch.asks_markdown(rows, NOW)
        self.assertIn("a1 (ai)", md)


class Watchdog(unittest.TestCase):
    def test_idle_alert_when_todo_and_nothing_started(self):
        items = [item("t1", "todo")]
        jobs = {"jobs": {"x": {"status": "ended", "spawnedAt": (NOW - 5 * 3600) * 1000}}}
        kinds = [k for k, _ in watch.watchdog(items, jobs, NOW, True, 50.0)]
        self.assertEqual(kinds, ["idle"])

    def test_no_idle_alert_when_a_task_started_or_runs(self):
        items = [item("t1", "todo")]
        started = {"jobs": {"x": {"status": "ended", "spawnedAt": (NOW - 600) * 1000}}}
        runs = {"jobs": {"x": {"status": "running", "spawnedAt": (NOW - 5 * 3600) * 1000}, "y": {"status": "interrupted"}}}
        self.assertEqual(len(watch.running(runs)), 1, "an interrupted job is not running")
        self.assertEqual(watch.watchdog(items, started, NOW, True, 50.0), [])
        self.assertEqual(watch.watchdog(items, runs, NOW, True, 50.0), [])

    def test_held_projects_never_count_as_idle(self):
        items = [item("t1", "todo", project="openclaw-ui")]
        self.assertEqual(watch.watchdog(items, {"jobs": {}}, NOW, True, 50.0, held=("openclaw-ui",)), [])

    def test_limit_replaces_idle_and_gateway_disk_reported(self):
        items = [item("t1", "todo")]
        jobs = {"jobs": {}, "claudeLimitedUntil": (NOW + 3600) * 1000}
        kinds = [k for k, _ in watch.watchdog(items, jobs, NOW, False, 93.0)]
        self.assertEqual(kinds, ["gateway", "disk", "limit"])

    def test_stale_in_progress(self):
        items = [item("s1", "in_progress", hist=[("in_progress", NOW - 4 * 3600, "")], updated=NOW - 4 * 3600)]
        jobs = {"jobs": {"x": {"status": "running", "spawnedAt": NOW * 1000}}}
        kinds = [k for k, _ in watch.watchdog(items, jobs, NOW, True, 10.0)]
        self.assertEqual(kinds, ["stale"])

    def test_rate_limit_and_digest_once_a_day(self):
        st = {"sent": {"idle": NOW - 3600}}
        self.assertFalse(watch.due(st, "idle", NOW))
        self.assertTrue(watch.due(st, "idle", NOW + 3700))
        self.assertTrue(watch.due(st, "disk", NOW))
        import datetime as dt
        at8 = dt.datetime(2026, 11, 21, 8, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertTrue(watch.digest_due({}, at8))
        self.assertFalse(watch.digest_due({"digest_day": "2026-11-21"}, at8))
        self.assertFalse(watch.digest_due({}, at8 - 3 * 3600))


class Page(unittest.TestCase):
    def test_page_shows_done_doing_asks_and_escapes(self):
        items = [item("d1", "done", hist=[("done", NOW - 3600, "live: curl 200 <b>")]),
                 item("d0", "done", hist=[("done", NOW - 3 * 86400, "old")]),
                 item("p1", "qa", hist=[("qa", NOW - 100, "panel: in review")]),
                 item("b1", "blocked", note="OWNER: create the bot"),
                 item("h1", "todo", project="openclaw-ui")]
        page = watch.render_page(items, {"jobs": {}}, NOW, "", held=("openclaw-ui",))
        self.assertIn("d1", page)
        self.assertNotIn(">d0<", page)
        self.assertIn("p1", page)
        self.assertIn("create the bot", page)
        self.assertNotIn(">h1<", page)
        self.assertIn("&lt;b&gt;", page)
        text = watch.digest_text(items, watch.owner_asks(items, ("openclaw-ui",)), NOW, ("openclaw-ui",))
        self.assertIn("1 item(s) done in 24 h", text)
        self.assertIn("b1", text)


class Cost(unittest.TestCase):
    def test_tokens_per_done_item(self):
        items = [item("d1", "done", hist=[("done", NOW - 3600, "")]), item("d2", "done", hist=[("done", NOW - 7200, "")])]
        jobs = {"jobs": {"a": {"cliTokens": 3_000_000, "spawnedAt": (NOW - 5000) * 1000, "endedAt": (NOW - 4000) * 1000},
                         "b": {"cliTokens": 1_000_000, "spawnedAt": (NOW - 2 * 86400) * 1000, "endedAt": (NOW - 2 * 86400) * 1000}}}
        self.assertEqual(watch.cost_24h(jobs, items, NOW), (3_000_000, 2, 1_500_000))
        self.assertIn("1.5M per item done", watch.digest_text(items, [], NOW, (), jobs))
        self.assertIn("no item done", watch.digest_text([], [], NOW, (), jobs))
        jobs["jobs"]["a"]["verdict"] = "blocked"
        self.assertIn("Task ends: blocked 1", watch.digest_text(items, [], NOW, (), jobs))


if __name__ == "__main__":
    unittest.main()
