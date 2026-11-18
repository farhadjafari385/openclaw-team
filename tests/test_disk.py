"""lib/disk.py: the janitor removes task leftovers, never work or services, and warns before the disk fills.

On 2026-11-07 the disk reached 100% and the whole team stopped for 9 hours. The leftovers: Chrome
profiles of killed Puppeteer runs (15.7 GB), finished git worktrees with node_modules (31 GB), test
copies in /tmp (9 GB), and test database containers with their volumes.
"""
import calendar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))
import disk  # noqa: E402
import teamlib  # noqa: E402

NOW = time.time()
H = 3600


def age(path, hours):
    """Make everything under `path` look untouched for `hours`."""
    t = NOW - hours * H
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            os.utime(os.path.join(root, n), (t, t), follow_symlinks=False)
    os.utime(path, (t, t))


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def container(name, status="running", restart="no", labels=None, image="postgres:16-alpine", hours=72, finished_hours=None):
    created = time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(NOW - hours * H))
    finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - finished_hours * H)) if finished_hours is not None else "0001-01-01T00:00:00Z"
    return {"Id": name, "Name": "/" + name, "Created": created, "State": {"Status": status, "FinishedAt": finished},
            "HostConfig": {"RestartPolicy": {"Name": restart}}, "Config": {"Image": image, "Labels": labels or {}}}


