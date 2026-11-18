import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import graphs  # noqa: E402
import teamlib  # noqa: E402

# Tests use the example private values, so they pass the same in a public clone and on the host.
os.environ["OPENCLAW_TEAM_PRIVATE"] = str(Path(__file__).resolve().parents[1] / "private.example.toml")
P = teamlib.fill

FAKE_GRAPHIFY = """#!/bin/sh
# Stands in for `graphify update <src> --force`: records what it saw and writes a graph.
mkdir -p "$GRAPHIFY_OUT"
find "$2" -type f | sort > "$GRAPHIFY_OUT/seen.txt"
echo '{"nodes": [], "links": []}' > "$GRAPHIFY_OUT/graph.json"
echo "[graphify watch] Rebuilt: 5 nodes, 4 edges, 1 communities"
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


class GraphsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.cfg = dict(graphs.DEFAULTS, root=str(self.t / "graphs"), max_file_kb=4)
        self.exe = self.t / "graphify"
        self.exe.write_text(FAKE_GRAPHIFY)
        self.exe.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, name, files):
        """A clone (on an agent branch, like the host's main checkouts) of a bare origin."""
        origin, work = self.t / f"{name}.git", self.t / "work" / name
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        seed = self.t / f"seed-{name}"
        seed.mkdir()
        git("init", "-q", "-b", "main", cwd=seed)
        for rel, data in files.items():
            (seed / rel).parent.mkdir(parents=True, exist_ok=True)
            (seed / rel).write_bytes(data if isinstance(data, bytes) else data.encode())
        git("add", "-A", cwd=seed)
        git("commit", "-qm", "seed", cwd=seed)
        git("push", "-q", str(origin), "main", cwd=seed)
        work.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
        git("checkout", "-qb", "agent/x", cwd=work)
        (work / "uncommitted.py").write_text("print('not on main')\n")
        return seed, origin, work

    def push(self, seed, origin, rel, data):
        (seed / rel).write_text(data)
        git("add", "-A", cwd=seed)
        git("commit", "-qm", "change", cwd=seed)
        git("push", "-q", str(origin), "main", cwd=seed)

    def test_projects_with_repos_are_mapped(self):
        got = graphs.graph_projects([
            {"name": "ai", "server": "/srv/ai", "graph": ["backend", "frontend"]},
            {"name": "hub", "server": "/srv/hub", "graph": ["."]},
            {"name": "sites", "server": "/srv/<site>", "graph": ["."]},
            {"name": "notes", "server": "/srv/notes"},
            {"name": "laptop-only", "graph": ["."]},
        ])
        self.assertEqual([(n, [(k, str(p)) for k, p in r]) for n, _, r in got],
                         [("ai", [("backend", "/srv/ai/backend"), ("frontend", "/srv/ai/frontend")]), ("hub", [(".", "/srv/hub")])])

    def test_export_takes_source_from_origin_main_and_leaves_binaries_and_big_files_out(self):
        _, _, work = self.repo("app", {"src/a.py": "def a(): pass\n", "img/logo.png": b"\x89PNG", "data/big.json": "x" * 9000, "README.md": "# app\n"})
        dst = self.t / "out" / "src"
        n = graphs.export(work, graphs.default_ref(work), dst, self.cfg)
        files = sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file())
        self.assertEqual(files, ["README.md", "src/a.py"])
        self.assertEqual(n, 2)
        self.assertEqual(graphs.default_ref(work), "origin/main")
        # The agent branch and its uncommitted file in the main checkout never reach the map.
        self.assertFalse((dst / "uncommitted.py").exists())

    def test_a_map_is_rebuilt_only_when_a_repo_moved(self):
        seed, origin, work = self.repo("hub", {"app.py": "def run(): pass\n"})
        state = {"repos": {}, "built": {}}
        repos = [(".", work)]
        r = graphs.build_project("hub", repos, self.cfg, self.exe, state, log=lambda *_: None)
        self.assertTrue(r["built"])
        graph = Path(r["graph"])
        self.assertTrue(graph.exists())
        self.assertEqual(state["built"]["hub"]["nodes"], 5)
        self.assertIn("/hub/src/app.py", (graph.parent / "seen.txt").read_text())
        self.assertFalse(graphs.build_project("hub", repos, self.cfg, self.exe, state, log=lambda *_: None)["built"])
        self.push(seed, origin, "new.py", "def new(): pass\n")
        self.assertTrue(graphs.build_project("hub", repos, self.cfg, self.exe, state, log=lambda *_: None)["built"])
        self.assertIn("/hub/src/new.py", (graph.parent / "seen.txt").read_text())

    def test_a_project_with_several_repos_keeps_each_under_its_name(self):
        _, _, back = self.repo("backend", {"src/api.php": "<?php function api() {}\n"})
        _, _, front = self.repo("frontend", {"src/App.tsx": "export const App = () => null\n"})
        state = {"repos": {}, "built": {}}
        r = graphs.build_project("ai", [("backend", back), ("frontend", front)], self.cfg, self.exe, state, log=lambda *_: None)
        seen = (Path(r["graph"]).parent / "seen.txt").read_text()
        self.assertIn("/ai/src/backend/src/api.php", seen)
        self.assertIn("/ai/src/frontend/src/App.tsx", seen)
        # A repo dropped from the config leaves the map.
        r = graphs.build_project("ai", [("backend", back)], self.cfg, self.exe, state, log=lambda *_: None)
        self.assertTrue(r["built"])
        self.assertNotIn("frontend", (Path(r["graph"]).parent / "seen.txt").read_text())

    def test_the_pinned_tool_is_linked_for_agents_and_checks_report_it(self):
        tool = self.t / "tool"
        (tool / "bin").mkdir(parents=True)
        (tool / "bin" / "python").write_text(f"#!/bin/sh\necho {graphs.DEFAULTS['version']}\n")
        (tool / "bin" / "python").chmod(0o755)
        cfg = dict(self.cfg, tool_dir=str(tool), bin_link=str(self.t / "bin" / "graphify"))
        exe = graphs.ensure_tool(cfg, log=lambda *_: None)
        self.assertEqual(os.readlink(self.t / "bin" / "graphify"), str(exe))
        team = {"graphs": cfg}
        projects = [{"name": "hub", "server": str(self.t / "work" / "hub"), "graph": ["."]}]
        res = graphs.checks(team, projects)
        self.assertTrue(res[0][0], res)
        self.assertFalse(res[1][0])  # never built
        self.assertFalse(res[2][0])  # no map yet
        graphs.save_state(cfg, {"repos": {}, "built": {}, "lastRun": time.time(), "errors": []})
        graphs.graph_file(cfg, "hub").parent.mkdir(parents=True)
        graphs.graph_file(cfg, "hub").write_text("{}")
        self.assertTrue(all(ok for ok, _ in graphs.checks(team, projects)))


