"""Disk janitor (`team disk`, every 30 minutes): removes what agent tasks leave behind and warns early.

On 2026-11-07 the server's disk reached 100% and the whole team stopped for 9 hours: the gateway
could not write its state database. Nothing ran away. Leftovers had piled up for 12 days with
nothing removing them:
- Chrome profiles that killed Puppeteer runs left in the Chromium snap's private /tmp (164, 15.7 GB),
- finished git worktrees with their node_modules (102, 31 GB),
- test copies in /tmp (9 GB), test database containers and their volumes (about 6 GB).

Work is never lost:
- a worktree or repo copy with uncommitted or unpushed work keeps its files (only node_modules
  goes, after its diff is saved);
- a container with a restart policy or a compose/Coolify label is never touched;
- a named Docker volume is never touched.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

DEFAULTS = {
    # From pressure_percent the clean-up gets harder (unused CI runner caches, build cache cut to 2 GB instead of 6 GB).
    # 2026-11-23 (item 1c1b3f47): the disk sat at 85% while only the 90% / Sunday guard acted. Every run already removes the
    # safe leftovers at any level; this only starts the harder (still safe) steps earlier. Volumes with data, running
    # containers and tagged images are never touched at any level.
    "pressure_percent": 75,
    "warn_percent": 80,
    "critical_percent": 90,
    "notify_every_hours": 6,
    "profile_idle_hours": 3,
    "worktree_idle_hours": 48,
    "tmp_idle_hours": 72,
    "tmp_min_mb": 20,
    "container_idle_hours": 48,
    "volume_idle_hours": 48,
    "repo_roots": ["~/projects", "~/projects/o", "~/projects/ai", "~/worktrees"],
    "tmp_dirs": ["/tmp"],
    # root-only: cleaned through a throwaway container (the user is in the docker group)
    "root_tmp_dirs": ["/tmp/snap-private-tmp/snap.chromium/tmp"],
    "tmp_keep": ["openclaw", "qa891f", "snap-private-tmp", "systemd-private", "tmux-", "ssh-", "claude",
                 ".X11-unix", ".ICE-unix", ".font-unix", ".XIM-unix", ".Test-unix", "node-compile-cache"],
    "keep_containers": [],
    "helper_image": "alpine:3.20",
    "backup_dir": "~/backups-local/janitor",
    "state_file": "~/.cache/openclaw-team/disk.json",
}

PROFILE = re.compile(r"^(puppeteer_dev_chrome_profile-|playwright_chromiumdev_profile-|\.org\.chromium\.Chromium\.|\.com\.google\.Chrome\.)")
ANON_VOLUME = re.compile(r"^[0-9a-f]{64}$")
# A GitLab runner's job cache volume ("runner-<id>-project-<n>-concurrent-<n>-cache-<hash>"): the next job that needs it
# builds it again. On 2026-11-11 unused ones held about 9 GB.
RUNNER_CACHE = re.compile(r"^runner-[a-z0-9-]+-project-\d+-concurrent-\d+-cache-[0-9a-f]+$")
SKIP_WALK = {"node_modules", ".git", "dist", "build", ".next", "test-results", "vendor"}


def config(team):
    cfg = dict(DEFAULTS)
    cfg.update(team.get("disk", {}) if isinstance(team, dict) else {})
    return cfg


def expand(p):
    return Path(os.path.expanduser(str(p)))


def run(cmd, timeout=300):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def newest_mtime(path, limit=100000):
    """Newest mtime of anything under `path`, not counting dependency and build folders."""
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return None
    if not os.path.isdir(path) or os.path.islink(path):
        return newest
    seen = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if d not in SKIP_WALK]
        for name in files + dirs:
            seen += 1
            try:
                newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
            if seen >= limit:
                return newest
    return newest


def size_mb(path):
    r = run(["du", "-sm", str(path)], timeout=600)
    try:
        return int(r.stdout.split()[0])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------- git copies

def git(path, *args, timeout=120):
    return run(["git", "-C", str(path), *args], timeout=timeout)


def git_state(path):
    """(changed files incl. untracked, commits on no remote) or None if `path` is not a git checkout."""
    st = git(path, "status", "--porcelain")
    if st.returncode != 0:
        return None
    dirty = len([line for line in st.stdout.splitlines() if line.strip()])
    unpushed = len(git(path, "log", "--oneline", "HEAD", "--not", "--remotes").stdout.splitlines())
    return dirty, unpushed


def classify_copy(age_hours, dirty, unpushed, idle_hours):
    """remove: nothing to lose; slim: has work, only node_modules goes; keep: maybe still in use."""
    if age_hours is None or age_hours < idle_hours:
        return "keep"
    if dirty == 0 and unpushed == 0:
        return "remove"
    return "slim"


def repos_under(roots):
    out = []
    for root in roots:
        root = expand(root)
        if not root.is_dir():
            continue
        for p in [root, *sorted(root.iterdir())]:
            try:
                if (p / ".git").is_dir():
                    out.append(p)
            except OSError:
                pass
    return sorted(set(out))


def worktrees(repo):
    """Linked worktrees of `repo` (never the repo itself)."""
    out = []
    for block in git(repo, "worktree", "list", "--porcelain").stdout.strip().split("\n\n"):
        fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines() if line)
        wt = fields.get("worktree")
        if wt and Path(wt) != Path(repo):
            out.append({"path": Path(wt), "repo": Path(repo), "detached": "detached" in fields,
                        "locked": "locked" in fields})
    return out


def remove_tree(path, cfg, log):
    """rm -rf; files a test container created as root are removed through a throwaway container."""
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        parent, name = os.path.split(str(path).rstrip("/"))
        run(["docker", "run", "--rm", "-v", f"{parent}:/x", cfg["helper_image"], "rm", "-rf", f"/x/{name}"], timeout=600)
    if os.path.exists(path):
        log(f"could not remove {path}")
        return False
    return True


def save_work(copy, cfg, now):
    """The diff, the untracked files and a branch for detached unpushed commits, before node_modules goes."""
    backup = expand(cfg["backup_dir"])
    backup.mkdir(parents=True, exist_ok=True)
    name = str(copy["path"]).strip("/").replace("/", "_")
    stamp = time.strftime("%Y%m%d", time.gmtime(now))
    diff = git(copy["path"], "diff", "HEAD").stdout
    if diff:
        (backup / f"{name}-{stamp}.patch").write_text(diff)
    untracked = [u for u in git(copy["path"], "ls-files", "--others", "--exclude-standard").stdout.splitlines() if u]
    if untracked:
        run(["tar", "czf", str(backup / f"{name}-{stamp}.untracked.tgz"), "-C", str(copy["path"]), *untracked[:2000]], timeout=600)
    if copy.get("detached") and copy.get("unpushed"):
        git(copy["path"], "branch", f"backup/{Path(copy['path']).name}-{stamp}", "HEAD")


def drop_node_modules(path):
    found = run(["find", str(path), "-name", "node_modules", "-type", "d", "-prune"], timeout=600).stdout.split()
    for d in found:
        shutil.rmtree(d, ignore_errors=True)
    return len(found)


def clean_copy(copy, action, cfg, now, log, dry):
    path = copy["path"]
    if action == "remove":
        log(f"{'would remove' if dry else 'removed'} {path} (clean, pushed, idle {copy['age_h']} h)")
        if dry:
            return
        if copy.get("repo"):
            git(copy["repo"], "worktree", "remove", "--force", str(path))
        if os.path.exists(path):
            remove_tree(path, cfg, log)
        if copy.get("repo"):
            git(copy["repo"], "worktree", "prune")
    elif action == "slim":
        if not run(["find", str(path), "-name", "node_modules", "-type", "d", "-print", "-quit"], timeout=600).stdout.strip():
            return  # nothing to drop: already slim
        log(f"{'would slim' if dry else 'slimmed'} {path} (kept: {copy['dirty']} changed files, {copy['unpushed']} unpushed commits; node_modules dropped)")
        if dry:
            return
        save_work(copy, cfg, now)
        drop_node_modules(path)


def clean_worktrees(cfg, now, log, dry=False):
    done = 0
    for repo in repos_under(cfg["repo_roots"]):
        for wt in worktrees(repo):
            if wt["locked"]:
                continue
            if not wt["path"].exists():
                if not dry:
                    git(repo, "worktree", "prune")
                continue
            state = git_state(wt["path"])
            if state is None:
                continue
            newest = newest_mtime(wt["path"])
            age = None if newest is None else (now - newest) / 3600
            wt.update(dirty=state[0], unpushed=state[1], age_h=None if age is None else round(age))
            action = classify_copy(age, state[0], state[1], cfg["worktree_idle_hours"])
            if action != "keep":
                clean_copy(wt, action, cfg, now, log, dry)
                done += 1
    return done


# ---------------------------------------------------------------- /tmp

def kept(name, keep):
    return any(name.startswith(k) for k in keep)


def stale_tmp_entries(tmp_dir, now, cfg, uid=None):
    """Top-level /tmp entries left by tasks: (path, kind) with kind 'profile', 'git' or 'copy'."""
    uid = os.getuid() if uid is None else uid
    out = []
    try:
        entries = list(os.scandir(tmp_dir))
    except OSError:
        return out
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if st.st_uid != uid or kept(e.name, cfg["tmp_keep"]) or e.is_symlink():
            continue
        if PROFILE.match(e.name):
            if now - st.st_mtime >= cfg["profile_idle_hours"] * 3600:
                out.append((e.path, "profile"))
            continue
        if not e.is_dir() or now - st.st_mtime < cfg["tmp_idle_hours"] * 3600:
            continue
        newest = newest_mtime(e.path)
        if newest is None or now - newest < cfg["tmp_idle_hours"] * 3600:
            continue
        if os.path.exists(os.path.join(e.path, ".git")):
            out.append((e.path, "git"))
        elif size_mb(e.path) >= cfg["tmp_min_mb"]:
            out.append((e.path, "copy"))
    return out


def visible_ancestor(path):
    """(deepest ancestor this user can see, the rest of the path): a root-only folder hides what is inside it."""
    parts = Path(path).parts
    for i in range(len(parts), 1, -1):
        if os.path.isdir(os.path.join(*parts[:i])):
            return os.path.join(*parts[:i]), "/".join(parts[i:])
    return None, ""


def root_profile_script(folder, minutes, dry):
    """Shell for the throwaway container: count (and remove) browser profiles idle `minutes` in `folder`."""
    match = (f"find '{folder}' -mindepth 1 -maxdepth 1 \\( -name 'puppeteer_dev_chrome_profile-*' -o -name 'playwright_chromiumdev_profile-*' "
             f"-o -name '.org.chromium.Chromium.*' -o -name '.com.google.Chrome.*' \\) -mmin +{minutes}")
    return (f"[ -d '{folder}' ] || {{ echo 0; exit 0; }}; n=$({match} | wc -l); "
            + ("" if dry else f"{match} -exec rm -rf {{}} +; ") + "echo $n")


def clean_tmp(cfg, now, log, dry=False):
    done = 0
    for tmp_dir in cfg["tmp_dirs"]:
        for path, kind in stale_tmp_entries(tmp_dir, now, cfg):
            if kind == "git":
                state = git_state(path)
                if state is None:
                    continue
                common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
                repo = Path(common).parent if common and Path(common).resolve() != (Path(path) / ".git").resolve() else None
                copy = {"path": Path(path), "repo": repo, "dirty": state[0], "unpushed": state[1],
                        "detached": git(path, "symbolic-ref", "-q", "HEAD").returncode != 0,
                        "age_h": round((now - (newest_mtime(path) or now)) / 3600)}
                clean_copy(copy, classify_copy(copy["age_h"], state[0], state[1], cfg["tmp_idle_hours"]), cfg, now, log, dry)
            else:
                log(f"{'would remove' if dry else 'removed'} {path} ({kind})")
                if not dry:
                    remove_tree(path, cfg, log)
            done += 1
    for root_dir in cfg["root_tmp_dirs"]:
        mount, inner = visible_ancestor(root_dir)
        if mount is None:
            continue
        r = run(["docker", "run", "--rm", "-v", f"{mount}:/m", cfg["helper_image"], "sh", "-c",
                 root_profile_script(f"/m/{inner}".rstrip("/"), int(cfg["profile_idle_hours"] * 60), dry)], timeout=900)
        n = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else "0"
        if n.isdigit() and int(n):
            log(f"{'would remove' if dry else 'removed'} {n} browser profiles in {root_dir}")
            done += int(n)
    return done


# ---------------------------------------------------------------- docker

def parse_ts(s):
    """Docker timestamps ("2026-11-05T21:42:31.123456789Z", "...-05:00"); None for the zero time."""
    if not s or str(s).startswith("0001-"):
        return None
    s = str(s).replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


TEST_IMAGES = {"postgres", "redis", "mariadb", "mysql", "mongo", "php", "node", "python", "composer", "registry",
               "alpine", "busybox", "ubuntu", "debian", "golang", "ruby", "selenium", "chromium"}


def image_base(image):
    """'registry.example/x/node:22-slim' -> 'node'"""
    return str(image or "").rsplit("/", 1)[-1].split("@")[0].split(":")[0]


def leftover_container(c, now, idle_hours, keep=()):
    """A container a task started by hand and left: no restart policy, no compose or Coolify label, old.
    A running one only if it is a test image (database, language runtime): every service has a restart policy."""
    restart = ((c.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
    labels = (c.get("Config") or {}).get("Labels") or {}
    name = str(c.get("Name") or "").lstrip("/")
    if restart != "no" or name in keep or any(k.startswith(("com.docker.compose", "coolify")) for k in labels):
        return False
    state = c.get("State") or {}
    ref = parse_ts(c.get("Created"))
    if state.get("Status") in ("exited", "created", "dead"):
        ref = parse_ts(state.get("FinishedAt")) or ref
    elif image_base((c.get("Config") or {}).get("Image")) not in TEST_IMAGES:
        return False
    return ref is not None and now - ref >= idle_hours * 3600


def leftover_volume(v, now, idle_hours):
    """Only unnamed (64-hex) volumes no container uses, after the idle time. Named volumes are data."""
    created = parse_ts(v.get("CreatedAt"))
    return bool(ANON_VOLUME.match(v.get("Name") or "")) and created is not None and now - created >= idle_hours * 3600


def runner_cache_volume(v):
    return bool(RUNNER_CACHE.match(v.get("Name") or ""))


def clean_docker(cfg, now, log, dry=False, pressure=False):
    """pressure: the disk is past pressure_percent, so unused runner caches go too and the build cache is cut harder.
    Images: only dangling (untagged) ones are pruned, so running containers' images and tagged rollback images stay."""
    if not shutil.which("docker"):
        return 0
    done = 0
    ids = run(["docker", "ps", "-aq"]).stdout.split()
    rows = json.loads(run(["docker", "inspect", *ids]).stdout or "[]") if ids else []
    for c in rows:
        if leftover_container(c, now, cfg["container_idle_hours"], cfg["keep_containers"]):
            log(f"{'would remove' if dry else 'removed'} container {c.get('Name', '').lstrip('/')} ({(c.get('Config') or {}).get('Image')}, {c['State'].get('Status')})")
            if not dry:
                run(["docker", "rm", "-f", "-v", c["Id"]])
            done += 1
    names = run(["docker", "volume", "ls", "-q", "-f", "dangling=true"]).stdout.split()
    vols = json.loads(run(["docker", "volume", "inspect", *names]).stdout or "[]") if names else []
    old = [v["Name"] for v in vols if leftover_volume(v, now, cfg["volume_idle_hours"])]
    if old:
        log(f"{'would remove' if dry else 'removed'} {len(old)} unused unnamed volumes")
        if not dry:
            run(["docker", "volume", "rm", *old], timeout=900)
        done += len(old)
    caches = [v["Name"] for v in vols if pressure and runner_cache_volume(v)]
    if caches:
        log(f"{'would remove' if dry else 'removed'} {len(caches)} unused CI runner cache volumes")
        if not dry:
            run(["docker", "volume", "rm", *caches], timeout=900)
        done += len(caches)
    dangling = run(["docker", "image", "ls", "-q", "-f", "dangling=true"]).stdout.split()
    if dangling:
        log(f"{'would remove' if dry else 'removing'} {len(dangling)} dangling images (tagged and running images are kept)")
    cap = "2GB" if pressure else "6GB"
    if dry:
        log(f"would cut the build cache to {cap}")
    else:
        run(["docker", "image", "prune", "-f"], timeout=900)
        # Image builds leave their layers in the build cache (10 GB on 2026-11-11). A size cap keeps the newest layers
        # (base images, npm) so a rare release is not built cold; an age filter dropped them all (d68a69cb).
        # --reserved-space is the renamed --keep-storage (Docker 29).
        run(["docker", "builder", "prune", "-f", "--reserved-space", cap], timeout=1800)
    return done