class Rules(unittest.TestCase):
    def test_a_copy_is_removed_only_when_nothing_can_be_lost(self):
        self.assertEqual(disk.classify_copy(72, 0, 0, 48), "remove")
        self.assertEqual(disk.classify_copy(72, 3, 0, 48), "slim", "uncommitted work: only node_modules goes")
        self.assertEqual(disk.classify_copy(72, 0, 1, 48), "slim", "a commit on no remote: only node_modules goes")
        self.assertEqual(disk.classify_copy(5, 0, 0, 48), "keep", "maybe a task still uses it")
        self.assertEqual(disk.classify_copy(None, 0, 0, 48), "keep")

    def test_only_containers_a_task_left_behind_are_removed(self):
        self.assertTrue(disk.leftover_container(container("ci-fast-pg"), NOW, 48), "a test database left running for 3 days")
        self.assertTrue(disk.leftover_container(container("qa-tests", "exited", image="node:22-bookworm-slim", hours=300, finished_hours=290), NOW, 48))
        self.assertTrue(disk.leftover_container(container("x", "created", image="node:22", hours=50), NOW, 48))
        self.assertFalse(disk.leftover_container(container("fresh-pg", hours=2), NOW, 48), "a task may still use it")
        self.assertFalse(disk.leftover_container(container("app-db", restart="unless-stopped"), NOW, 48), "a service has a restart policy")
        self.assertFalse(disk.leftover_container(container("hub-db", restart="always"), NOW, 48))
        self.assertFalse(disk.leftover_container(container("shop-db", labels={"com.docker.compose.project": "shop"}), NOW, 48))
        self.assertFalse(disk.leftover_container(container("site", labels={"coolify.managed": "true"}), NOW, 48))
        self.assertFalse(disk.leftover_container(container("my-app", image="registry.example/team/app:1"), NOW, 48),
                         "a running container of a non-test image is never guessed to be a leftover")
        self.assertTrue(disk.leftover_container(container("old-app", "exited", image="registry.example/team/app:1", finished_hours=60), NOW, 48))
        self.assertFalse(disk.leftover_container(container("ci-fast-pg"), NOW, 48, keep=["ci-fast-pg"]))
        self.assertFalse(disk.leftover_container(container("new-exit", "exited", hours=100, finished_hours=3), NOW, 48),
                         "counted from when it stopped")

    def test_only_unnamed_unused_volumes_are_removed(self):
        anon = "a" * 64
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 72 * H))
        young = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 2 * H))
        self.assertTrue(disk.leftover_volume({"Name": anon, "CreatedAt": old}, NOW, 48))
        self.assertFalse(disk.leftover_volume({"Name": anon, "CreatedAt": young}, NOW, 48))
        self.assertFalse(disk.leftover_volume({"Name": "src_pgdata", "CreatedAt": old}, NOW, 48), "a named volume is data")
        self.assertFalse(disk.leftover_volume({"Name": "runner-cache-" + "a" * 40, "CreatedAt": old}, NOW, 48))

    def test_build_cache_always_and_unused_ci_caches_only_when_the_disk_is_filling(self):
        old = "2026-11-01T00:00:00Z"
        cache = "runner-thj5j-48u-project-87818942-concurrent-4-cache-c33bcaa1fd2c77edfc3893b41966cea8"
        vols = [{"Name": cache, "CreatedAt": old}, {"Name": "src_pgdata", "CreatedAt": old}, {"Name": "aihub-runner-cache", "CreatedAt": old}]
        calls = []

        def fake(cmd, timeout=300):
            calls.append(cmd)
            out = ""
            if cmd[:3] == ["docker", "volume", "ls"]:
                out = "\n".join(v["Name"] for v in vols)
            elif cmd[:3] == ["docker", "volume", "inspect"]:
                out = json.dumps(vols)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        orig = disk.run
        self.addCleanup(lambda: setattr(disk, "run", orig))
        disk.run = fake
        cfg = dict(disk.DEFAULTS)
        disk.clean_docker(cfg, NOW, lambda m: None)
        removed = [c for c in calls if c[:3] == ["docker", "volume", "rm"]]
        self.assertEqual(removed, [], "no pressure: CI caches stay")
        self.assertIn(["docker", "builder", "prune", "-f", "--filter", "until=72h"], calls)
        calls.clear()
        n = disk.clean_docker(cfg, NOW, lambda m: None, pressure=True)
        removed = [c for c in calls if c[:3] == ["docker", "volume", "rm"]]
        self.assertEqual(removed, [["docker", "volume", "rm", cache]], "only the runner job cache; named data volumes stay")
        self.assertEqual(n, 1)
        self.assertIn(["docker", "builder", "prune", "-f", "--filter", "until=24h"], calls)
        calls.clear()
        disk.clean_docker(cfg, NOW, lambda m: None, dry=True, pressure=True)
        self.assertFalse([c for c in calls if c[1:3] in (["volume", "rm"], ["builder", "prune"])], "a dry run removes nothing")

    def test_docker_times(self):
        self.assertAlmostEqual(disk.parse_ts("2026-11-05T21:42:31.123456789Z"), calendar.timegm((2026, 11, 5, 21, 42, 31)) + 0.123456, places=3)
        self.assertEqual(disk.parse_ts("2026-11-05T16:42:31-05:00"), disk.parse_ts("2026-11-05T21:42:31Z"))
        self.assertIsNone(disk.parse_ts("0001-01-01T00:00:00Z"))
        self.assertIsNone(disk.parse_ts(""))

    def test_the_alarm_is_loud_when_critical_and_not_spam_when_high(self):
        cfg = disk.config({})
        self.assertEqual(disk.alarm(95, {"notifiedAt": NOW - 60}, NOW, cfg), "critical", "every run")
        self.assertEqual(disk.alarm(84, {}, NOW, cfg), "warn")
        self.assertIsNone(disk.alarm(84, {"notifiedAt": NOW - 2 * H}, NOW, cfg), "warned 2 h ago")
        self.assertEqual(disk.alarm(84, {"notifiedAt": NOW - 7 * H}, NOW, cfg), "warn")
        self.assertIsNone(disk.alarm(60, {}, NOW, cfg))

    def test_the_timer_runs_it_and_verify_checks_it(self):
        self.assertIn("openclaw-team-disk.timer", teamlib.TIMERS)
        self.assertIn("bin/team disk", teamlib.UNITS["openclaw-team-disk.service"])
        self.assertIn("OnUnitActiveSec=30min", teamlib.UNITS["openclaw-team-disk.timer"])
        src = (REPO / "lib" / "teamlib.py").read_text()
        self.assertIn("disk.usage()", src.split("\ndef checks(")[1].split("\ndef ")[0], "verify reports the disk")

    def test_workers_are_told_to_leave_nothing_behind(self):
        self.assertIn("Leave nothing behind", (REPO / "rules" / "worker.md").read_text())