class TeamWiringTest(unittest.TestCase):
    def setUp(self):
        self.team, self.projects = teamlib.load()

    def test_every_code_project_on_the_host_has_a_map_and_the_guard_enforces_it(self):
        names = {n for n, _, _ in graphs.graph_projects(self.projects)}
        self.assertEqual(names, {"ai", P("{{private:BRAND_GAME_SLUG}}"), P("{{private:BRAND_AI_SLUG}}-hub"), "openclaw-ui", P("{{private:BRAND_IR_SLUG}}")})
        g = teamlib.guard_config(self.team, self.projects, {}, mirror=False)["graphs"]
        self.assertTrue(g["enabled"])
        self.assertEqual(sorted(g["projects"]), sorted(names))
        self.assertEqual(g["bin"], "~/.openclaw/bin/graphify")
        # Agents with no shell (growth was blocked on 2026-11-08 with no way to run graphify) are not held to it.
        # Growth and social got a shell on 2026-11-09 (owner), so the map rule holds them too.
        self.assertTrue({"writer", "reviewer", "qa", "devops", "product", "designer", "growth", "social"} <= set(g["agents"]))
        self.assertNotIn("strategy", g["agents"])
        self.assertEqual(self.team["graphs"]["version"], graphs.DEFAULTS["version"])

    def test_maps_refresh_on_a_timer_and_workers_are_told(self):
        self.assertIn("openclaw-team-graphs.timer", teamlib.TIMERS)
        self.assertIn("team graphs", teamlib.UNITS["openclaw-team-graphs.service"])
        files = teamlib.render_files(self.team, self.projects)
        devops = next(c for p, c in files.items() if p.endswith("/devops/AGENTS.md"))
        self.assertIn("**Code map first:**", devops)


if __name__ == "__main__":
    unittest.main()
