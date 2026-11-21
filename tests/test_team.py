import os
import sys
import json
import tomllib
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import teamlib  # noqa: E402

# Tests use the example private values, so they pass the same in a public clone and on the host.
os.environ["OPENCLAW_TEAM_PRIVATE"] = str(Path(__file__).resolve().parents[1] / "private.example.toml")
P = teamlib.fill

ROOT = Path(__file__).resolve().parents[1]

LIVE = {
    "channels": {"telegram": {"accounts": {"orchestrator": {"tokenFile": "/s/orc"}, "devops": {"tokenFile": "/s/dev"}}}},
    "agents": {"entries": {"coordinator": {"tools": {"profile": "full", "deny": ["exec"], "alsoAllow": ["task_inbox"]}, "model": {"primary": "anthropic/claude-sonnet-5-5"}}}},
}


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.team, self.projects = teamlib.load()
        self.files = teamlib.render_files(self.team, self.projects)

    def agents_md(self, agent):
        return next(c for p, c in self.files.items() if p.endswith(f"/{agent}/AGENTS.md"))

    def test_every_agent_gets_all_files_well_below_the_truncation_cap(self):
        limit = self.team["limits"]["bootstrap_max_chars"]
        for a in self.team["agents"]:
            for name in teamlib.MANAGED_WORKSPACE_FILES:
                self.assertTrue(any(p.endswith(f"/{a['id']}/{name}") for p in self.files), (a["id"], name))
            self.assertLess(len(self.agents_md(a["id"])), limit * teamlib.BOOTSTRAP_SHARE, a["id"])
            self.assertLessEqual(teamlib.BOOTSTRAP_SHARE, 0.85, "keep headroom below the point where OpenClaw truncates")

    def test_persona_rules_and_facts_reach_spawned_workers(self):
        # Spawned workers only load AGENTS.md, so the persona and all rules must be in it.
        md = self.agents_md("devops")
        self.assertTrue(md.startswith("# Senior Staff DevSecOps Engineer"))
        for needle in ["Hard skills", "## Team rules", "## Your role: CI/CD", "## Doing a task", "RESULT: done", "## Projects", "## Facts", P("{{private:BRAND_GAME_SLUG}}")]:
            self.assertIn(needle, md)
        self.assertNotIn("## Running the team", md)
        orc = self.agents_md("coordinator")
        self.assertIn("login check, never a limit: on `[dispatch]`, resend", orc)  # a login-check refusal was reported to the owner as a limit
        self.assertIn("a merge to `master` deploys to production by itself", orc)  # the backend MR 9 merge shipped to production with no owner OK
        for needle in ["# Senior AI Orchestrator", "## Running the team", "sessions_spawn", "thinking", "## Team rules"]:
            self.assertIn(needle, orc)
        self.assertNotIn("## Doing a task", orc)

    def test_the_owners_postmortem_rule_reaches_every_agent_in_their_words(self):
        # 2026-11-12: the owner's 2026-11-01 rule was cut to one orchestrator line when the rules moved into this repo
        # (23b95b9); for 8 days no agent wrote a postmortem.
        for a in self.team["agents"]:
            md = self.agents_md(a["id"])
            self.assertIn("Postmortem should be BY THE AGENT WHO CAUSE IT! (IF MORE THAN ONE THEN ALL OF THEM SHOULD DO SAME!)", md, a["id"])
            self.assertIn("A REAL ACT TO PREVENT IT FROM U!", md, a["id"])
            self.assertIn("Postmortems topic (2596)", md, a["id"])
        self.assertIn("`Postmortem:`", self.agents_md("devops"))
        self.assertIn("Reply under each with the real preventive act you took", self.agents_md("coordinator"))

    def test_no_agent_runs_or_is_told_to_use_sonnet_5(self):
        # Owner 2026-11-13: Sonnet 5.5 or higher; a retired version is left out and the guard moves it to the next.
        import re
        old = re.compile(r"claude-sonnet-5(?!-5)\b")
        self.assertEqual(self.team["models"]["specialists"]["primary"], "anthropic/claude-sonnet-5-5")
        self.assertFalse([m for m in self.team["models"]["spawn_allowed"] if old.search(m)])
        for a in self.team["agents"]:
            self.assertIsNone(old.search(self.agents_md(a["id"])), a["id"])
            self.assertIsNone(old.search(json.dumps(a)), a["id"])

    def test_no_contradicting_legacy_rules(self):
        for a in self.team["agents"]:
            md = self.agents_md(a["id"])
            for legacy in ["overrides everything", "TASKS.md", "sessions_send to its group session", "every 10 minutes", "Never use `sessions_spawn`"]:
                self.assertNotIn(legacy, md, (a["id"], legacy))

    def test_identity_matches_config_name(self):
        for a in self.team["agents"]:
            ident = next(c for p, c in self.files.items() if p.endswith(f"/{a['id']}/IDENTITY.md"))
            self.assertIn(f"**Name:** {a['name']}", ident)
            soul = next(c for p, c in self.files.items() if p.endswith(f"/{a['id']}/SOUL.md"))
            self.assertTrue(soul.startswith(f"# {a['name']}"), a["id"])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.team, self.projects = teamlib.load()

    def test_desired_config(self):
        d = teamlib.desired_config(self.team, self.projects, LIVE)
        e = d["agents"]["entries"]
        guard = d["plugins"]["entries"]["token-guard"]["config"]
        self.assertIs(guard["pace"]["enabled"], True)  # the weekly limit lasts the week; off, the team spent it by mid-week (2026-11-21)
        self.assertEqual(guard["pace"]["reservePercent"], 85)  # owner 2026-11-21 (cost): hard stop at 85%
        self.assertEqual(guard["pace"]["fiveHourPercent"], 85)
        self.assertEqual(guard["board"]["maxNoProgress"], 2)
        self.assertIs(guard["board"]["requireDoneWhen"], True)
        self.assertIn("anthropic/claude-opus-5-5", guard["spawn"]["allowedModels"])  # owner: never downgrade to save tokens
        self.assertEqual(e["coordinator"]["model"]["primary"], "anthropic/claude-opus-5-5")  # owner: never downgrade to save tokens
        self.assertEqual(e["coordinator"]["thinkingDefault"], "medium")
        self.assertEqual(e["devops"]["model"]["fallbacks"], [])
        g = d["plugins"]["entries"]["token-guard"]["config"]
        self.assertEqual(g["mirror"]["botTokenFiles"], {"coordinator": "/s/orc", "devops": "/s/dev"})
        self.assertEqual(g["mirror"]["projectTopics"][P("{{private:BRAND_GAME_SLUG}}")], 151)
        self.assertEqual(g["mirror"]["projectTopics"][P("{{private:BRAND_STUDIO_SLUG}}")], 151)
        self.assertEqual(g["jobs"]["idleWakeSession"], "agent:coordinator:explicit:team-ops")  # not the owner's DM session
        self.assertNotIn("openai/gpt-6-astra", g["spawn"]["allowedModels"])
        self.assertEqual(g["spawn"]["codexModel"], "")  # Codex off (owner order), written out explicitly
        for a in e.values():
            self.assertEqual(a["model"]["fallbacks"], [])

    def test_dreaming_off_without_touching_other_memory_settings(self):
        live = dict(LIVE, plugins={"entries": {"memory-core": {"config": {"dreaming": {"enabled": True}, "embeddings": {"x": 1}}}}})
        patch = teamlib.build_patch(live, teamlib.desired_config(self.team, self.projects, live))
        self.assertEqual(patch["plugins"]["entries"]["memory-core"], {"config": {"dreaming": {"enabled": False}}})

    def test_patch_removes_keys_agents_added_and_is_empty_when_equal(self):
        d = teamlib.desired_config(self.team, self.projects, LIVE)
        patch = teamlib.build_patch(LIVE, d)
        tools = patch["agents"]["entries"]["coordinator"]["tools"]
        self.assertNotIn("exec", tools["deny"])  # the stray "exec" deny is gone
        self.assertIn("devtools__*", tools["deny"])
        self.assertEqual(patch["agents"]["entries"]["coordinator"]["model"]["primary"], "anthropic/claude-opus-5-5")
        self.assertEqual(teamlib.build_patch(d, d), {})

    def test_patch_removes_a_setting_dropped_from_the_repo_deep_inside_the_guard_config(self):
        # The host kept spawn.codexModel after the repo dropped it: exactness stopped one level down.
        d = teamlib.desired_config(self.team, self.projects, LIVE)
        live = json.loads(json.dumps(d))
        g = live["plugins"]["entries"]["token-guard"]["config"]
        g["spawn"]["codexModel"] = "openai/gpt-6-astra"
        g["spawn"]["claudeFallbackModel"] = "x/y"
        g["mirror"]["leftover"] = {"a": 1}
        patch = teamlib.build_patch(live, d)
        pg = patch["plugins"]["entries"]["token-guard"]["config"]
        self.assertEqual(pg["spawn"], {"codexModel": "", "claudeFallbackModel": None})
        self.assertEqual(pg["mirror"], {"leftover": None})
        # other plugins' settings stay merged, not exact
        live2 = json.loads(json.dumps(d))
        live2["plugins"]["entries"].setdefault("memory-core", {}).setdefault("config", {})["embeddings"] = {"x": 1}
        self.assertNotIn("memory-core", teamlib.build_patch(live2, d).get("plugins", {}).get("entries", {}))



