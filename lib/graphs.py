"""Code maps (graphify) for the team's code projects: agents query a map instead of reading code.

Each project's repos (projects.toml `graph`) are exported from origin's default branch with
`git archive` (binaries and big files left out) into ~/graphs/<project>/src[/<repo>], and
`graphify update` builds ~/graphs/<project>/graphify-out/graph.json from them: tree-sitter only,
no AI calls, no tokens. A repo is exported again only when its branch moved. The main checkouts
are never touched (they sit on agent branches), and nothing is written into any repo.

graphify is pinned (team.toml [graphs] version) in its own venv and linked as ~/.openclaw/bin/graphify.
The guard makes every task in a project with a map query it before searching or reading code.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

DEFAULTS = {
    "version": "0.9.104",
    "root": "~/graphs",
    "tool_dir": "~/.openclaw/tools/graphify",
    "bin_link": "~/.openclaw/bin/graphify",
    "budget": 1500,
    "max_file_kb": 1024,
    "build_timeout_minutes": 20,
    "stale_hours": 3,
    "skip_ext": [
        "png", "jpg", "jpeg", "gif", "webp", "avif", "ico", "bmp", "tif", "tiff", "psd", "xcf", "svgz",
        "blend", "blend1", "fbx", "glb", "gltf", "obj", "stl", "ktx2", "hdr", "exr",
        "mp3", "ogg", "wav", "flac", "m4a", "mp4", "webm", "mov", "avi", "mkv",
        "ttf", "otf", "woff", "woff2", "eot", "zip", "gz", "tgz", "bz2", "xz", "7z", "rar", "jar", "apk", "aab",
        "pdf", "bin", "exe", "dll", "so", "dylib", "wasm", "onnx", "pt", "pth", "safetensors", "ckpt",
        "npy", "npz", "db", "sqlite", "lock", "map",
    ],
}
OUT = "graphify-out"  # graphify's own output name, which its scan always skips


def expand(p):
    return Path(os.path.expanduser(str(p)))


def config(team):
    c = dict(DEFAULTS)
    c.update(team.get("graphs", {}) or {})
    return c


def graph_projects(projects):
    """[(name, server path, [(key, repo path)])] for every project that lists repos to map."""
    out = []
    for p in projects:
        repos, server = p.get("graph") or [], p.get("server")
        if not repos or not server or "<" in server:
            continue
        base = expand(server)
        out.append((p["name"], base, [(r if r != "." else ".", base if r == "." else base / r) for r in repos]))
    return out


def graph_file(c, name):
    return expand(c["root"]) / name / OUT / "graph.json"


def run(cmd, timeout=120, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(cmd, 124, "", str(e))


# ---------------------------------------------------------------- the tool

def tool_version(c):
    py = expand(c["tool_dir"]) / "bin" / "python"
    if not py.exists():
        return ""
    r = run([str(py), "-c", "import importlib.metadata as m; print(m.version('graphifyy'))"], timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


def ensure_tool(c, log=print):
    """The pinned graphify in its own venv, linked into ~/.openclaw/bin. Returns its path."""
    tool = expand(c["tool_dir"])
    exe = tool / "bin" / "graphify"
    if not (tool / "bin" / "python").exists():
        log(f"graphs: creating venv {tool}")
        r = run([sys.executable, "-m", "venv", str(tool)], timeout=300)
        if r.returncode:
            raise SystemExit(f"graphs: venv failed: {r.stderr.strip()[-300:]}")
    have = tool_version(c)
    if have != c["version"]:
        log(f"graphs: installing graphifyy {c['version']} (had {have or 'none'})")
        r = run([str(tool / "bin" / "pip"), "install", "-q", "--disable-pip-version-check", f"graphifyy=={c['version']}"], timeout=1200)
        if r.returncode or tool_version(c) != c["version"]:
            raise SystemExit(f"graphs: installing graphifyy {c['version']} failed: {r.stderr.strip()[-400:]}")
    link = expand(c["bin_link"])
    if not (link.is_symlink() and os.readlink(link) == str(exe)):
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp = link.with_name(link.name + ".new")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(exe)
        os.replace(tmp, link)
    return exe


# ---------------------------------------------------------------- export

def default_ref(repo):
    r = run(["git", "-C", str(repo), "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    for ref in ("origin/main", "origin/master"):
        if run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", ref]).returncode == 0:
            return ref
    return "HEAD"


def keep_member(m, c):
    """Source files only: no binaries, no big files, nothing that escapes the folder."""
    if not (m.isfile() or m.isdir()):
        return False
    name = m.name.lstrip("./")
    if name.startswith("/") or ".." in Path(name).parts:
        return False
    if m.isfile():
        ext = name.rsplit(".", 1)[-1].lower() if "." in Path(name).name else ""
        if ext in set(c["skip_ext"]) or m.size > int(c["max_file_kb"]) * 1024:
            return False
    return True


def export(repo, ref, dst, c):
    """Export `ref` of `repo` into dst (replaced only once the new copy is complete). Returns file count."""
    tmp = dst.with_name(dst.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    proc = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", ref], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    files = 0
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for m in tar:
                if keep_member(m, c):
                    tar.extract(m, tmp, filter="data")
                    files += m.isfile()
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode(errors="replace")
        proc.stderr.close()
        proc.wait()
    if proc.returncode or files == 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"git archive {ref} in {repo} failed: {err.strip()[-200:] or 'no files'}")
    old = dst.with_name(dst.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dst.exists():
        os.replace(dst, old)
    os.replace(tmp, dst)
    shutil.rmtree(old, ignore_errors=True)
    return files


# ---------------------------------------------------------------- build

def load_state(c):
    try:
        return json.loads((expand(c["root"]) / ".state.json").read_text())
    except (OSError, ValueError):
        return {"repos": {}, "built": {}}


def save_state(c, state):
    p = expand(c["root"]) / ".state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, p)


def build_project(name, repos, c, exe, state, force=False, log=print):
    pd = expand(c["root"]) / name
    single = len(repos) == 1 and repos[0][0] == "."
    changed = False
    for key, repo in repos:
        if not (repo / ".git").exists():
            log(f"graphs: {name}: {repo} is not a git checkout, skipped")
            continue
        run(["git", "-C", str(repo), "fetch", "-q", "origin"], timeout=180)  # best effort: the last fetched branch still works
        ref = default_ref(repo)
        sha = run(["git", "-C", str(repo), "rev-parse", ref]).stdout.strip()
        dst = pd / "src" if single else pd / "src" / Path(key).name
        seen = state["repos"].setdefault(name, {})
        if not force and seen.get(key) == sha and dst.exists():
            continue
        n = export(repo, ref, dst, c)
        seen[key] = sha
        changed = True
        log(f"graphs: {name}: exported {key} at {ref} {sha[:8]} ({n} files)")
    if not single and (pd / "src").exists():
        wanted = {Path(k).name for k, _ in repos}
        for d in (pd / "src").iterdir():
            if d.name not in wanted:
                shutil.rmtree(d, ignore_errors=True)
                changed = True
    graph = pd / OUT / "graph.json"
    if not (changed or force or not graph.exists()):
        return {"name": name, "built": False, "graph": str(graph)}
    env = dict(os.environ, GRAPHIFY_OUT=str(pd / OUT), GRAPHIFY_VIZ_NODE_LIMIT="1")
    r = run([str(exe), "update", str(pd / "src"), "--force"], timeout=int(c["build_timeout_minutes"]) * 60, env=env, cwd=str(pd))
    m = re.search(r"Rebuilt: (\d+) nodes, (\d+) edges", r.stdout + r.stderr)
    if r.returncode or not graph.exists():
        raise RuntimeError(f"graphify update for {name} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    state["built"][name] = {"at": time.time(), "nodes": int(m.group(1)) if m else None, "edges": int(m.group(2)) if m else None}
    log(f"graphs: {name}: map rebuilt" + (f", {m.group(1)} nodes, {m.group(2)} edges" if m else ""))
    return {"name": name, "built": True, "graph": str(graph)}


def build_all(team, projects, force=False, log=print):
    """Install or upgrade graphify, then refresh every project's map. One run at a time."""
    c = config(team)
    root = expand(c["root"])
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("graphs: another build is running")
            return []
        exe = ensure_tool(c, log)
        state = load_state(c)
        if state.get("version") != c["version"]:
            force, state["version"] = True, c["version"]  # a new graphify rebuilds every map
        results, errors = [], []
        for name, _, repos in graph_projects(projects):
            try:
                results.append(build_project(name, repos, c, exe, state, force, log))
            except (RuntimeError, OSError) as e:
                errors.append(f"{name}: {e}")
                log(f"graphs: {name}: FAILED: {e}")
            save_state(c, state)
        state["lastRun"] = time.time()
        state["errors"] = errors
        save_state(c, state)
        return results


def checks(team, projects, now=None):
    """(ok, text) lines for `team verify`."""
    c = config(team)
    now = now or time.time()
    state = load_state(c)
    out = [(tool_version(c) == c["version"], f"graphify {tool_version(c) or 'missing'} (pinned {c['version']})")]
    last = state.get("lastRun") or 0
    out.append((now - last < float(c["stale_hours"]) * 3600 and not state.get("errors"),
                f"code maps refreshed {int((now - last) / 60)} min ago" + (f", errors: {state['errors']}" if state.get("errors") else "")))
    for name, _, _ in graph_projects(projects):
        out.append((graph_file(c, name).exists(), f"code map for {name}"))
    return out


def guard_settings(team, projects):
    """What the guard needs: which projects have a map, where it is, and the command to query it."""
    c = config(team)
    names = [n for n, _, _ in graph_projects(projects)]
    # Only agents with a shell can run graphify; the guard holds no one else to it.
    shell = [a["id"] for a in team.get("agents", []) if "exec" not in (a.get("tools") or {}).get("deny", [])]
    return {"enabled": bool(names), "root": c["root"], "bin": c["bin_link"], "budget": int(c["budget"]), "projects": names, "agents": shell}