# ---------------------------------------------------------------- alarm

def usage(path="/"):
    u = shutil.disk_usage(path)
    return round(u.used * 100 / u.total, 1), round(u.free / 2**30, 1)


def alarm(percent, state, now, cfg):
    """critical: every run; warn: once per notify_every_hours; below warn: nothing (and the clock resets)."""
    if percent >= cfg["critical_percent"]:
        return "critical"
    if percent >= cfg["warn_percent"]:
        if now - (state.get("notifiedAt") or 0) >= cfg["notify_every_hours"] * 3600:
            return "warn"
    return None


def janitor(team, notify, dry=False, now=None, log=print):
    cfg = config(team)
    now = time.time() if now is None else now
    before, _ = usage()
    lines = []

    def note(msg):
        lines.append(msg)
        log(msg)

    pressure = before >= min(cfg["pressure_percent"], cfg["warn_percent"])
    note(f"disk {before}%: " + (f"over {cfg['pressure_percent']}%, hard (safe) clean-up: unused CI runner caches and a 2 GB build cache cap"
                                if pressure else "normal clean-up"))
    counts = {"browser profiles and /tmp copies": clean_tmp(cfg, now, note, dry),
              "git worktrees": clean_worktrees(cfg, now, note, dry),
              "docker leftovers": clean_docker(cfg, now, note, dry, pressure=pressure)}
    percent, free = usage()
    log(f"disk {before}% -> {percent}% used, {free} GB free; cleaned: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    state_file = expand(cfg["state_file"])
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    level = None if dry else alarm(percent, state, now, cfg)
    if level:
        head = "🚩 Server disk is" if level == "critical" else "⚠️ Server disk is"
        text = (f"{head} {percent}% full ({free} GB free) after the automatic clean-up. At 100% the gateway cannot "
                f"write its database and the whole team stops (it happened on 2026-11-07). Operator: find what grows with "
                f"`du -xh --max-depth=2 / | sort -h | tail`.")
        if notify(text):
            state["notifiedAt"] = now
    if percent < cfg["warn_percent"]:
        state.pop("notifiedAt", None)
    state.update(percent=percent, freeGb=free, at=now)
    if not dry:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state))
    return {"percent": percent, "free": free, "level": level, "counts": counts, "lines": lines}