class BoardTopicsSessionsTest(unittest.TestCase):
    def setUp(self):
        self.team, self.projects = teamlib.load()
        self.files = teamlib.render_files(self.team, self.projects)

    def agents_md(self, agent):
        return next(c for p, c in self.files.items() if p.endswith(f"/{agent}/AGENTS.md"))

    def test_guard_config_has_topics_board_and_projects(self):
        g = teamlib.guard_config(self.team, self.projects, LIVE)
        self.assertEqual(g["ownerId"], P("{{private:OWNER_TELEGRAM_ID}}"))
        self.assertEqual(g["topics"], {"enabled": True, "chatId": P("{{private:TEAM_CHAT_ID}}"), "teamTopic": 160, "keepTopics": [2596]})
        self.assertEqual(g["board"]["agent"], "product")
        self.assertEqual(g["projects"][P("{{private:BRAND_GAME_SLUG}}")]["topic"], 151)
        self.assertIn(P("{{private:BRAND_AI_SLUG}}"), g["projects"]["ai"]["aliases"])  # the owner's correction: {{private:BRAND_AI}} is the ai project
        self.assertNotIn("aliases", g["projects"][P("{{private:BRAND_AI_SLUG}}-hub")])
        self.assertEqual(g["projects"][P("{{private:BRAND_GAME_SLUG}}")]["path"], P("{{private:SERVER_HOME}}/projects/o"))
        self.assertIn(P("{{private:BRAND_STUDIO_SLUG}}"), g["projects"][P("{{private:BRAND_GAME_SLUG}}")]["aliases"])
        self.assertTrue(g["projects"]["ai"]["strict"])
        self.assertEqual(g["mirror"]["accountTokenFiles"], {"orchestrator": "/s/orc", "devops": "/s/dev"})

    def test_guard_budgets_count_tool_calls_and_triage_does_not_rewake_the_same_state(self):
        g = teamlib.guard_config(self.team, self.projects, LIVE)
        self.assertEqual(g["jobs"]["budgetCalls"], 100)
        self.assertEqual(g["orchestratorMaxCallsPerRun"], 60)
        self.assertEqual(g["triage"]["maxWakesPerDay"], 24)
        self.assertEqual(g["triage"]["sameStateRewakeMinutes"], 180)

    def test_up_to_10_items_at_once_in_the_owners_project_order_and_ci_followed_by_the_guard(self):
        # Owner 2026-11-09 ("DO THEM ALL"): one item at a time left the team idle 46% of the last 48 h;
        # 2026-11-10: 1-3 jobs per agent (per-agent limits in test_each_agent_runs_one_to_three_tasks...).
        g = teamlib.guard_config(self.team, self.projects, LIVE)
        self.assertEqual(g["board"]["wip"], 14)
        self.assertEqual(g["board"]["maxOpen"], 20)  # finish before adding
        self.assertEqual(g["board"]["maxTasksPerItem"], 10)  # a whole feature on one item gets split (2026-11-15)
        self.assertEqual(g["board"]["projectOrder"][:5], ["ai", P("{{private:BRAND_AI_SLUG}}-hub"), P("{{private:BRAND_GAME_SLUG}}"), "openclaw-ui", "team"])
        for p in g["board"]["projectOrder"]:
            self.assertIn(p, g["projects"], p)  # every name in the order is a real project
        self.assertTrue(g["jobs"]["ci"]["gitlabTokenFile"].endswith("gitlab-pipeline-watch.token"))
        self.assertFalse(g["jobs"]["ci"]["gitlabTokenFile"].startswith("~"))
        files = teamlib.render_files(self.team, self.projects)
        worker = next(c for p, c in files.items() if p.endswith("/writer/AGENTS.md"))
        self.assertIn("RESULT: waiting", worker)
        self.assertNotIn("oc-wait-pipeline", worker)  # nobody waits for CI in a session any more
        orc = next(c for p, c in files.items() if p.endswith("/coordinator/AGENTS.md"))
        self.assertIn("up to 14 items at once, each agent up to its limit", orc)
        self.assertIn("one item per deliverable that one task can finish", orc)
        self.assertIn("10 tasks per item, then split (enforced)", orc)
        self.assertIn("`User path:` line (the guard checks)", orc)

    def test_agents_reach_the_owners_logins_through_the_laptop_browser_profile(self):
        # Owner 2026-11-09: give the team the laptop browser. The node browser needs operator.admin (no
        # browser-only grant exists), so the agents attach to the owner's Team Chrome through a tunnel.
        desired = teamlib.desired_config(self.team, self.projects, LIVE, mirror=False)
        self.assertEqual(desired["browser"]["profiles"]["laptop"], {"cdpUrl": "http://127.0.0.1:19222", "attachOnly": True})
        self.assertNotIn("executablePath", desired["browser"])  # the server's own browser settings stay untouched
        files = teamlib.render_files(self.team, self.projects)
        for a in self.team["agents"]:
            md = next(c for p, c in files.items() if p.endswith(f"/{a['id']}/AGENTS.md"))
            self.assertIn('profile="laptop"', md, a["id"])
            # 2026-11-09: QA twice reported the laptop "offline" after a timeout while it was up (a 0.25 s
            # link to a busy host); the default 20 s snapshot timeout is too short for it.
            self.assertIn("`timeoutMs: 60000`", md, a["id"])
            self.assertIn("a timeout means slow, not offline", md, a["id"])

    def test_growth_and_social_act_on_their_own_with_a_shell_and_the_laptop_browser(self):
        # Owner 2026-11-09: "ALLOW GROWTH AND SOCIAL SHELL TO USE MY LAPTOP BROWSER AND DO WHATEVER THEY WANTED".
        e = teamlib.desired_config(self.team, self.projects, LIVE, mirror=False)["agents"]["entries"]
        files = teamlib.render_files(self.team, self.projects)
        for a in ("growth", "social"):
            deny = e[a]["tools"].get("deny", [])
            for keep in ("exec", "process", "browser"):
                self.assertNotIn(keep, deny, a)
            self.assertIn("gateway", deny, a)  # no agent changes the OpenClaw config
            md = next(c for p, c in files.items() if p.endswith(f"/{a}/AGENTS.md"))
            self.assertIn("act, do not ask", md, a)
            self.assertNotIn("without the owner's OK", md, a)
        self.assertIn("exec", e["strategy"]["tools"]["deny"])  # not part of the order

    def test_browser_agents_get_a_bigger_call_budget_per_task(self):
        # 2026-11-09: social's task "5 new posts" hit the 100-call budget after one live post (86 of its 102
        # calls were laptop-browser steps), so browser agents get their own budget; the rest keep the default.
        jobs = teamlib.guard_config(self.team, self.projects, LIVE, mirror=False)["jobs"]
        self.assertEqual(jobs["budgetCalls"], 100)
        for a in ("growth", "social"):
            self.assertGreaterEqual(jobs["budgetCallsByAgent"][a], 300, a)
        self.assertEqual(set(jobs["budgetCallsByAgent"]), {"growth", "social"})
        e = teamlib.desired_config(self.team, self.projects, LIVE, mirror=False)["agents"]["entries"]
        self.assertNotIn("task_budget_calls", e["growth"])  # a guard setting, not an OpenClaw one

    def test_each_agent_runs_one_to_three_tasks_at_once_within_the_team_total(self):
        # Owner 2026-11-10: "Make some agents between 1-3 jobs per agent (depends on the agent role and tasks!)".
        g = teamlib.guard_config(self.team, self.projects, LIVE, mirror=False)
        caps = g["board"]["maxTasksByAgent"]
        workers = {a["id"] for a in self.team["agents"] if a["id"] != g["orchestratorAgent"]}
        self.assertEqual(set(caps), workers)
        self.assertTrue(all(1 <= n <= 3 for n in caps.values()), caps)
        self.assertEqual((caps["growth"], caps["reviewer"]), (3, 1))
        total = int(self.team["limits"]["max_concurrent_tasks"])
        # Items in hand may exceed tasks at once: items waiting in Review on CI run no task (2026-11-16: 6 of 10 slots).
        self.assertGreaterEqual(g["board"]["wip"], total)
        sub = teamlib.desired_config(self.team, self.projects, LIVE, mirror=False)["agents"]["defaults"]["subagents"]
        self.assertEqual((sub["maxConcurrent"], sub["maxChildrenPerAgent"]), (total, total))  # OpenClaw lets the guard decide
        e = teamlib.desired_config(self.team, self.projects, LIVE, mirror=False)["agents"]["entries"]
        self.assertNotIn("max_tasks", e["growth"])  # a guard setting, not an OpenClaw one

    def test_exec_commands_stay_in_the_foreground_for_two_minutes(self):
        # OpenClaw's default is 10 s, then the model polls (one full-context call per poll); its maximum is 120 s.
        self.assertEqual(int(self.team["limits"]["exec_background_ms"]), 120000)
        live = {"tools": {"exec": {"host": "auto", "mode": "full"}, "web": {"x": 1}}}
        t = teamlib.desired_config(self.team, self.projects, live, mirror=False)["tools"]
        self.assertEqual(t["exec"], {"host": "auto", "mode": "full", "backgroundMs": 120000})
        self.assertEqual(t["web"], {"x": 1})

    def test_the_remote_browser_timeouts_are_raised_in_openclaw_and_checked(self):
        # 2026-11-11: OpenClaw hard-codes 1.5 s / 3 s for a remote browser; the laptop Team Chrome answers in 0.6-3 s.
        self.assertGreaterEqual(int(self.team["limits"]["remote_browser_timeout_ms"]), 10000)
        with tempfile.TemporaryDirectory() as d:
            dist = Path(d) / "dist"
            (dist / "sub").mkdir(parents=True)
            src = ("import x from './a.mjs';\nconst DEFAULT_BROWSER_REMOTE_CDP_TIMEOUT_MS = 1500;\n"
                   "const DEFAULT_BROWSER_REMOTE_CDP_HANDSHAKE_TIMEOUT_MS = 3e3;\nconst OTHER_TIMEOUT_MS = 1500;\n")
            cfg = dist / "config-AbC12.mjs"
            cfg.write_text(src)
            (dist / "sub" / "other.mjs").write_text("const OTHER = 1500;\n")
            ok, msg = teamlib.browser_timeouts_check(dist, 15000)
            self.assertFalse(ok)
            self.assertIn("reset", msg)
            changed, files = teamlib.patch_browser_timeouts(dist, 15000, Path(d) / "backup")
            self.assertEqual(changed, [str(cfg)])
            text = cfg.read_text()
            self.assertIn("const DEFAULT_BROWSER_REMOTE_CDP_TIMEOUT_MS = 15000;", text)
            self.assertIn("const DEFAULT_BROWSER_REMOTE_CDP_HANDSHAKE_TIMEOUT_MS = 15000;", text)
            self.assertIn("const OTHER_TIMEOUT_MS = 1500;", text)  # nothing else touched
            self.assertEqual((Path(d) / "backup" / cfg.name).read_text(), src)  # the original is kept, outside OpenClaw's folder
            self.assertEqual(teamlib.patch_browser_timeouts(dist, 15000)[0], [])  # idempotent
            self.assertTrue(teamlib.browser_timeouts_check(dist, 15000)[0])
            cfg.write_text("const SOMETHING_ELSE = 1;\n")  # an update moved them
            ok, msg = teamlib.browser_timeouts_check(dist, 15000)
            self.assertFalse(ok)
            self.assertIn("not found", msg)

    def test_the_model_runtime_build_timeout_is_raised_in_openclaw_and_checked(self):
        # 2026-11-16: the orchestrator's model setup timed out at OpenClaw's fixed 120 s three starts in a row and stayed locked.
        self.assertGreaterEqual(int(self.team["limits"]["model_runtime_build_timeout_ms"]), 300000)
        with tempfile.TemporaryDirectory() as d:
            dist = Path(d) / "dist"
            dist.mkdir()
            src = "const DEFAULT_MODEL_RUNTIME_BUILD_TIMEOUT_MS = 12e4;\nlet t = DEFAULT_MODEL_RUNTIME_BUILD_TIMEOUT_MS;\nconst OTHER_TIMEOUT_MS = 12e4;\n"
            f = dist / "prepared-model-runtime-Xy1.mjs"
            f.write_text(src)
            self.assertFalse(teamlib.model_build_timeout_check(dist, 600000)[0])
            changed, files = teamlib.patch_model_build_timeout(dist, 600000, Path(d) / "backup")
            self.assertEqual(changed, [str(f)])
            text = f.read_text()
            self.assertIn("const DEFAULT_MODEL_RUNTIME_BUILD_TIMEOUT_MS = 600000;\n", text)
            self.assertIn("const OTHER_TIMEOUT_MS = 12e4;", text)  # nothing else touched
            self.assertEqual((Path(d) / "backup" / f.name).read_text(), src)
            self.assertEqual(teamlib.patch_model_build_timeout(dist, 600000)[0], [])  # idempotent
            self.assertTrue(teamlib.model_build_timeout_check(dist, 600000)[0])
            f.write_text("const DEFAULT_MODEL_RUNTIME_BUILD_TIMEOUT_MS = 6e5; // hand patch with a note\n")
            self.assertFalse(teamlib.model_build_timeout_check(dist, 600000)[0])  # normalised by apply
            self.assertEqual(len(teamlib.patch_model_build_timeout(dist, 600000)[0]), 1)
            f.write_text("const SOMETHING_ELSE = 1;\n")
            ok, msg = teamlib.model_build_timeout_check(dist, 600000)
            self.assertFalse(ok)
            self.assertIn("not found", msg)

    def test_the_tool_server_schemas_are_deferred_in_openclaw_and_checked(self):
        # 2026-11-16: alwaysLoad: true put every tool schema into every call (~51k tokens before a task started).
        self.assertTrue(self.team["limits"]["mcp_defer_tools"])
        with tempfile.TemporaryDirectory() as d:
            dist = Path(d) / "dist"
            dist.mkdir()
            src = ("function createMcpServerConfig(port, headers) {\n\treturn { mcpServers: { openclaw: {\n\t\ttype: \"http\",\n"
                   "\t\turl: `http://127.0.0.1:${port}/mcp`,\n\t\talwaysLoad: true,\n\t\theaders\n\t} } };\n}\nconst other = { alwaysLoad: true };\n")
            f = dist / "mcp-http.loopback-runtime-Ab1.mjs"
            f.write_text(src)
            (dist / "startup-state-readiness-X.mjs").write_text("url: `http://127.0.0.1:${address.port}${route}`,\n\talwaysLoad: true\n")
            self.assertFalse(teamlib.mcp_defer_check(dist)[0])
            changed, files = teamlib.patch_mcp_defer(dist, True, Path(d) / "backup")
            self.assertEqual(changed, [str(f)])
            text = f.read_text()
            self.assertIn("url: `http://127.0.0.1:${port}/mcp`,\n\t\talwaysLoad: false,", text)
            self.assertIn("const other = { alwaysLoad: true };", text)  # nothing else touched
            self.assertIn("alwaysLoad: true", (dist / "startup-state-readiness-X.mjs").read_text())  # other files untouched
            self.assertEqual((Path(d) / "backup" / f.name).read_text(), src)
            self.assertEqual(teamlib.patch_mcp_defer(dist, True)[0], [])  # idempotent
            self.assertTrue(teamlib.mcp_defer_check(dist)[0])
            f.write_text("nothing here\n")
            self.assertIn("not found", teamlib.mcp_defer_check(dist)[1])

    def test_every_model_the_team_runs_has_a_context_budget(self):
        # 2026-11-14: Sonnet 5.5 had no entry and ran with a 1,000,000-token window; long tasks never compacted.
        d = teamlib.desired_config(self.team, self.projects, LIVE)
        budgets = {m["id"]: m["contextTokens"] for m in d["models"]["providers"]["anthropic"]["models"]}
        for spec in (self.team["models"]["orchestrator"], self.team["models"]["specialists"]):
            mid = spec["primary"].split("/", 1)[1]
            self.assertIn(mid, budgets)
            self.assertLessEqual(budgets[mid], 200000)
        self.assertLessEqual(budgets["claude-sonnet-5-5"], 100000)
        self.assertLessEqual(budgets["claude-opus-5-5"], 90000)  # the orchestrator's conversation never resets (2026-11-17)

    def test_telegram_gets_results_only(self):
        # 2026-11-14: three posts per task (dispatch, "On it", result), ~860 a day; the owner saw no deliveries in them.
        m = teamlib.desired_config(self.team, self.projects, LIVE)["plugins"]["entries"]["token-guard"]["config"]["mirror"]
        self.assertIs(m["resultsOnly"], True)
        self.assertIs(m["acks"], False)

    def test_the_page_attach_timeout_is_raised_in_openclaw_and_checked(self):
        # 2026-11-14: snapshot/click/navigate attach for 5/7/9 s whatever the agent asks; the laptop browser needs 6.1-9.8 s.
        self.assertGreaterEqual(int(self.team["limits"]["remote_browser_attach_ms"]), 20000)
        with tempfile.TemporaryDirectory() as d:
            dist = Path(d) / "dist"
            dist.mkdir()
            cfg = dist / "config-AbC12.mjs"
            cfg.write_text("const DEFAULT_BROWSER_REMOTE_CDP_TIMEOUT_MS = 15000;\nconst DEFAULT_BROWSER_REMOTE_CDP_HANDSHAKE_TIMEOUT_MS = 15000;\n")
            src = ("\t\tfor (let attempt = 0; attempt < 3; attempt += 1) {\n"
                   "\t\t\ttry {\n\t\t\t\tconst timeout = 5e3 + attempt * 2e3;\n\t\t\t\tlet endpointDiscoveryError;\n")
            nav = dist / "pw-tools-core.interactions.navigation-C_EWwW-j.mjs"
            nav.write_text(src)
            self.assertTrue(teamlib.browser_timeouts_check(dist, 15000)[0])  # attach not asked for: not checked
            ok, msg = teamlib.browser_timeouts_check(dist, 15000, 30000)
            self.assertFalse(ok)
            self.assertIn(nav.name, msg)
            changed, _ = teamlib.patch_browser_timeouts(dist, 15000, Path(d) / "backup", 30000)
            self.assertEqual(changed, [str(nav)])  # the constants were right already
            self.assertIn("const timeout = 30000 + attempt * 15000;", nav.read_text())
            self.assertEqual(nav.read_text(), src.replace("5e3 + attempt * 2e3", "30000 + attempt * 15000"))
            self.assertEqual((Path(d) / "backup" / nav.name).read_text(), src)
            self.assertEqual(teamlib.patch_browser_timeouts(dist, 15000, None, 30000)[0], [])  # idempotent
            ok, msg = teamlib.browser_timeouts_check(dist, 15000, 30000)
            self.assertTrue(ok, msg)
            self.assertIn("attach 30000 ms", msg)
            nav.write_text("const somethingElse = 1;\n")  # an update moved it
            ok, msg = teamlib.browser_timeouts_check(dist, 15000, 30000)
            self.assertFalse(ok)
            self.assertIn("browser attach timeout: not found", msg)

    def test_the_codex_plugin_is_off(self):
        # Owner: Codex off. Its plugin loaded at every start with supervision on, and codex processes ran.
        d = teamlib.desired_config(self.team, self.projects, LIVE, mirror=False)
        self.assertIs(d["plugins"]["entries"]["codex"]["enabled"], False)
        self.assertTrue(all(t in self.team["tool_trim"]["nobody"] for t in ("codex_threads", "codex_session_send")))

    def test_owner_pre_approval_and_user_path_proof_reach_every_agent(self):
        # Owner 2026-11-09: "ALL AGENTS 100% FREE"; 70 of 77 blocked items waited on an owner OK, and the
        # {{private:BRAND_IR_TIGHT}} bot was approved on webhook checks while it had run 0 jobs.
        files = teamlib.render_files(self.team, self.projects)
        for a in self.team["agents"]:
            md = next(c for p, c in files.items() if p.endswith(f"/{a['id']}/AGENTS.md"))
            self.assertIn("Pre-approved by the owner (2026-11-09", md, a["id"])
            self.assertIn("never park an item on an owner OK", md, a["id"])
            self.assertIn("User path: <action> → <result, id>", md, a["id"])
            self.assertNotIn("Needs the owner's OK first", md, a["id"])

    def test_owner_orders_of_2026_11_05_are_in_every_agent_and_the_guard(self):
        files = teamlib.render_files(self.team, self.projects)
        agents = {p.split("/")[-2]: c for p, c in files.items() if p.endswith("/AGENTS.md") and "/workspace/" in p}
        for a in self.team["agents"]:
            md = agents[a["id"]]
            # 9eac545e: never claim what you did not check, near the top of every agent's own file
            self.assertLess(md.find("Never claim what you did not check yourself"), 1500, a["id"])
            self.assertGreaterEqual(md.find("Never claim what you did not check yourself"), 0, a["id"])
        worker = agents["writer"]
        self.assertIn("Before any push, run every step of the project's pipeline locally", worker)  # 9a46aaf7
        self.assertIn("Local checks:", worker)
        self.assertIn("wake_orchestrator", worker)  # 93375200
        g = teamlib.guard_config(self.team, self.projects, LIVE)
        self.assertIn("Model:", g["requireBriefFields"])  # d1789262
        self.assertIn("Effort:", g["requireBriefFields"])
        self.assertIn("`Model:` · `Effort:`", agents["coordinator"])

    def test_project_name_contract(self):
        g = teamlib.guard_config(self.team, self.projects, LIVE)
        names = set(g["projects"])
        aliases = {a.lower(): n for n, p in g["projects"].items() for a in p.get("aliases", [])}
        self.assertEqual(len(aliases), sum(len(p.get("aliases", [])) for p in g["projects"].values()), "an alias names two projects")
        self.assertFalse(names & set(aliases), "an alias is also a project name")
        self.assertEqual(aliases[P("{{private:BRAND_AI_SLUG}}.com")], "ai")
        self.assertEqual(aliases["team-wide"], "team")
        files = teamlib.render_files(self.team, self.projects)
        for p, c in files.items():
            if p.endswith("/AGENTS.md") and "/workspace/" in p:
                self.assertIn("**Project names:**", c, p)

    def test_every_guard_setting_is_allowed_by_the_plugin_schema(self):
        # OpenClaw refuses a plugin config with a key its schema does not list (a deploy was once refused for this).
        schema_file = ROOT.parent / "openclaw-token-guard" / "plugin" / "openclaw.plugin.json"
        if not schema_file.exists():
            self.skipTest("openclaw-token-guard is not checked out next to this repo")
        props = json.loads(schema_file.read_text())["configSchema"]["properties"]
        g = teamlib.guard_config(self.team, self.projects, LIVE)
        self.assertEqual([k for k in g if k not in props], [])

    def test_rules_describe_topics_board_and_one_task_per_session(self):
        files = teamlib.render_files(self.team, self.projects)
        md = next(c for p, c in files.items() if p.endswith("/devops/AGENTS.md"))
        for needle in ["**Topics:**", "**The board:**", "**One task, one session:**", "The board moves by itself"]:
            self.assertIn(needle, md)
        pm = next(c for p, c in files.items() if p.endswith("/product/AGENTS.md"))
        self.assertIn("You own the board's accuracy", pm)
        orc = next(c for p, c in files.items() if p.endswith("/coordinator/AGENTS.md"))
        self.assertIn("approve it with `task_inbox` action `approve`", orc)

    def test_gc_keeps_running_and_recent_sessions(self):
        now = 10 * 86400000
        rows = [
            {"key": "agent:qa:subagent:1", "status": "done", "updatedAt": now - 40 * 60000},
            {"key": "agent:qa:subagent:2", "status": "done", "updatedAt": now - 10 * 60000},
            {"key": "agent:qa:subagent:3", "status": "running", "updatedAt": now - 90 * 60000},
            {"key": "agent:devops:telegram:group:-1:topic:151", "status": "done", "ageMs": 30 * 3600000},
            {"key": "agent:coordinator:main", "status": "done", "updatedAt": now - 3600000},
        ]
        self.assertEqual([r["key"] for r in teamlib.gc_candidates(rows, now)], ["agent:qa:subagent:1", "agent:devops:telegram:group:-1:topic:151"])
        self.assertEqual(len(teamlib.gc_candidates(rows, now, everything=True)), 4)  # all but the running one

    def test_product_can_use_the_board_tool(self):
        d = teamlib.desired_config(self.team, self.projects, LIVE)
        self.assertIn("task_inbox", d["agents"]["entries"]["product"]["tools"]["alsoAllow"])

    def test_everyone_knows_who_is_who_and_how_to_talk(self):
        handles = {a["id"]: a["handle"] for a in self.team["agents"]}
        self.assertEqual(len(handles), 11)
        self.assertEqual(len(set(handles.values())), 11)
        for a in self.team["agents"]:
            md = self.agents_md(a["id"])
            for h in handles.values():
                self.assertIn(f"@{h}", md, (a["id"], h))
            self.assertIn(P("@{{private:OWNER_HANDLE}}"), md)
            for needle in ["### 6. Talking in the team", "**A mention is not a handoff:**", "**Reply and mention:**"]:
                self.assertIn(needle, md, (a["id"], needle))
        worker = self.agents_md("qa")
        self.assertIn("Start with one or two plain sentences for the team", worker)
        self.assertIn("<one or two plain sentences>\n\nRESULT: done", worker)
        self.assertIn("It is threaded under the worker's result automatically", self.agents_md("coordinator"))

    def test_the_owner_is_never_given_a_pronoun(self):
        import re
        for p, c in self.files.items():
            if p.endswith(".md"):
                self.assertIsNone(re.search(r"\b(he|him|his|she|her)\b", c, re.I), p)

    def test_guard_gets_the_handles_for_mentions(self):
        g = teamlib.guard_config(self.team, self.projects, LIVE)
        self.assertEqual(g["mirror"]["agentHandles"]["coordinator"], P("{{private:BOT_COORDINATOR}}"))
        self.assertEqual(g["mirror"]["agentHandles"]["designer"], P("{{private:BOT_DESIGN}}"))
        self.assertEqual(g["mirror"]["ownerHandle"], P("{{private:OWNER_HANDLE}}"))
        self.assertEqual(g["mirror"]["pmAgent"], "product")


