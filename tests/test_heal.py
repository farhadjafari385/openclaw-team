import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import heal  # noqa: E402

T0 = 1794416000.0  # 2026-11-11 ~16:13 UTC


def line(t, text):
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000+00:00", time.gmtime(t))
    return '{"0":"{\\"subsystem\\":\\"x\\"}","1":"%s","time":"%s"}\n' % (text, iso)


START = 'starting...'
LOCK = ('telegram dispatch failed: AgentDatabaseAdmissionError: Agent coordinator (/srv/agents/coordinator/agent/openclaw-agent.sqlite): '
        'worker task timed out\\nSessions remain unavailable. Stop the Gateway, run openclaw doctor --fix, and restart.')
LOCKED_DB = ('SQLite writer failed: Agent devops (/srv/agents/devops/agent/openclaw-agent.sqlite): database is locked'
             '\\nSessions remain unavailable. Stop the Gateway, run openclaw doctor --fix to inspect and repair this agent database, and restart.')
WARN = 'main-session startup orphan marking failed before channel startup: WorkerTaskError: worker task timed out'


class DecideTest(unittest.TestCase):
    def evs(self, *rows):
        return heal.events([line(t, x) for t, x in rows])

    def test_a_lock_after_the_last_start_restarts_when_nothing_runs(self):
        e = self.evs((T0, START), (T0 + 600, LOCK), (T0 + 660, LOCK))
        d = heal.decide(e, T0 + 700, [], {})
        self.assertEqual(d["action"], "restart")
        self.assertEqual(d["agents"], ["coordinator"])

    def test_the_refusals_while_starting_up_and_a_single_line_are_not_a_lock(self):
        boot = ('Subagent announce failed: GatewayClientRequestError: Agent devops has not completed startup inspection and preparation.'
                '\\nSessions remain unavailable until background inspection and preparation finish. If they cannot complete, stop the Gateway.')
        e = self.evs((T0, START), (T0 + 60, boot), (T0 + 200, boot))
        self.assertEqual(heal.decide(e, T0 + 250, [], {})["action"], "none")
        e = self.evs((T0, START), (T0 + 600, LOCK))
        self.assertEqual(heal.decide(e, T0 + 620, [], {})["action"], "none")  # one line: not yet

    def test_a_locked_database_is_the_same_lasting_state(self):
        e = self.evs((T0, START), (T0 + 600, LOCKED_DB), (T0 + 700, LOCKED_DB))
        d = heal.decide(e, T0 + 720, [], {})
        self.assertEqual((d["action"], d["agents"]), ("restart", ["devops"]))

    def test_a_reply_runtime_never_published_counts_only_after_the_startup_grace(self):
        unpub = 'telegram dispatch failed: Error: prepared reply dispatch runtime owner was not published for coordinator'
        e = self.evs((T0, START), (T0 + 300, unpub), (T0 + 420, unpub))
        self.assertEqual(heal.decide(e, T0 + 430, [], {})["action"], "none")  # still starting up
        e = self.evs((T0, START), (T0 + 620, unpub), (T0 + 700, unpub))
        d = heal.decide(e, T0 + 710, [], {})
        self.assertEqual((d["action"], d["agents"]), ("restart", ["coordinator"]))

    def test_a_lock_from_before_the_last_start_is_over(self):
        e = self.evs((T0, START), (T0 + 600, LOCK), (T0 + 900, START))
        self.assertEqual(heal.decide(e, T0 + 950, [], {})["action"], "none")

    def test_the_harmless_startup_timeout_warning_is_not_a_lock(self):
        e = self.evs((T0, START), (T0 + 200, WARN))
        self.assertEqual(heal.decide(e, T0 + 250, [], {})["action"], "none")

    def test_running_tasks_finish_first(self):
        e = self.evs((T0, START), (T0 + 600, LOCK), (T0 + 660, LOCK))
        d = heal.decide(e, T0 + 670, ["ai: fix checkout"], {})
        self.assertEqual(d["action"], "wait")

    def test_no_restart_during_a_deploy_or_within_30_minutes_of_the_last(self):
        e = self.evs((T0, START), (T0 + 590, LOCK), (T0 + 650, LOCK))
        self.assertEqual(heal.decide(e, T0 + 650, [], {}, flag_age_s=60, deploy_running=True)["action"], "none")
        self.assertEqual(heal.decide(e, T0 + 650, [], {}, flag_age_s=3600, deploy_running=True)["action"], "none")
        self.assertEqual(heal.decide(e, T0 + 650, [], {}, flag_age_s=60)["action"], "none")  # someone else's flag
        self.assertEqual(heal.decide(e, T0 + 650, [], {"flagSetAt": T0 + 300}, flag_age_s=60)["action"], "restart")  # our own flag
        self.assertEqual(heal.decide(e, T0 + 650, [], {"heals": [T0 + 100]})["action"], "none")

    def test_a_deploy_still_waiting_for_its_quiet_moment_does_not_hold_off_the_heal(self):
        # 2026-11-12: a deploy waited up to 6 hours for a quiet moment; a lock meanwhile must not stay until it ends.
        e = self.evs((T0, START), (T0 + 590, LOCK), (T0 + 650, LOCK))
        self.assertEqual(heal.decide(e, T0 + 650, [], {}, deploy_running=True)["action"], "restart")

    def test_three_restarts_in_six_hours_go_to_the_operator(self):
        e = self.evs((T0, START), (T0 + 590, LOCK), (T0 + 650, LOCK))
        d = heal.decide(e, T0 + 650, [], {"heals": [T0 - 5 * 3600, T0 - 3 * 3600, T0 - 3600]})
        self.assertEqual(d["action"], "escalate")

    def test_the_agent_is_named_from_either_form_of_the_refusal(self):
        cron = '{"0":"{\\"subsystem\\":\\"cron/maintenance\\"}","1":{"error":{"refusal":{"agentId":"writer","reason":"worker task timed out. Sessions remain unavailable. Stop the Gateway and restart."}}},"time":"%s"}\n'
        iso = time.strftime("%Y-%m-%dT%H:%M:%S.000+00:00", time.gmtime(T0 + 570))
        e = heal.events([line(T0, START), cron % iso, line(T0 + 610, LOCK), line(T0 + 640, LOCK)])
        self.assertEqual(heal.decide(e, T0 + 650, [], {})["agents"], ["coordinator", "writer"])

    def test_a_stale_lock_line_is_not_acted_on(self):
        e = self.evs((T0, START), (T0 + 600, LOCK))
        self.assertEqual(heal.decide(e, T0 + 600 + 16 * 60, [], {})["action"], "none")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.sdir = self.home / ".openclaw" / "token-guard-state"
        self.sdir.mkdir(parents=True)
        self.logs = self.home / "logs"
        self.logs.mkdir()
        (self.logs / "openclaw-2026-11-11.log").write_text(line(T0, START) + line(T0 + 580, LOCK) + line(T0 + 640, LOCK))
        self.sent, self.restarts = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, now):
        return heal.run(self.sent.append, home=self.home, log_dir=str(self.logs), now=now,
                        restart=lambda: self.restarts.append(now), sleep=lambda s: None, deploy_running=False)

    def jobs(self, running):
        (self.sdir / "jobs.json").write_text(json.dumps({"jobs": {"k": {"status": "running" if running else "ended", "label": "ai: fix"}}}))

    def test_waits_for_running_tasks_with_the_flag_on_then_restarts_and_clears_it(self):
        self.jobs(True)
        self.assertEqual(self.go(T0 + 650)["action"], "wait")
        self.assertTrue((self.sdir / "maintenance").exists(), "no new tasks while waiting")
        self.assertEqual(len(self.sent), 1)
        self.go(T0 + 700)  # still running: no second message
        self.assertEqual(len(self.sent), 1)
        self.jobs(False)
        self.assertEqual(self.go(T0 + 800)["action"], "restart")
        self.assertEqual(self.restarts, [T0 + 800])
        self.assertFalse((self.sdir / "maintenance").exists(), "the flag is cleared after the restart")
        self.assertIn("restarted the gateway", self.sent[-1])
        self.assertEqual(self.go(T0 + 900)["action"], "none")  # cooldown: one restart per incident
        self.assertEqual(self.restarts, [T0 + 800])

    def test_leaves_a_deploys_flag_alone(self):
        (self.sdir / "maintenance").touch()
        d = heal.run(self.sent.append, home=self.home, log_dir=str(self.logs), now=time.time(),
                     restart=lambda: self.restarts.append(1), sleep=lambda s: None, deploy_running=False)
        self.assertEqual(d["action"], "none")  # the lock line is old relative to time.time(): nothing to do
        self.assertTrue((self.sdir / "maintenance").exists())
        self.assertEqual(self.restarts, [])


