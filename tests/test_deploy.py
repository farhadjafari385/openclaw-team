"""bin/deploy: failing tests stop the deploy before the gateway is touched.

Runs the real script in a sandbox HOME with fake systemctl/journalctl/sleep on PATH and a token-guard
archive whose node test fails. (On 2026-11-07 a deploy printed "fail 1" and restarted anyway.)
"""
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = """#!/bin/bash
echo "$(basename "$0") $*" >> "$HOME/calls.log"
"""


def git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)


@unittest.skipUnless(shutil.which("node") and shutil.which("git"), "needs node and git")
class DeployTestGate(unittest.TestCase):
    def run_deploy(self, node_test, jobs='{"jobs": {}, "orcActivity": {}}', env_extra=None, sleep_extra="", gateway_age=None):
        home = Path(tempfile.mkdtemp(prefix="deploy-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / ".openclaw" / "token-guard-state").mkdir(parents=True)
        (home / ".openclaw" / "token-guard-state" / "jobs.json").write_text(jobs)
        (home / ".openclaw" / "gateway.systemd.env").write_text("")
        bindir = home / ".openclaw" / "bin"
        bindir.mkdir()
        for name in ("systemctl", "journalctl", "sleep", "pkill", "pgrep", "ps"):
            # pgrep finds no gateway: the test must never see or touch a real process
            extra = {"pgrep": "exit 1\n", "sleep": sleep_extra,
                     # a running gateway of a given age, or none
                     "systemctl": '[ "$2" = "show" ] && echo %s\n' % ("4242" if gateway_age is not None else "0"),
                     "ps": "echo %s\n" % (gateway_age if gateway_age is not None else "")}.get(name, "")
            (bindir / name).write_text(FAKE + extra)
            (bindir / name).chmod(0o755)
        # the team checkout already on the host (its tests pass)
        team = home / "openclaw-team"
        (team / "tests").mkdir(parents=True)
        (team / "tests" / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        (team / "bin").mkdir()
        (team / "bin" / "team").write_text(FAKE)
        (team / "bin" / "team").chmod(0o755)
        git_repo(team)
        # the token-guard archive being deployed
        src = home / "src" / "openclaw-token-guard"
        (src / "plugin" / "test").mkdir(parents=True)
        (src / "plugin" / "test" / "a.test.js").write_text(node_test)
        git_repo(src)
        archive = home / "tg.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(src, arcname="openclaw-token-guard")
        env = {"HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}", "ORC_QUIET_MIN": "0", "MAX_WAIT_MIN": "1", **(env_extra or {})}
        out = subprocess.run(["bash", str(REPO / "bin" / "deploy"), str(archive)], env=env, capture_output=True, text=True, timeout=120)
        calls = (home / "calls.log").read_text() if (home / "calls.log").exists() else ""
        flag = (home / ".openclaw" / "token-guard-state" / "maintenance").exists()
        return out, calls, flag

    def test_failing_test_stops_before_restart(self):
        out, calls, flag = self.run_deploy("import test from 'node:test';\nimport assert from 'node:assert';\ntest('x', () => assert.equal(1, 2));\n")
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("tests FAILED: not deploying", out.stdout)
        self.assertNotIn("stop openclaw-gateway", calls)
        self.assertNotIn("team apply", calls)
        self.assertIn("start openclaw-team-drift.timer", calls)
        self.assertFalse(flag, "the maintenance flag must be removed")

    def test_passing_tests_go_on_to_the_restart(self):
        out, calls, _ = self.run_deploy("import test from 'node:test';\ntest('x', () => {});\n")
        self.assertNotIn("tests FAILED", out.stdout)
        self.assertIn("stop openclaw-gateway", calls)


    PASS = "import test from 'node:test';\ntest('x', () => {});\n"
    RUNNING = '{"jobs": {"agent:growth:subagent:g1": {"status": "running", "label": "ai: directories"}}, "orcActivity": {}}'

    def test_a_busy_team_is_drained_then_restarted(self):
        # 2026-11-10: three tasks ran back to back and the deploy waited 90 minutes for a quiet moment.
        # The fake sleep ends the running task once the flag is on, like a task finishing while no new one starts.
        finish = """f="$HOME/.openclaw/token-guard-state"
if [ -f "$f/maintenance" ] && grep -q running "$f/jobs.json"; then echo "task ended while the flag was on" >> "$HOME/calls.log"; echo '{"jobs": {}, "orcActivity": {}}' > "$f/jobs.json"; fi
"""
        out, calls, flag = self.run_deploy(self.PASS, jobs=self.RUNNING, env_extra={"DRAIN_AFTER_MIN": "0"}, sleep_extra=finish)
        self.assertIn("no quiet moment: maintenance flag on", out.stdout)
        self.assertIn("task ended while the flag was on", calls)
        self.assertLess(calls.index("task ended while the flag was on"), calls.index("stop openclaw-gateway"))
        self.assertFalse(flag, "the maintenance flag must be removed after the restart")

    def test_a_busy_team_without_a_drain_is_not_restarted(self):
        out, calls, flag = self.run_deploy(self.PASS, jobs=self.RUNNING, env_extra={"MAX_WAIT_MIN": "0", "DRAIN_AFTER_MIN": "999"})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("never quiet: not deploying", out.stdout)
        self.assertNotIn("stop openclaw-gateway", calls)
        self.assertFalse(flag)

    def test_a_gateway_still_starting_is_not_restarted_again(self):
        # 2026-11-11: the locked-agent check restarted the gateway at 22:38; this script restarted it again at 22:41.
        out, calls, flag = self.run_deploy(self.PASS, env_extra={"MAX_WAIT_MIN": "0"}, gateway_age=5 * 60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("gateway started 5 min ago", out.stdout)
        self.assertIn("never quiet: not deploying", out.stdout)
        self.assertNotIn("stop openclaw-gateway", calls)
        self.assertFalse(flag)

    def test_an_older_gateway_is_deployed_and_our_timers_pause_for_the_deploy(self):
        out, calls, _ = self.run_deploy(self.PASS, gateway_age=45 * 60)
        timers = "openclaw-team-heal.timer openclaw-team-gc.timer openclaw-team-disk.timer openclaw-team-graphs.timer"
        self.assertIn("stop openclaw-gateway", calls)
        self.assertLess(calls.index(f"systemctl --user stop {timers}"), calls.index("stop openclaw-gateway"))
        self.assertGreater(calls.rindex(f"systemctl --user start {timers}"), calls.index("start openclaw-gateway"))
        # apply starts the timers again: they pause once more until the start is over
        self.assertGreater(calls.rindex(f"systemctl --user stop {timers}"), calls.index("team apply"))
        self.assertLess(calls.rindex(f"systemctl --user stop {timers}"), calls.index("start openclaw-gateway"))
        # the timers run again before the final check, which counts them
        resumed = calls.find(f"systemctl --user start {timers}", calls.rindex(f"systemctl --user stop {timers}"))
        self.assertTrue(0 <= resumed < calls.index("team verify"), "timers resume after the start, before the check")
        # the owner hears of the restart before the gateway goes away, and again when it is back
        self.assertLess(calls.index("team notify"), calls.index("stop openclaw-gateway"))
        self.assertGreater(calls.rindex("team notify"), calls.index("team verify"))

    def test_our_timers_keep_running_while_the_deploy_waits_for_its_quiet_moment(self):
        # 2026-11-12: a deploy waited hours for a quiet moment with the locked-agent check and disk janitor off.
        out, calls, _ = self.run_deploy(self.PASS, jobs=self.RUNNING, env_extra={"MAX_WAIT_MIN": "0", "DRAIN_AFTER_MIN": "999"})
        self.assertIn("never quiet: not deploying", out.stdout)
        self.assertNotIn("stop openclaw-team-heal.timer", calls)
        # once the running task ends, the deploy goes ahead and only then pauses them
        end = """f="$HOME/.openclaw/token-guard-state"
if grep -q running "$f/jobs.json"; then echo "task ended" >> "$HOME/calls.log"; echo '{"jobs": {}, "orcActivity": {}}' > "$f/jobs.json"; fi
"""
        out, calls, _ = self.run_deploy(self.PASS, jobs=self.RUNNING, env_extra={"DRAIN_AFTER_MIN": "999"}, sleep_extra=end, gateway_age=45 * 60)
        self.assertIn("stop openclaw-gateway", calls)
        self.assertLess(calls.index("task ended"), calls.index("stop openclaw-team-heal.timer"))
        self.assertLess(calls.index("stop openclaw-team-heal.timer"), calls.index("stop openclaw-gateway"))

    def test_the_timers_come_back_when_the_deploy_gives_up(self):
        out, calls, _ = self.run_deploy(self.PASS, jobs=self.RUNNING, env_extra={"MAX_WAIT_MIN": "0", "DRAIN_AFTER_MIN": "999"})
        self.assertIn("never quiet: not deploying", out.stdout)
        self.assertIn("systemctl --user start openclaw-team-heal.timer", calls)


if __name__ == "__main__":
    unittest.main()