class DriftTest(unittest.TestCase):
    def test_drift_restores_only_what_was_applied(self):
        files = {"/w/a": "same", "/w/b": "new version in the repo", "/w/c": "added in the repo"}
        applied = {"files": {"/w/a": teamlib.sha("same"), "/w/b": teamlib.sha("old applied version")}}
        restore, held = teamlib.drift_restorable(["/w/a", "/w/b", "/w/c"], files, applied)
        self.assertEqual(restore, ["/w/a"])
        self.assertEqual(held, ["/w/b", "/w/c"])
        self.assertEqual(teamlib.drift_restorable(["/w/a"], files, {}), (["/w/a"], []))  # no manifest yet: restore all


class SkillsTest(unittest.TestCase):
    def test_each_agent_sees_only_its_skills_and_every_workspace_skill_ships(self):
        team, projects = teamlib.load()
        d = teamlib.desired_config(team, projects, LIVE)
        files = teamlib.render_files(team, projects)
        bundled = {"healthcheck", "browser-automation", "node-inspect-debugger", "python-debugpy"}
        import re
        vendor_names = set()
        for skill_md in (teamlib.REPO / "vendor-skills").glob("*/SKILL.md"):
            m = re.search(r"^name:\s*([\w-]+)", skill_md.read_text(), re.M)
            vendor_names.add(m.group(1) if m else skill_md.parent.name)
        self.assertTrue({"frontend-design", "ui-ux-pro-max", "design-taste-frontend", "verification-before-completion"} <= vendor_names, vendor_names)
        self.assertTrue(teamlib.vendor_files(team), "vendor skills are installed by apply")
        self.assertIn(str(teamlib.vendor_root(team)), d["skills"]["load"]["extraDirs"])
        for a in team["agents"]:
            allowed = d["agents"]["entries"][a["id"]]["skills"]
            self.assertNotIn("meme-maker", allowed)
            for name in allowed:
                if name in bundled or name in vendor_names:
                    continue
                shipped = any(p.endswith(f"/{a['id']}/skills/{name}/SKILL.md") for p in files)
                self.assertTrue(shipped, f"{a['id']} lists skill {name} but neither its workspace nor vendor-skills has it")
        self.assertIn("frontend-design", d["agents"]["entries"]["designer"]["skills"])
        cat = teamlib.skill_catalog(team)
        des = {x["name"]: x for x in cat["designer"]}
        self.assertTrue(des["design-taste-frontend"]["about"].startswith("tasteskill"), des["design-taste-frontend"]["about"][:80])
        self.assertTrue(des["ui-ux-pro-max"]["about"])
        self.assertTrue(des["product-design-playbook"]["always"] and des["verification-before-completion"]["always"])
        missing = [(a, x["name"]) for a, xs in cat.items() for x in xs if not x["about"] and x["name"] not in bundled]
        self.assertEqual(missing, [], "every repo skill has a description the guard can match")
        self.assertEqual(d["skills"]["workshop"]["autonomous"]["mode"], "off")  # agents never write their own skills
        self.assertIn("reconcile-ci-handoffs", d["agents"]["entries"]["coordinator"]["skills"])
        tg = d["plugins"]["entries"]["token-guard"]
        self.assertEqual(tg["subagent"], {"allowModelOverride": True, "allowedModels": ["anthropic/claude-haiku-5-5"]})
        self.assertIn("webapp-testing", d["agents"]["entries"]["qa"]["skills"])