class WiringTest(unittest.TestCase):
    def test_the_check_runs_every_2_minutes_as_its_own_unit(self):
        import teamlib
        self.assertIn("openclaw-team-heal.timer", teamlib.TIMERS)
        self.assertIn("OnUnitActiveSec=2min", teamlib.UNITS["openclaw-team-heal.timer"])
        self.assertIn("bin/team heal", teamlib.UNITS["openclaw-team-heal.service"])


class StartupWindowTest(unittest.TestCase):
    def test_the_teams_own_heavy_jobs_wait_out_a_gateway_start(self):
        import teamlib
        self.assertTrue(teamlib.in_startup_window(5 * 60))
        self.assertFalse(teamlib.in_startup_window(25 * 60))
        self.assertTrue(teamlib.in_startup_window(None), "a stopped gateway is about to start")
        calls = []
        orig = (teamlib.gateway_age_s, teamlib.run)
        self.addCleanup(lambda: (setattr(teamlib, "gateway_age_s", orig[0]), setattr(teamlib, "run", orig[1])))
        teamlib.gateway_age_s = lambda: 120
        teamlib.run = lambda *a, **k: calls.append(a) or None
        args = type("A", (), {"dry_run": False, "all": False, "force": False, "fix": True, "notify": False})()
        for cmd in (teamlib.cmd_gc, teamlib.cmd_disk, teamlib.cmd_graphs, teamlib.cmd_drift):
            cmd(args)
        self.assertEqual(calls, [], "nothing ran during the start")

    def test_a_stopped_gateway_or_a_restart_under_way_holds_them_back_too(self):
        # 2026-11-12: a deploy's apply started the timers while the gateway was stopped; the session clean-up and
        # code maps then ran through the whole start (20 CPU minutes) and the orchestrator answered after 15 minutes.
        import tempfile, time
        import teamlib
        orig = (teamlib.gateway_age_s, teamlib.MAINTENANCE_FLAG)
        self.addCleanup(lambda: (setattr(teamlib, "gateway_age_s", orig[0]), setattr(teamlib, "MAINTENANCE_FLAG", orig[1])))
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        teamlib.MAINTENANCE_FLAG = Path(tmp.name) / "maintenance"
        teamlib.gateway_age_s = lambda: None
        self.assertTrue(teamlib.skip_during_startup("session clean-up"))
        teamlib.gateway_age_s = lambda: 45 * 60
        self.assertFalse(teamlib.skip_during_startup("session clean-up"))
        teamlib.MAINTENANCE_FLAG.touch()
        self.assertTrue(teamlib.skip_during_startup("session clean-up"))
        old = time.time() - 31 * 60
        os.utime(teamlib.MAINTENANCE_FLAG, (old, old))
        self.assertFalse(teamlib.skip_during_startup("session clean-up"), "a flag left behind does not stop them for good")


if __name__ == "__main__":
    unittest.main()
