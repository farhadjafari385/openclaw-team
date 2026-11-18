# openclaw-team

The single source of truth for an OpenClaw agent team: who the agents are, the rules they follow, the
projects they work on, their skills and tools, and the policy that enforces it all. Agents cannot change
any of it; the host is rebuilt from this repo and checked against it every 15 minutes.

This is the public copy of a team that runs in production. It holds no private data: every id, host, path,
bot name and brand is a `{{private:KEY}}` placeholder filled from a file that never enters git (see
[Private values](#private-values)). Clone it, copy `private.example.toml`, fill in your own values, and the
same code runs your team.

Three repos work together:

| Repo | Role |
|---|---|
| **openclaw-team** (this one) | Team definition: agents, personas, rules, projects, facts, skills, MCP tools. `bin/team` renders, installs, locks, verifies, reports; `bin/deploy` rolls a change out safely. |
| [openclaw-token-guard](https://github.com/farhadjafari385/openclaw-token-guard) (public) | The OpenClaw plugin that enforces the rules on every tool call, keeps the task board, follows every task to the end, names the right skills in every brief, and writes the team's Telegram threads. |
| openclaw-ui (private) | Team HQ, the team's own web UI (built by the agents). |

## Private values

The owner's ids, hosts, paths, bot names and brands are not in git. Team files hold placeholders such as
`{{private:OWNER_TELEGRAM_ID}}`, and `private.toml` (gitignored) holds the values:

```toml
[values]
OWNER_TELEGRAM_ID = "123456789"
```

`bin/team` fills every placeholder when it reads a team file, so what the agents get is the same as with the
values written inline. A missing file or key is refused by name; nothing is ever rendered half-filled.

- **Where:** `private.toml` at the repo root (where you edit), or `~/.config/openclaw-team/private.toml` on the
  host (a deploy replaces the checkout; this file stays). `$OPENCLAW_TEAM_PRIVATE` overrides both.
- **Start:** copy `private.example.toml` (every key, with fake values) and fill it in. The tests always use the
  example, so a fresh clone passes them as is.
- **Guards:** `bin/team leaks` fails if any private value, or any line of `~/.config/openclaw-privacy-patterns`,
  is in a tracked file. The pre-commit hook runs it; the pre-push hook checks every pushed commit for any remote
  not listed as private in `~/.config/openclaw-team-private-remotes`. Enable both with
  `git config core.hooksPath .githooks`.
- **Change a value:** edit `private.toml`, copy it to the host's `~/.config/openclaw-team/private.toml`, then
  `bin/team apply`.
- **Too long with real values:** real values are longer than the fake ones, so a generated `AGENTS.md` can pass
  the tests with the example and still go over the size cap. The tests also render with your real
  `private.toml` when one is present, and `bin/deploy` renders with it before it touches the gateway.

## Public and private repos

The owner works in a private repo (`openclaw-team-private`) whose history predates the placeholders. This public
repo shares no history with it: each publish is one new commit holding the private repo's current tree, and it
goes out only after `bin/team leaks` (the tree) and `bin/team leaks --commits` (the new commit) both find nothing
with the real values loaded. The pre-push hook runs the same check again, because this remote is not listed as
private. Pull requests and issues are welcome here; accepted changes are applied in the private repo and come
back with the next publish.

## Install on a host

Requirements: OpenClaw 2026.10.5+ with its gateway as the systemd user unit `openclaw-gateway`, Python 3.11+,
passwordless `sudo` for `chattr` (file locking; set `lock_files = false` in `team.toml` without it).

```bash
git clone <this repo> ~/openclaw-team
git clone https://github.com/farhadjafari385/openclaw-token-guard.git ~/openclaw-token-guard
cd ~/openclaw-team
mkdir -p ~/.config/openclaw-team && cp private.example.toml ~/.config/openclaw-team/private.toml   # then fill it in
python3 -m unittest discover -s tests          # the repo checks itself
bin/team apply --dry-run                        # every file and config key it would change
bin/team apply                                  # backup, install, lock
systemctl --user restart openclaw-gateway       # agents are back in about 10 minutes
bin/team verify                                 # PASS/FAIL per check (files, config, skills, tools, timers, bots)
```

After that, roll every change out with `bin/deploy --pull` (or `bin/deploy <archive>.tgz`): it waits until no
task is running and the orchestrator is idle, stops new dispatches (maintenance flag), restarts the gateway
once, verifies, and pushes.

## Layout

```
team.toml         agents (id, name, bot, handle, role, tools, skills, mcp), models, limits, follow-ups, triage,
                  board, sessions, crons kept off, tool trim, MCP servers
projects.toml     project registry: name, aliases, Telegram topic, server and laptop paths
rules/            core.md (every agent), orchestrator.md, worker.md
roles/<id>.md     one role's duties and definition of done
personas/<id>.md  who each agent is (also its SOUL.md)
facts.md          hosts, credentials (locations only), tools, git, CI, release facts
owner.md          the owner's profile (USER.md)
skills/<id>/      the team's own skills per agent (role playbooks, reviewed lessons)
vendor-skills/    third-party skills, copied unchanged, each with its LICENSE and a NOTICE (source and commit)
bin/team          render | apply | verify | drift | report | gc | reset | leaks
bin/deploy        the safe rollout described above
```

Each agent's `AGENTS.md` is generated as persona + core rules + (orchestrator rules | role + worker rules) +
team roster + projects + facts, and stays below 80% of `bootstrap_max_chars`, so nothing is ever truncated.

## Skills

* Every agent sees only the skills listed in its `skills = [...]` in `team.toml` (an OpenClaw allowlist).
  Agents never write their own: OpenClaw's Skill Workshop is off and `team apply` moves any agent-written skill
  into the backup.
* The guard names the skills that fit each task in the brief ("Skills for this task: ..."), matched against
  each skill's own description, so they are used without anyone remembering them.
* `vendor-skills/` holds the third-party skills (Anthropic skills and knowledge-work plugins, obra/superpowers,
  Leonxlnx/taste-skill, nextlevelbuilder/ui-ux-pro-max, pbakaus/impeccable, Dammyjay93/interface-design,
  emilkowalski/skills, coreyhaines31/marketingskills). `team apply` copies them byte for byte to
  `~/.openclaw/vendor-skills`, which OpenClaw loads for every agent; `team verify` checks every file's hash.

**Add a skill:** copy its folder into `vendor-skills/<name>/` unchanged with its `LICENSE` and a `NOTICE.md`
(source URL and commit), add the name to the agents' `skills = [...]`, run the tests, deploy. Read its scripts
first: what does it reach over the network, does it run commands, does it need credentials?

## MCP tools

`[mcp.servers.<name>]` in `team.toml` defines a server (stdio `command`/`args`, or `url`/`transport`), with the
version pinned. An agent may use a server only when it is in its `mcp = [...]`; every other agent gets a deny
rule for `<name>__*`. Today: `devtools` (Google's Chrome DevTools MCP) for QA, `context7` (current library docs)
for the writer, reviewer and devops.

## Tokens

Every model call re-sends the system prompt and every tool definition (about 128k characters of tool
definitions alone before the trim). `[tool_trim]` leaves out tools an agent can never use on this host
(desktop and UI tools for everyone; dispatch and messaging tools for workers), which saves more than the
skills and MCP tools add. Measure with a probe run before and after a change: the run report's
`systemPrompt.chars`, `tools.schemaChars` and the token usage.

## Enforcement

1. **token-guard** checks every tool call: rules, personas, the guard and the OpenClaw config are read-only for
   agents; no hidden channels; only the orchestrator dispatches and does no specialist work; no model pins;
   one request is one board item; tasks are isolated, deleted when done, time-boxed and budgeted (`task_budget_calls` tool calls per task, `orchestrator_run_calls` per orchestrator run: the Claude CLI reports too little token usage for a token budget to work); the triage does not wake the orchestrator twice for the same board; one item at a time (`[board] wip`), in the PM's order (`project_order` for the rest); workers never wait for CI, the guard follows the pipeline (`[ci]`); usage limits
   stop new dispatches until the reset (no Codex or OpenAI fallback: the owner's order).
2. **Locked files:** generated files are `chattr +i`, so even a shell write fails.
3. **Drift check:** a timer runs `team drift --fix --notify` every 15 minutes. It restores only what was last
   applied (a newer repo version goes out through `team apply`, never through drift).
4. **Daily report:** `team report --send` (08:00) posts tokens per agent, tasks and outcomes, stalls, guard
   blocks, restarts, open work and drift. No model is used.

## Commands

```
bin/team render [--out DIR]      show what would be generated (and the config diff)
bin/team apply [--dry-run]       install on this host (backup first, then lock)
bin/team verify [-v]             prove this host matches the repo (also checks every bot's @handle with Telegram)
bin/team drift [--fix] [--notify]
bin/team report [--send]
bin/team gc [--all]              delete finished and idle sessions
bin/team reset --yes             one-time clean slate (archives memory and boards, deletes all sessions)
bin/team leaks [--commits A..B]  refuse if any private value or privacy pattern is in the tree or those commits
bin/deploy --pull | <tgz>...     the safe rollout
```

Tests: `python3 -m unittest discover -s tests` (they use `private.example.toml`, so a fresh clone passes; with a
real `private.toml` present they also check the real render).

## License

MIT: see [LICENSE](LICENSE). Each skill in `vendor-skills/` keeps its own `LICENSE` and `NOTICE.md`, and those
terms apply to that skill.

## Disclaimer

This software is provided "as is", under the MIT License, without warranty of any kind. You use it at your own
risk. The authors and contributors accept no responsibility and no liability for anything that results from
using, copying, changing or deploying it, including lost or damaged data, outages, security incidents, suspended
accounts, and bills (model, API, cloud or any other charges).

It comes with no support and no promise: no one is obliged to answer issues, fix bugs, review changes or keep it
working. The agents it runs execute commands and spend money on the accounts you connect, so read the code, review
the configuration and test it on a machine and accounts you can afford to lose. Nothing here is legal, security or
financial advice.