class McpTest(unittest.TestCase):
    def test_each_agent_sees_only_its_mcp_servers(self):
        team, projects = teamlib.load()
        d = teamlib.desired_config(team, projects, LIVE)
        servers = d["mcp"]["servers"]
        self.assertEqual(set(servers), {"devtools", "context7"})
        self.assertIn("chrome-devtools-mcp@1.12.0", servers["devtools"]["args"])  # pinned, never @latest
        self.assertFalse(any("~" in a for a in servers["devtools"]["args"] + [servers["devtools"]["command"]]))
        self.assertEqual(servers["context7"]["transport"], "streamable-http")
        e = d["agents"]["entries"]
        self.assertNotIn("devtools__*", e["qa"]["tools"].get("deny", []))
        self.assertIn("devtools__*", e["designer"]["tools"]["deny"])  # one Chrome per session: QA only
        self.assertIn("context7__*", e["qa"]["tools"]["deny"])
        for s in servers:
            self.assertIn(f"{s}__*", e["coordinator"]["tools"]["deny"])  # the orchestrator dispatches, it does not drive browsers
        self.assertEqual(e["coordinator"]["tools"]["alsoAllow"], ["task_inbox"])
        # tool trim: nobody gets desktop/UI tools; workers lose what they can never use; the orchestrator keeps dispatch
        for a in ("writer", "qa", "coordinator"):
            self.assertIn("computer", e[a]["tools"]["deny"])
        for t in ("sessions_spawn", "message", "automations", "ask_user"):
            self.assertIn(t, e["writer"]["tools"]["deny"])
            self.assertNotIn(t, e["coordinator"]["tools"]["deny"])
        for keep in ("exec", "browser", "nodes", "web_fetch", "secrets", "pdf", "view_image", "image_generate"):
            self.assertNotIn(keep, e["writer"]["tools"]["deny"])