@unittest.skipUnless(shutil.which("git"), "needs git")
class OnDisk(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="disk-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.cfg = disk.config({"disk": {}})
        self.cfg.update(repo_roots=[str(self.root / "projects")], tmp_dirs=[str(self.tmp)], root_tmp_dirs=[],
                        backup_dir=str(self.root / "backup"), state_file=str(self.root / "state" / "disk.json"), tmp_min_mb=0)
        self.log = []

    def repo(self):
        """projects/app pushed to a bare remote."""
        remote = self.root / "remote.git"
        sh("git", "init", "-q", "--bare", str(remote))
        app = self.root / "projects" / "app"
        app.mkdir(parents=True)
        sh("git", "init", "-q", "-b", "main", str(app))
        (app / "a.txt").write_text("a\n")
        (app / ".gitignore").write_text("node_modules/\n")
        for cmd in (["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
                    ["remote", "add", "origin", str(remote)], ["push", "-q", "origin", "main"]):
            sh("git", "-C", str(app), *cmd)
        return app

    def worktree(self, app, name, branch):
        path = self.root / "wt" / name
        sh("git", "-C", str(app), "worktree", "add", "-q", "-b", branch, str(path))
        nm = path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("x" * 1000)
        return path

    def test_idle_worktrees_go_and_work_is_never_lost(self):
        app = self.repo()
        sh("git", "-C", str(app), "push", "-q", "origin", "main:clean")
        clean = self.worktree(app, "clean", "clean-branch")
        sh("git", "-C", str(clean), "push", "-q", "origin", "clean-branch")
        dirty = self.worktree(app, "dirty", "dirty-branch")
        (dirty / "a.txt").write_text("unsaved work\n")
        (dirty / "new.txt").write_text("new file\n")
        fresh = self.worktree(app, "fresh", "fresh-branch")
        sh("git", "-C", str(fresh), "push", "-q", "origin", "fresh-branch")
        for p in (clean, dirty):
            age(p, 72)
        n = disk.clean_worktrees(self.cfg, NOW, self.log.append)
        self.assertEqual(n, 2, self.log)
        self.assertFalse(clean.exists(), "a clean, pushed, idle worktree is removed")
        self.assertIn("clean-branch", subprocess.run(["git", "-C", str(app), "branch"], capture_output=True, text=True).stdout, "its branch stays")
        self.assertNotIn(str(clean), subprocess.run(["git", "-C", str(app), "worktree", "list"], capture_output=True, text=True).stdout)
        self.assertEqual((dirty / "a.txt").read_text(), "unsaved work\n", "uncommitted work stays in place")
        self.assertTrue((dirty / "new.txt").exists())
        self.assertFalse((dirty / "node_modules").exists(), "only node_modules goes")
        patches = list((self.root / "backup").glob("*dirty*.patch"))
        self.assertTrue(patches and "unsaved work" in patches[0].read_text(), "the diff is saved first")
        self.assertTrue(list((self.root / "backup").glob("*dirty*.untracked.tgz")))
        self.assertTrue((fresh / "node_modules").exists(), "a worktree used today is not touched")
        # a second run finds nothing left to do
        self.log.clear()
        self.assertEqual(disk.clean_worktrees(self.cfg, NOW, self.log.append), 0)
        self.assertEqual(self.log, [], "the slim worktree is not saved again")
        age(dirty, 72)
        self.assertEqual(disk.clean_worktrees(self.cfg, NOW, self.log.append), 1)
        self.assertEqual(self.log, [], "nothing left to drop, nothing saved again")

    def test_tmp_leftovers_go_but_kept_names_and_fresh_ones_stay(self):
        old_copy = self.tmp / "qa-mr5"
        (old_copy / "main").mkdir(parents=True)
        (old_copy / "main" / "shot.png").write_bytes(b"x" * 2000)
        kept = self.tmp / "openclaw"
        kept.mkdir()
        (kept / "gateway.log").write_text("log")
        account = self.tmp / "qa891f"
        account.mkdir()
        fresh = self.tmp / "ek-wt-today"
        fresh.mkdir()
        (fresh / "f").write_text("x")
        old_profile = self.tmp / "puppeteer_dev_chrome_profile-AbC123"
        (old_profile / "Default").mkdir(parents=True)
        young_profile = self.tmp / "puppeteer_dev_chrome_profile-New999"
        young_profile.mkdir()
        app = self.repo()
        clone = self.tmp / "mr28t"
        sh("git", "clone", "-q", str(self.root / "remote.git"), str(clone))
        own = self.tmp / "local-only"
        sh("git", "init", "-q", str(own))
        (own / "work.txt").write_text("never pushed")
        for p in (old_copy, kept, account, clone, own):
            age(p, 100)
        age(old_profile, 5)
        entries = dict((Path(p).name, k) for p, k in disk.stale_tmp_entries(str(self.tmp), NOW, self.cfg))
        self.assertEqual(entries, {"qa-mr5": "copy", "puppeteer_dev_chrome_profile-AbC123": "profile", "mr28t": "git", "local-only": "git"})
        disk.clean_tmp(self.cfg, NOW, self.log.append)
        self.assertFalse(old_copy.exists())
        self.assertFalse(old_profile.exists())
        self.assertFalse(clone.exists(), "a clean clone with nothing unpushed is removed")
        self.assertTrue((own / "work.txt").exists(), "a repo with work on no remote is kept")
        for p in (kept, account, fresh, young_profile, app):
            self.assertTrue(p.exists(), p)

    def test_root_only_browser_profiles_are_matched_by_the_container_script(self):
        folder = self.root / "snap-tmp"
        old = folder / "puppeteer_dev_chrome_profile-old1"
        (old / "Default").mkdir(parents=True)
        new = folder / "puppeteer_dev_chrome_profile-new1"
        new.mkdir()
        other = folder / "keep-me"
        other.mkdir()
        lock = folder / ".org.chromium.Chromium.x1"
        lock.write_text("x")
        for p in (old, other, lock):
            age(p, 5)
        out = subprocess.run(["sh", "-c", disk.root_profile_script(str(folder), 180, True)], capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "2")
        self.assertTrue(old.exists(), "a dry run removes nothing")
        out = subprocess.run(["sh", "-c", disk.root_profile_script(str(folder), 180, False)], capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "2")
        self.assertFalse(old.exists())
        self.assertFalse(lock.exists())
        self.assertTrue(new.exists() and other.exists())
        missing = subprocess.run(["sh", "-c", disk.root_profile_script(str(self.root / "nope"), 180, False)], capture_output=True, text=True).stdout
        self.assertEqual(missing.strip(), "0")

    def test_the_operator_is_warned_once_and_told_every_run_when_critical(self):
        sent = []
        orig = (disk.usage, disk.clean_docker, disk.clean_tmp, disk.clean_worktrees)
        self.addCleanup(lambda: setattr_all(orig))

        def setattr_all(o):
            disk.usage, disk.clean_docker, disk.clean_tmp, disk.clean_worktrees = o
        disk.clean_docker = disk.clean_tmp = disk.clean_worktrees = lambda *a, **k: 0
        team = {"disk": {k: self.cfg[k] for k in ("state_file", "backup_dir", "repo_roots", "tmp_dirs", "root_tmp_dirs")}}
        notify = lambda text: sent.append(text) or True
        disk.usage = lambda path="/": (84.0, 25.0)
        disk.janitor(team, notify, now=NOW, log=lambda m: None)
        disk.janitor(team, notify, now=NOW + 1800, log=lambda m: None)
        self.assertEqual(len(sent), 1, "a high disk is reported once per 6 hours")
        self.assertIn("84.0% full", sent[0])
        self.assertIn("whole team stops", sent[0])
        disk.usage = lambda path="/": (95.0, 8.0)
        disk.janitor(team, notify, now=NOW + 3600, log=lambda m: None)
        disk.janitor(team, notify, now=NOW + 5400, log=lambda m: None)
        self.assertEqual(len(sent), 3, "critical: every run")
        self.assertTrue(sent[-1].startswith("🚩"))
        state = json.loads(Path(self.cfg["state_file"]).read_text())
        self.assertEqual(state["percent"], 95.0)
        disk.usage = lambda path="/": (50.0, 80.0)
        disk.janitor(team, notify, now=NOW + 7200, log=lambda m: None)
        self.assertEqual(len(sent), 3)
        self.assertNotIn("notifiedAt", json.loads(Path(self.cfg["state_file"]).read_text()), "back to normal resets the clock")


if __name__ == "__main__":
    unittest.main()