class MergePatchTest(unittest.TestCase):
    def test_rfc7386(self):
        self.assertEqual(teamlib.merge_patch({"a": 1, "b": {"c": 2, "d": 3}}, {"b": {"c": None, "e": 4}, "f": [1]}), {"a": 1, "b": {"d": 3, "e": 4}, "f": [1]})



class PluginFilesTest(unittest.TestCase):
    def test_every_plugin_module_is_installed(self):
        team, _ = teamlib.load()
        repo = teamlib.expand(team["team"]["token_guard_repo"])
        if not (repo / "plugin" / "index.js").exists():
            team["team"]["token_guard_repo"] = str(Path.home() / "Desktop" / "openclaw-token-guard")
            repo = teamlib.expand(team["team"]["token_guard_repo"])
        if not (repo / "plugin" / "index.js").exists():
            self.skipTest("token-guard checkout not found")
        installed = {Path(p).name for p in teamlib.plugin_files(team) if "/token-guard/plugin/" in p}
        imported = set()
        import re
        for js in (repo / "plugin").glob("*.js"):
            imported |= set(re.findall(r'from "\./([\w.-]+\.js)"', js.read_text()))
        self.assertTrue(imported, "no local imports found")
        self.assertLessEqual(imported, installed, f"modules imported but not installed: {imported - installed}")

    def test_agents_can_store_a_credential(self):
        # 2026-11-08: the owner's bot token could not be kept anywhere: the secrets folder was blocked
        # for every agent and nothing told them the sanctioned way in.
        team, projects = teamlib.load()
        repo = teamlib.expand(team["team"]["token_guard_repo"])
        if not (repo / "bin" / "oc-secret-save").exists():
            team["team"]["token_guard_repo"] = str(Path.home() / "Desktop" / "openclaw-token-guard")
            repo = teamlib.expand(team["team"]["token_guard_repo"])
        if not (repo / "bin" / "oc-secret-save").exists():
            self.skipTest("token-guard checkout not found")
        self.assertIn("oc-secret-save", teamlib.HELPERS)
        self.assertTrue(any(p.endswith("/bin/oc-secret-save") for p in teamlib.plugin_files(team)))
        files = teamlib.render_files(team, projects)
        for a in team["agents"]:
            md = next(c for p, c in files.items() if p.endswith(f"/{a['id']}/AGENTS.md"))
            self.assertIn("~/.openclaw/bin/oc-secret-save <name> <<'EOF'", md, a["id"])
            self.assertIn("transcripts show it masked", md, a["id"])


if __name__ == "__main__":
    unittest.main()


class VoiceNotesTest(unittest.TestCase):
    # 2026-11-14: the owner's voice notes failed (the server CPU took 88 s for a 9 s note against a 90 s cap).
    def setUp(self):
        self.team, self.projects = teamlib.load()

    def test_voice_notes_use_the_repo_wrapper_and_leave_the_other_tool_settings_alone(self):
        live = dict(LIVE, tools={"profile": "full", "exec": {"host": "gateway"}, "media": {"models": [{"command": "/old"}]}})
        tools = teamlib.desired_config(self.team, self.projects, live)["tools"]
        self.assertEqual(tools["profile"], "full")
        self.assertEqual(tools["exec"], {"host": "gateway", "backgroundMs": 120000})  # the rest of exec stays as it is
        model = tools["media"]["models"][0]
        self.assertEqual(model["command"], str(teamlib.REPO / "bin" / "stt-transcribe"))
        self.assertEqual(model["args"], ["{{AttachmentPath}}"])
        self.assertGreaterEqual(model["timeoutSeconds"], 150)
        patch = teamlib.build_patch(live, teamlib.desired_config(self.team, self.projects, live))
        self.assertEqual(set(patch.get("tools", {})), {"media", "exec"})  # nothing else under tools changes

    def test_the_wrapper_takes_the_laptop_answer_and_falls_back_to_the_cpu(self):
        import http.server
        import os
        import subprocess
        import threading

        class Node(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                out = ("heard %d bytes" % len(body)).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Node)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as d:
            audio = Path(d) / "note.ogg"
            audio.write_bytes(b"x" * 1234)
            cpu = Path(d) / "cpu-python"
            cpu.write_text("#!/bin/sh\necho \"cpu transcript of $2\"\n")
            cpu.chmod(0o755)
            env = dict(os.environ, STT_NODE_PORT=str(srv.server_address[1]), STT_CPU_PYTHON=str(cpu))
            wrapper = str(teamlib.REPO / "bin" / "stt-transcribe")
            r = subprocess.run([wrapper, str(audio)], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(r.stdout.strip(), "heard 1234 bytes")
            srv.shutdown()
            srv.server_close()
            r = subprocess.run([wrapper, str(audio)], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(r.stdout.strip(), f"cpu transcript of {audio}")
            self.assertIn("CPU", r.stderr)


class McpTimeoutTest(unittest.TestCase):
    def test_agent_sessions_get_two_minutes_to_reach_the_openclaw_tools(self):
        # 2026-11-13/14: five tasks started without exec or the browser (the CLI's 30 s MCP connect timeout).
        conf = teamlib.UNITS["openclaw-gateway.service.d/team-mcp-timeout.conf"]
        self.assertIn("[Service]", conf)
        ms = int(conf.split("MCP_TIMEOUT=")[1].split()[0])
        self.assertGreaterEqual(ms, 90000)


class InstallUnitsTest(unittest.TestCase):
    def test_apply_does_not_start_timers_a_deploy_has_paused(self):
        # 2026-11-14: apply ran "enable --now" on every timer, restarting the heal check 6 s before the gateway start.
        import subprocess as sp
        calls = []

        class R:
            def __init__(self, out=""):
                self.stdout, self.returncode = out, 0

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:3] == ["systemctl", "--user", "is-enabled"]:
                return R("enabled\n" if cmd[3] != "openclaw-team-heal.timer" else "disabled\n")
            return R()
        real_run, real_write = teamlib.subprocess.run, teamlib.write_files
        teamlib.subprocess.run, teamlib.write_files = fake_run, (lambda files, lock: [])
        try:
            teamlib.install_units(False)
        finally:
            teamlib.subprocess.run, teamlib.write_files = real_run, real_write
        started = [c[-1] for c in calls if "--now" in c]
        self.assertEqual(started, ["openclaw-team-heal.timer"])  # only the one not yet enabled


class StartupFailuresTest(unittest.TestCase):
    def test_verify_fails_after_a_failed_gateway_start(self):
        import os
        import time
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(teamlib.startup_failures_check(d)[0])
            old = Path(d) / "openclaw-stability-a-gateway.startup_failed.json"
            old.write_text("{}")
            os.utime(old, (time.time() - 3600, time.time() - 3600))
            self.assertTrue(teamlib.startup_failures_check(d)[0], "an old failure is history")
            (Path(d) / "openclaw-stability-b-gateway.stop_shutdown_timeout.json").write_text("{}")
            self.assertTrue(teamlib.startup_failures_check(d)[0], "a slow stop is not a failed start")
            (Path(d) / "openclaw-stability-c-gateway.startup_failed.json").write_text("{}")
            ok, msg = teamlib.startup_failures_check(d)
            self.assertFalse(ok)
            self.assertIn("1 failed gateway start", msg)


class TelegramCacheTest(unittest.TestCase):
    def test_old_retained_telegram_messages_are_trimmed_and_kept_in_the_backup(self):
        import gzip
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "openclaw.sqlite"
            con = sqlite3.connect(db)
            con.execute("create table plugin_state_entries (plugin_id text, namespace text, entry_key text, value_json text, created_at integer, expires_at integer)")
            now = 1794670000000
            day = 86400000
            rows = [("telegram", teamlib.TELEGRAM_RETAINED_NS, "a:1", "{}", now - 20 * day, None),
                    ("telegram", teamlib.TELEGRAM_RETAINED_NS, "a:2", "{}", now - 8 * day, None),
                    ("telegram", teamlib.TELEGRAM_RETAINED_NS, "a:3", "{}", now - 2 * day, None),
                    ("telegram", "telegram.message-cache", "b:1", "{}", now - 20 * day, None),
                    ("memory-core", "short-term-recall", "c:1", "{}", now - 20 * day, None)]
            con.executemany("insert into plugin_state_entries values (?,?,?,?,?,?)", rows)
            con.commit()
            con.close()
            self.assertEqual(teamlib.prune_telegram_cache(db, 7, Path(d) / "bk", now), 2)
            left = [r[0] for r in sqlite3.connect(db).execute("select entry_key from plugin_state_entries order by entry_key")]
            self.assertEqual(left, ["a:3", "b:1", "c:1"])  # only old retained Telegram entries go
            with gzip.open(Path(d) / "bk" / "telegram-retained-cache.jsonl.gz", "rt") as f:
                self.assertEqual(sorted(json.loads(l)["entry_key"] for l in f), ["a:1", "a:2"])
            self.assertEqual(teamlib.prune_telegram_cache(db, 7, Path(d) / "bk2", now), 0)


class PrioTest(unittest.TestCase):
    def test_the_gateway_runs_ahead_of_the_agents_work(self):
        # 2026-11-15: the gateway got a 1-in-36 CPU share; the owner's messages waited 5-12 minutes.
        self.assertIn("openclaw-team-prio.timer", teamlib.TIMERS)
        self.assertIn("OnUnitActiveSec=30s", teamlib.UNITS["openclaw-team-prio.timer"])
        script = (teamlib.REPO / "bin" / "team-prio").read_text()
        self.assertIn("GW_NICE=${TEAM_GW_NICE:--10}", script)
        self.assertIn("claude --strict-mcp-config", script)
        import subprocess
        self.assertEqual(subprocess.run(["bash", "-n", str(teamlib.REPO / "bin" / "team-prio")]).returncode, 0)


class StablePromptTest(unittest.TestCase):
    """2026-11-17: the orchestrator's system prompt changed 70 times in one session (owner messages, guard wakes,
    worker results, the web UI); each change restarted its Claude process and re-cached the whole conversation."""

    ANCHOR_FILE = "\n".join([
        'import { t as resolveConversationCapabilityProfile } from "./conversation-capability-profile-X.mjs";',
        "function resolveCliRuntimeToolPolicy(input) {",
        "}",
        "\tconst baseExtraSystemPromptHash = bindingExtraSystemPromptStatic !== void 0 ? hashCliSessionText(bindingExtraSystemPromptStatic.trim() || void 0) : hashCliSessionText(extraSystemPrompt);",
        "\t\tconst turnRuntimeFacts = params.runtimeFactsInTurn && !skipsTurnPreparation ? splitSystemPromptRelocatableBoundary(transformedSystemPrompt) : void 0;",
        ""])

    def node(self):
        import shutil
        n = shutil.which("node")
        if not n:
            self.skipTest("node is not installed")
        return n

    def run_js(self, body):
        import subprocess
        js = teamlib.STABLE_HELPERS + "\n" + body
        r = subprocess.run([self.node(), "--input-type=module", "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_apply_patches_the_cli_prompt_preparation_once_and_checks_it(self):
        self.assertTrue(self.team_limits()["cli_stable_system_prompt"])
        with tempfile.TemporaryDirectory() as d:
            dist = Path(d) / "dist"
            dist.mkdir()
            f = dist / "prepare.runtime-Ab1.mjs"
            f.write_text(self.ANCHOR_FILE)
            (dist / "prepare.runtime-Cd2.mjs").write_text('import { t } from "./prepare.runtime-Ab1.mjs";\n')
            self.assertFalse(teamlib.stable_prompt_check(dist)[0])
            changed, files, problems = teamlib.patch_stable_prompt(dist, Path(d) / "backup")
            self.assertEqual((changed, files, problems), ([str(f)], [str(f)], []))
            text = f.read_text()
            for old, new in teamlib.STABLE_EDITS:
                self.assertEqual(text.count(new), 1)
            self.assertEqual((Path(d) / "backup" / f.name).read_text(), self.ANCHOR_FILE)
            self.assertTrue(teamlib.stable_prompt_check(dist)[0])
            self.assertEqual(teamlib.patch_stable_prompt(dist)[0], [])  # idempotent
            f.write_text(self.ANCHOR_FILE.replace("void 0;", "undefined;"))  # OpenClaw changed the code
            changed, files, problems = teamlib.patch_stable_prompt(dist)
            self.assertEqual(changed, [])
            self.assertIn("anchors changed", problems[0])
            self.assertFalse(teamlib.stable_prompt_check(dist)[0])

    def team_limits(self):
        return teamlib.load()[0]["limits"]

    def prompt(self, ctx=None, web=False):
        lines = ["You are a personal assistant running inside OpenClaw.", "## Tooling", "- exec: Run shell", "## Messaging",
                 "- Current-session final text normally routes to source."]
        if web:
            lines += ["- Inline buttons OFF for webchat; ask owner for webchat.capabilities.inlineButtons=dm|group|all|allowlist.",
                      "## UI Presentation", "`message`: Tools/skills first.", "## Control UI Embed", "`[embed ...]`: Control UI/webchat only.", "",
                      "## Control UI Side Chat", "- Operator has a read-only Side chat.", "",
                      "## Collapsible Details", "This surface renders `<details>` disclosures.", ""]
        lines += ["## Authorized Senders", "Owner: telegram:1", ""]
        if ctx:
            lines += ["## Conversation Context", "## Message Context", "```json", "{\n  \"chat_type\": \"direct\"\n}", "```", "", ctx, ""]
        lines += ["## Runtime", "Model identity line.", "Reasoning=off.",
                  "<!-- OPENCLAW-RELOCATABLE-BOUNDARY -->",
                  "Runtime: agent=coordinator | channel=" + ("webchat" if web else "telegram") + "\n<!-- /OPENCLAW-RELOCATABLE-BOUNDARY -->",
                  "## OpenClaw tool names in Claude Code", "OpenClaw MCP: `mcp__openclaw__<name>`."]
        return "\n".join(lines)

    def test_every_kind_of_wake_gets_the_same_system_prompt_and_keeps_its_context_in_the_turn(self):
        split = """
function splitRelocatable(text) {
	const opening = "<!-- OPENCLAW-RELOCATABLE-BOUNDARY -->", closing = "<!-- /OPENCLAW-RELOCATABLE-BOUNDARY -->";
	const start = text.indexOf(opening), end = text.indexOf(closing);
	if (start === -1 || end < start) return;
	return { remainingPrompt: [text.slice(0, start).trimEnd(), text.slice(end + closing.length).trimStart()].filter(Boolean).join("\\n"), relocatable: text.slice(start + opening.length, end).trim() };
}
"""
        cases = {"owner": self.prompt(ctx="You are in a chat direct conversation. Your replies are automatically sent to this conversation."),
                 "event": self.prompt(), "web": self.prompt(ctx="You are in a WebChat direct conversation.", web=True)}
        out = self.run_js(split + f"const cases = {json.dumps(cases)};\n"
                          "const out = {};\nfor (const [k, v] of Object.entries(cases)) out[k] = teamStableSystemPrompt(v, void 0, splitRelocatable);\n"
                          "console.log(JSON.stringify(out));")
        prompts = {k: v["remainingPrompt"] for k, v in out.items()}
        self.assertEqual(prompts["owner"], prompts["event"])
        self.assertEqual(prompts["web"], prompts["event"])
        for kept in ("## Tooling", "## Messaging", "## Authorized Senders", "## Runtime", "Reasoning=off.", "## OpenClaw tool names in Claude Code", "OpenClaw MCP:"):
            self.assertIn(kept, prompts["event"])
        self.assertNotIn("Runtime: agent=", prompts["event"])
        owner = out["owner"]["relocatable"]
        self.assertIn("## Conversation Context", owner)
        self.assertIn("\"chat_type\": \"direct\"", owner)
        self.assertIn("Your replies are automatically sent", owner)
        self.assertIn("channel=telegram", owner)
        web = out["web"]["relocatable"]
        for moved in ("## UI Presentation", "## Control UI Embed", "## Control UI Side Chat", "## Collapsible Details",
                      "Inline buttons OFF for webchat", "WebChat direct conversation", "channel=webchat"):
            self.assertIn(moved, web)
        self.assertEqual(out["event"]["relocatable"], "Runtime: agent=coordinator | channel=telegram")
        # every line of every prompt survives: either in the system prompt or in the turn
        for k, v in cases.items():
            kept = set(out[k]["remainingPrompt"].splitlines()) | set(out[k]["relocatable"].splitlines())
            missing = [l for l in v.splitlines() if l.strip() and "OPENCLAW-RELOCATABLE" not in l and l not in kept]
            self.assertEqual(missing, [], k)


class PrivateValuesTest(unittest.TestCase):
    """The repo is public-safe: private values live only in private.toml (gitignored), files hold placeholders."""

    def used_keys(self):
        texts = teamlib.tracked_texts()
        team_files = ("team.toml", "projects.toml", "facts.md", "owner.md", "rules/", "roles/", "personas/", "skills/", "(file names)")
        return {k for n, t in texts.items() if n.startswith(team_files) for k in teamlib.PRIVATE_RE.findall(t)}

    def test_a_missing_key_is_refused_by_name(self):
        with self.assertRaises(SystemExit) as e:
            teamlib.fill("owner {{private:NO_SUCH_KEY_X}} here", "rules/x.md")
        self.assertIn("NO_SUCH_KEY_X", str(e.exception))
        self.assertIn("rules/x.md", str(e.exception))

    def test_fill_replaces_and_leaves_plain_text_alone(self):
        self.assertEqual(P("@{{private:OWNER_HANDLE}}"), "@example_owner")
        self.assertEqual(teamlib.fill("no placeholders"), "no placeholders")

    def test_rendered_files_hold_no_placeholder(self):
        team, projects = teamlib.load()
        for path, text in teamlib.render_files(team, projects).items():
            self.assertNotIn("{{private:", path)
            self.assertNotIn("{{private:", text, path)

    def test_the_example_has_every_key_the_repo_uses(self):
        example = set(teamlib.private_values())
        self.assertEqual(self.used_keys() - example, set())

    def real_private(self):
        return [p for p in (ROOT / "private.toml", Path.home() / ".config" / "openclaw-team" / "private.toml") if p.exists()]

    def test_the_real_values_render_under_the_cap(self):
        # The example values are shorter than the real ones: AGENTS.md can fit the cap with them and overflow with
        # the real ones (2026-11-18: the orchestrator's was 29332 > 28900 and only the deploy caught it).
        real = self.real_private()
        if not real:
            self.skipTest("no real private.toml here (a public clone)")
        old = os.environ["OPENCLAW_TEAM_PRIVATE"]
        os.environ["OPENCLAW_TEAM_PRIVATE"] = str(real[0])
        try:
            team, projects = teamlib.load()
            teamlib.render_files(team, projects)   # refuses (SystemExit) any AGENTS.md over the cap
        except SystemExit as e:
            self.fail(f"with the real private values: {e}")
        finally:
            os.environ["OPENCLAW_TEAM_PRIVATE"] = old

    def test_no_real_private_value_is_tracked(self):
        real = self.real_private()
        if not real:
            self.skipTest("no real private.toml here (a public clone)")
        old = os.environ.pop("OPENCLAW_TEAM_PRIVATE")
        try:
            self.assertEqual(set(teamlib.private_values()), set(tomllib.loads(teamlib.PRIVATE_EXAMPLE.read_text())["values"]),
                             "private.toml and private.example.toml must have the same keys")
            self.assertEqual(teamlib.leak_scan(teamlib.tracked_texts()), [])
        finally:
            os.environ["OPENCLAW_TEAM_PRIVATE"] = old
