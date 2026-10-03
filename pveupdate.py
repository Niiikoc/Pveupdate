#!/usr/bin/env python3
"""pveupdate - check and update selected Proxmox LXCs and VMs on demand.

Runs on the Proxmox host as root. You choose which guests to track, check
them for pending updates, and update the ones you pick. Each update can take
a snapshot first and can run an app-specific update step (community-scripts
`update`, or your own command) after the OS packages.

Usage:
  pveupdate.py                  interactive menu
  pveupdate.py track [ID ...]   choose which guests to track (or add these IDs)
  pveupdate.py untrack ID ...   stop tracking guests
  pveupdate.py list             show tracked guests and their settings
  pveupdate.py check [ID ...]   show pending OS and app updates (changes nothing)
  pveupdate.py update [ID ...]  snapshot + update the given guests (asks if no IDs)
                                IDs can also be `all` or `pending`
  pveupdate.py status           show the result of the last check/update
  pveupdate.py serve            HTTP API for the Home Assistant integration
  pveupdate.py token            print the API token
  pveupdate.py set ID [options] change a guest's settings (see `set --help`)

For Home Assistant / remote use see pveupdate-remote and the README.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("PVEUPDATE_CONFIG", "/etc/pveupdate.json")
STATUS_PATH = os.environ.get("PVEUPDATE_STATUS", "/var/lib/pveupdate/status.json")
LOG_PATH = os.environ.get("PVEUPDATE_LOG", "/var/log/pveupdate.log")
LOCK_PATH = os.environ.get("PVEUPDATE_LOCK", "/run/pveupdate.lock")
TOKEN_PATH = os.environ.get("PVEUPDATE_TOKEN", "/etc/pveupdate.token")
SNAP_PREFIX = "pveupd"
VERSION = "0.5.1"
EXEC_TIMEOUT = 3600

DEFAULTS = {
    "snapshot": True,
    "keep_snapshots": 3,
    "autoremove": True,
    # When a snapshot isn't possible (directory storage, bind mounts), take a
    # vzdump backup instead. Storage None = first active backup storage.
    "backup_fallback": True,
    "backup_storage": None,
    "keep_backups": 1,
    # RAM (MB) an LXC gets while its app update runs; apps that compile
    # (Zigbee2MQTT, Obico...) can crawl for hours in 1 GB. 0 = never change.
    "build_memory": 2048,
}
BACKUP_NOTE = "pveupdate: before update"

# Apps not installed by community-scripts: how to read the installed version
# and where releases live. Community-scripts apps are detected automatically.
APP_PRESETS = {
    "zigbee2mqtt": {
        "version_cmd": "grep -m1 '\"version\"' /opt/zigbee2mqtt/package.json | cut -d'\"' -f4",
        "github": "Koenkk/zigbee2mqtt",
    },
}

OS_CHECK = r"""
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null 2>&1 || { echo "ERR apt-get update failed"; exit 1; }
  apt list --upgradable 2>/dev/null | grep -v '^Listing' | cut -d/ -f1
elif command -v apk >/dev/null; then
  apk update -q >/dev/null 2>&1
  apk version -l '<' 2>/dev/null | tail -n +2 | awk '{print $1}'
else
  echo "ERR no supported package manager"; exit 1
fi
"""

OS_UPGRADE = r"""
set -e
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade
  [ "$AUTOREMOVE" = 1 ] && apt-get -y autoremove --purge || true
elif command -v apk >/dev/null; then
  apk update && apk upgrade
else
  echo "no supported package manager"; exit 1
fi
"""

REBOOT_CHECK = "test -f /var/run/reboot-required && echo yes || echo no"

# Line 1: reboot needed (yes/no). Line 2: OS name and version, e.g. "Debian 12.11".
# Line 3: the OS version pending updates will bring, if they change it.
# Run after OS_CHECK, so the package lists are fresh.
GUEST_INFO = REBOOT_CHECK + r"""
[ -r /etc/os-release ] && . /etc/os-release
v=$VERSION_ID
[ "$ID" = debian ] && [ -r /etc/debian_version ] && v=$(cat /etc/debian_version)
echo "${NAME%% *} $v"
new=""
if [ "$ID" = debian ] && apt list --upgradable 2>/dev/null | grep -q '^base-files/'; then
  # The point release is in the new base-files package's /etc/debian_version.
  d=$(mktemp -d) && (cd "$d" && apt-get download -qq base-files >/dev/null 2>&1 &&
    dpkg-deb --fsys-tarfile base-files_*.deb | tar -xO ./etc/debian_version) > "$d/v" 2>/dev/null &&
    new=$(cat "$d/v")
  rm -rf "$d"
elif [ "$ID" = alpine ]; then
  new=$(apk list -u alpine-release 2>/dev/null | sed -n 's/^alpine-release-\([0-9.]*\)-r.*/\1/p' | head -n1)
fi
[ -n "$new" ] && [ "$new" != "$v" ] && echo "${NAME%% *} $new"
true
"""

# Prints the URL of the community-scripts ct script this container was made from.
DETECT_APP = r"""
if [ -x /usr/bin/update ]; then
  echo "community $(grep -o 'https://[^\"]*/ct/[A-Za-z0-9_.-]*\.sh' /usr/bin/update | head -n1)"
fi
"""

QUIET = False


# ---------------------------------------------------------------- helpers

def c(text, code):
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def ok(t): return c(t, "32")
def warn(t): return c(t, "33")
def bad(t): return c(t, "31")
def dim(t): return c(t, "2")


def say(*a):
    if not QUIET:
        print(*a, flush=True)


def run(cmd, check=True):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def run_stream(cmd, on_line):
    """Run cmd, calling on_line(line) for each output line as it arrives.
    Returns (exit_code, combined_output)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    out = []
    for line in p.stdout:
        out.append(line)
        on_line(line)
    return p.wait(), "".join(out)


def ask(prompt, default=True):
    suffix = " [Y/n] " if default else " [y/N] "
    ans = input(prompt + suffix).strip().lower()
    return default if not ans else ans.startswith("y")


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def log(text):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def acquire_lock():
    """Only one check/update runs at a time (matters when triggered remotely)."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another pveupdate run is in progress.")
    return fh


def is_locked():
    try:
        with open(LOCK_PATH, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- config / status

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_PATH, {})
    cfg.setdefault("defaults", {})
    for k, v in DEFAULTS.items():
        cfg["defaults"].setdefault(k, v)
    cfg.setdefault("guests", {})
    return cfg


def save_config(cfg):
    save_json(CONFIG_PATH, cfg)


def load_status():
    st = load_json(STATUS_PATH, {})
    st.setdefault("guests", {})
    return st


def setting(cfg, gid, key):
    return cfg["guests"][gid].get(key, cfg["defaults"][key])


# ---------------------------------------------------------------- guests

def list_guests():
    """All guests on this node: {id: {type, name, status}}."""
    out = {}
    for line in run(["pct", "list"]).stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            # VMID Status [Lock] Name
            out[parts[0]] = {"type": "lxc", "status": parts[1], "name": parts[-1]}
    for line in run(["qm", "list"]).stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3:
            # VMID NAME STATUS MEM BOOTDISK PID
            out[parts[0]] = {"type": "vm", "name": parts[1], "status": parts[2]}
    return dict(sorted(out.items(), key=lambda kv: int(kv[0])))


def guest_exec(gtype, gid, script, env=None, interactive=False, on_line=None):
    """Run a shell script inside a guest. Returns (exit_code, stdout, stderr).

    interactive=True (LXC only) connects the command to your terminal so its
    output streams live and prompts can be answered. on_line (LXC only) is
    called with each output line as it arrives, for progress.
    """
    if env:
        script = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items()) + script
    if gtype == "lxc":
        cmd = ["pct", "exec", gid, "--", "sh", "-c", script]
        if interactive:
            return subprocess.call(cmd), "", ""
        if on_line:
            code, out = run_stream(cmd, on_line)
            return code, out, ""
        r = run(cmd, check=False)
        return r.returncode, r.stdout, r.stderr
    # VM via QEMU guest agent (never interactive)
    cmd = ["qm", "guest", "exec", gid, "--timeout", str(EXEC_TIMEOUT), "--", "sh", "-c", script]
    r = run(cmd, check=False)
    if r.returncode != 0:
        return r.returncode, "", r.stderr.strip() or "guest agent not responding"
    res = json.loads(r.stdout)
    if not res.get("exited"):
        return 124, res.get("out-data", ""), "timed out waiting for command"
    return res.get("exitcode", 1), res.get("out-data", ""), res.get("err-data", "")


def is_running(gtype, gid):
    tool = "pct" if gtype == "lxc" else "qm"
    return "running" in run([tool, "status", gid], check=False).stdout


# ---------------------------------------------------------------- snapshots

def take_snapshot(gtype, gid):
    name = f"{SNAP_PREFIX}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    tool = "pct" if gtype == "lxc" else "qm"
    r = run([tool, "snapshot", gid, name, "--description", "before pveupdate"], check=False)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()
    return name, None


def disk_signature(gtype, gid):
    """Fingerprint of a guest's disks and mount points. A guest that couldn't
    be snapshotted is retried only when this changes (disk moved or added)."""
    tool = "pct" if gtype == "lxc" else "qm"
    r = run([tool, "config", gid, "--current"], check=False)
    disks = sorted(line for line in r.stdout.splitlines()
                   if re.match(r"(rootfs|mp\d+|scsi\d+|virtio\d+|sata\d+|ide\d+|efidisk\d+|tpmstate\d+):", line))
    return hashlib.sha1("\n".join(disks).encode()).hexdigest()[:12]


def backup_storage(cfg, gid):
    """Configured backup storage, or the first active storage that holds backups."""
    st = setting(cfg, gid, "backup_storage")
    if st:
        return st
    r = run(["pvesm", "status", "--content", "backup"], check=False)
    rows = [line.split() for line in r.stdout.splitlines()[1:] if line.strip()]
    active = [row for row in rows if len(row) > 2 and row[2] == "active"]
    # Prefer Proxmox Backup Server if there is one.
    active.sort(key=lambda row: row[1] != "pbs")
    return active[0][0] if active else None


def take_backup(cfg, gtype, gid, on_line=None):
    """vzdump fallback for guests that can't be snapshotted. Returns (storage, error)."""
    storage = backup_storage(cfg, gid)
    if not storage:
        return None, "no storage with backup content found (set one with `pveupdate set default --backup-storage NAME`)"
    say(f"  snapshot not possible, taking a backup to {storage} instead (this can take a while)...")
    code, out = run_stream(["vzdump", gid, "--storage", storage, "--mode", "snapshot", "--compress", "zstd",
                            "--notes-template", BACKUP_NOTE, "--prune-backups", "keep-all=1"],
                           on_line or (lambda line: None))
    log(out)
    if code != 0:
        lines = out.strip().splitlines()
        return None, lines[-1] if lines else f"vzdump exit {code}"
    return storage, None


def prune_backups(storage, gid, keep):
    """Remove older pveupdate backups of this guest; never touches other backups."""
    node = socket.gethostname()
    r = run(["pvesh", "get", f"/nodes/{node}/storage/{storage}/content", "--content", "backup",
             "--vmid", gid, "--output-format", "json"], check=False)
    if r.returncode != 0:
        return
    ours = sorted((b for b in json.loads(r.stdout) if (b.get("notes") or "").startswith(BACKUP_NOTE)),
                  key=lambda b: b.get("ctime", 0))
    for b in ours[:-keep] if keep > 0 else ours:
        run(["pvesm", "free", b["volid"]], check=False)
        say(dim(f"    removed old backup {b['volid']}"))


def prune_snapshots(gtype, gid, keep):
    node = socket.gethostname()
    kind = "lxc" if gtype == "lxc" else "qemu"
    r = run(["pvesh", "get", f"/nodes/{node}/{kind}/{gid}/snapshot", "--output-format", "json"], check=False)
    if r.returncode != 0:
        return
    snaps = sorted(s["name"] for s in json.loads(r.stdout) if s["name"].startswith(SNAP_PREFIX + "-"))
    tool = "pct" if gtype == "lxc" else "qm"
    for name in snaps[:-keep] if keep > 0 else snaps:
        run([tool, "delsnapshot", gid, name], check=False)
        say(dim(f"    removed old snapshot {name}"))


# ---------------------------------------------------------------- app versions

_http_cache = {}


def http_get(url, headers=None):
    if url in _http_cache:
        return _http_cache[url]
    req = urllib.request.Request(url, headers={"User-Agent": "pveupdate", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode()
    except Exception:
        body = None
    _http_cache[url] = body
    return body


def latest_github_release(repo):
    body = http_get(f"https://api.github.com/repos/{repo}/releases/latest",
                    {"Accept": "application/vnd.github+json"})
    try:
        return json.loads(body)["tag_name"].lstrip("v")
    except Exception:
        return None


def community_app_source(script_url):
    """From a community-scripts ct script, find (version_file_name, github_repo).

    Their update scripts record the installed version in /root/.<app> and
    compare it with `check_for_gh_release "<app>" "<owner/repo>"`.
    """
    body = http_get(script_url) or ""
    m = re.search(r'check_for_gh_release\s+"([^"]+)"\s+"([^"]+)"', body)
    if m:
        return m.group(1).lower().replace(" ", ""), m.group(2)
    name = script_url.rsplit("/", 1)[-1].removesuffix(".sh")
    return name.lower(), None


def app_version(gtype, gid, app):
    """Returns {label, installed, latest, update_available} or None if unknown."""
    preset = APP_PRESETS.get(app.get("preset", ""), {})
    version_cmd = app.get("version_cmd") or preset.get("version_cmd")
    repo = app.get("github") or preset.get("github")
    label = app.get("preset") or app.get("script_name") or (repo or "").split("/")[-1] or "app"
    if not version_cmd and app.get("script"):
        vfile, gh = community_app_source(app["script"])
        version_cmd = f"cat /root/.{vfile} 2>/dev/null"
        repo = repo or gh
    if not version_cmd:
        return None
    code, out, _ = guest_exec(gtype, gid, version_cmd)
    installed = out.strip().lstrip("v") if code == 0 else ""
    latest = latest_github_release(repo) if repo else None
    return {
        "label": label,
        "installed": installed or None,
        "latest": latest,
        "update_available": bool(installed and latest and installed != latest),
    }


def app_text(info):
    if info is None:
        return dim("app: version unknown")
    label, inst, latest = info["label"], info["installed"], info["latest"]
    if not inst:
        return dim(f"{label}: version unknown")
    if not latest:
        return f"{label} {inst}"
    if info["update_available"]:
        return warn(f"{label} {inst} -> {latest} available")
    return ok(f"{label} {inst} (latest)")


# ---------------------------------------------------------------- commands

def cmd_track(cfg, args):
    guests = list_guests()
    removed = forget_missing(cfg, guests)
    if args and args.ids:
        add_guests(cfg, guests, args.ids)
        save_config(cfg)
        return
    if not guests:
        print("No guests found on this node.")
        if removed:
            save_config(cfg)
        return
    print("Guests on this node (* = tracked):\n")
    ids = list(guests)
    for i, gid in enumerate(ids, 1):
        g = guests[gid]
        mark = "*" if gid in cfg["guests"] else " "
        print(f"  {i:>2}. [{mark}] {gid:<5} {g['type']:<4} {g['name']:<25} {dim(g['status'])}")
    print("\nEnter numbers to toggle (e.g. `1 3 5`), `all`, or empty to keep as is.")
    sel = input("> ").strip()
    if sel == "all":
        add_guests(cfg, guests, [gid for gid in ids if gid not in cfg["guests"]])
    elif sel:
        try:
            picks = [ids[int(x) - 1] for x in sel.replace(",", " ").split()]
        except (ValueError, IndexError):
            sys.exit(f"Invalid selection: {sel} (use the numbers from the list, 1-{len(ids)})")
        for gid in picks:
            if gid in cfg["guests"]:
                del cfg["guests"][gid]
                print(f"  untracked {gid} {guests[gid]['name']}")
            else:
                add_guests(cfg, guests, [gid])
    elif not removed:
        return
    save_config(cfg)
    print(f"\nSaved to {CONFIG_PATH}")


def add_guests(cfg, guests, ids):
    for gid in ids:
        if gid not in guests:
            print(f"  {gid}: no such guest on this node")
            continue
        g = guests[gid]
        entry = cfg["guests"].setdefault(gid, {"type": g["type"], "name": g["name"]})
        entry["name"] = g["name"]
        print(f"  tracking {gid} {g['name']}")
        if g["type"] == "lxc" and g["status"] == "running" and "app" not in entry:
            detect_app(entry, gid)


def forget_missing(cfg, guests):
    """Stop tracking guests that were deleted from Proxmox."""
    missing = [gid for gid in cfg["guests"] if gid not in guests]
    if not missing:
        return []
    st = load_status()
    for gid in missing:
        print(f"  {gid} {cfg['guests'][gid]['name']} no longer exists, untracked")
        del cfg["guests"][gid]
        st["guests"].pop(gid, None)
    save_json(STATUS_PATH, st)
    return missing


def cmd_untrack(cfg, args):
    for gid in args.ids:
        if cfg["guests"].pop(gid, None) is None:
            print(f"  {gid} was not tracked")
        else:
            print(f"  untracked {gid}")
    st = load_status()
    for gid in args.ids:
        st["guests"].pop(gid, None)
    save_json(STATUS_PATH, st)
    save_config(cfg)


def detect_app(entry, gid):
    """Fill entry['app'] for community-scripts containers. Returns True if changed."""
    code, out, _ = guest_exec("lxc", gid, DETECT_APP)
    out = out.strip()
    if code != 0 or not out.startswith("community"):
        return False
    url = out.split(" ", 1)[1] if " " in out else ""
    name = url.rsplit("/", 1)[-1].removesuffix(".sh")
    app = entry.setdefault("app", {"cmd": "update", "interactive": True})
    if url:
        app["script"] = url
        app["script_name"] = name
    say(f"    found community-scripts app{(' ' + name) if name else ''}: will run `update` after OS packages")
    return True


def cmd_list(cfg, args):
    if not cfg["guests"]:
        print("No guests tracked yet. Run `pveupdate.py track`.")
        return
    for gid, g in sorted(cfg["guests"].items(), key=lambda kv: int(kv[0])):
        app = g.get("app")
        app_txt = f"app: `{app['cmd']}`" if app else "no app step"
        snap = "snapshot" if setting(cfg, gid, "snapshot") else "no snapshot"
        print(f"  {gid:<5} {g['type']:<4} {g['name']:<25} {snap:<12} {app_txt}")


def pick_ids(cfg, ids, status=None):
    tracked = sorted(cfg["guests"], key=int)
    if not ids or ids == ["all"]:
        return tracked
    if ids == ["pending"]:
        st = (status or load_status())["guests"]
        return [g for g in tracked if g in st and is_pending(st[g])]
    unknown = [i for i in ids if i not in cfg["guests"]]
    if unknown:
        sys.exit(f"Not tracked: {', '.join(unknown)} (run `track` first)")
    return ids


def is_pending(entry):
    return bool(entry.get("packages")) or bool((entry.get("app") or {}).get("update_available"))


def check_guest(cfg, gid):
    """Returns a status entry: {name, type, state, packages, package_names, app, error}."""
    g = cfg["guests"][gid]
    res = {"name": g["name"], "type": g["type"], "checked_at": now()}
    if not is_running(g["type"], gid):
        res.update(state="stopped", packages=0, package_names=[])
        return res
    code, out, err = guest_exec(g["type"], gid, OS_CHECK)
    if code != 0:
        res.update(state="error", error=(out + err).strip()[:300], packages=0, package_names=[])
        return res
    pkgs = [p for p in out.split() if p]
    res.update(state="ok", packages=len(pkgs), package_names=pkgs)
    _, out, _ = guest_exec(g["type"], gid, GUEST_INFO)
    lines = out.strip().splitlines()
    res["reboot_required"] = bool(lines) and lines[0] == "yes"
    if len(lines) > 1 and lines[1].strip():
        res["os"] = lines[1].strip()
    if len(lines) > 2 and lines[2].strip():
        res["os_latest"] = lines[2].strip()
    app = g.get("app")
    if app:
        # Containers tracked before script detection existed: detect once now.
        if g["type"] == "lxc" and app.get("cmd") == "update" and "script" not in app:
            if detect_app(g, gid):
                save_config(cfg)
        res["app"] = app_version(g["type"], gid, app)
    return res


def print_check_line(gid, res, verbose):
    head = f"{gid:<5} {res['name']:<25}"
    if res["state"] == "stopped":
        print(f"{head} {dim('stopped, skipped')}")
        return
    if res["state"] == "error":
        print(f"{head} {bad('check failed: ' + res.get('error', '')[:120])}")
        return
    n = res["packages"]
    parts = [warn(f"{n} packages") if n else ok("OS up to date")]
    if "app" in res:
        parts.append(app_text(res["app"]))
    print(f"{head} " + "  |  ".join(parts))
    if verbose and n:
        print(dim("      " + " ".join(res["package_names"])))


def cmd_check(cfg, args):
    ids = pick_ids(cfg, args.ids)
    if not ids:
        say("No guests tracked yet. Run `pveupdate.py track`.")
        return {}
    lock = acquire_lock()
    st = load_status()
    st["activity"] = "checking"
    save_json(STATUS_PATH, st)
    say("Checking (read-only)...\n")
    results = {}
    for gid in ids:
        res = check_guest(cfg, gid)
        results[gid] = res
        prev = st["guests"].get(gid, {})
        kept = {k: v for k, v in prev.items() if k.startswith("last_") or k == "nosnap_sig"}
        st["guests"][gid] = {**kept, **res}
        if not QUIET:
            print_check_line(gid, res, args.verbose)
    st["checked_at"] = now()
    st.pop("activity", None)
    save_json(STATUS_PATH, st)
    lock.close()
    if getattr(args, "json", False):
        print(json.dumps(status_summary(cfg, st), indent=2))
    return results


# Share of a guest's update each step takes, for the progress percentage.
PROGRESS_BACKUP = (0, 20)
PROGRESS_OS = (20, 80)
PROGRESS_APP = (80, 98)


def apt_counter(total, span, progress, step):
    """on_line callback: moves progress through span as apt downloads,
    unpacks and sets up about `total` packages."""
    lo, hi = span
    seen = [0]
    events = max(total, 1) * 3  # Get:, Unpacking, Setting up

    def on_line(line):
        if line.startswith(("Get:", "Unpacking ", "Setting up ")):
            seen[0] += 1
            progress(lo + (hi - lo) * min(seen[0] / events, 0.99), step)
    return on_line


def percent_counter(span, progress, step):
    """on_line callback for output with "NN%" progress (vzdump)."""
    lo, hi = span

    def on_line(line):
        m = re.search(r"\b(\d{1,3})%", line)
        if m and int(m.group(1)) <= 100:
            progress(lo + (hi - lo) * int(m.group(1)) / 100, step)
    return on_line


def _build_memory_marker(gid):
    return os.path.join(os.path.dirname(STATUS_PATH), f"build-memory-{gid}")


def raise_memory(cfg, gtype, gid):
    """Give an LXC more RAM for its app update, if the host can spare it.
    Returns the original memory (MB) to restore, or None if unchanged."""
    target = setting(cfg, gid, "build_memory") or 0
    if gtype != "lxc" or target <= 0:
        return None
    m = re.search(r"^memory:\s*(\d+)", run(["pct", "config", gid], check=False).stdout, re.M)
    current = int(m.group(1)) if m else 512
    if current >= target:
        return None
    try:
        with open("/proc/meminfo") as f:
            avail = int(re.search(r"MemAvailable:\s*(\d+)", f.read()).group(1)) // 1024
    except (OSError, AttributeError):
        avail = 0
    if avail < target - current + 1024:
        say(dim(f"  host has {avail} MB free, keeping {current} MB for the app update"))
        return None
    if run(["pct", "set", gid, "--memory", str(target)], check=False).returncode != 0:
        return None
    # Remembered on disk too, so an interrupted update is still undone next time.
    with open(_build_memory_marker(gid), "w") as f:
        f.write(str(current))
    say(f"  memory raised to {target} MB for the app update (was {current} MB)")
    log(f"memory {current} -> {target} MB for app update")
    return current


def restore_memory(gid):
    """Put back the RAM raise_memory changed, including after an interrupted run."""
    marker = _build_memory_marker(gid)
    try:
        with open(marker) as f:
            original = int(f.read().strip())
    except (OSError, ValueError):
        return
    if run(["pct", "set", gid, "--memory", str(original)], check=False).returncode == 0:
        os.remove(marker)
        say(f"  memory back to {original} MB")
        log(f"memory restored to {original} MB")


def update_guest(cfg, gid, opts, progress=None, entry=None):
    """Returns (result, detail). result: ok | reboot | failed | skipped.

    progress(percent, step) is called as the update moves along. entry is the
    guest's status entry; it remembers guests whose storage can't snapshot.
    """
    entry = {} if entry is None else entry
    g = cfg["guests"][gid]
    gtype = g["type"]
    interactive = not opts.non_interactive
    progress = progress or (lambda percent, step: None)
    say(f"\n== {gid} {g['name']} ==")
    log(f"\n== {now()} {gid} {g['name']} ==")
    if not is_running(gtype, gid):
        say(dim("  stopped, skipped"))
        return "skipped", "stopped"
    restore_memory(gid)  # left over from an interrupted update

    if setting(cfg, gid, "snapshot") and not opts.no_snapshot:
        sig = disk_signature(gtype, gid)
        if entry.get("nosnap_sig") == sig:
            # Known from an earlier attempt; trying again only adds a failed task in Proxmox.
            snap, err = None, "not supported by this guest's storage"
        else:
            progress(PROGRESS_BACKUP[0], "snapshot")
            snap, err = take_snapshot(gtype, gid)
            if not snap and "snapshot feature is not available" in err:
                entry["nosnap_sig"] = sig
        if snap:
            say(ok(f"  snapshot {snap}"))
            log(f"snapshot {snap}")
            prune_snapshots(gtype, gid, setting(cfg, gid, "keep_snapshots"))
        else:
            say(warn(f"  no snapshot: {err}"))
            log(f"no snapshot: {err}")
            storage, berr = (None, "backup fallback is off")
            if setting(cfg, gid, "backup_fallback"):
                progress(PROGRESS_BACKUP[0], "backup")
                storage, berr = take_backup(cfg, gtype, gid, percent_counter(PROGRESS_BACKUP, progress, "backup"))
            if storage:
                say(ok(f"  backup saved to {storage}"))
                log(f"backup saved to {storage}")
                prune_backups(storage, gid, setting(cfg, gid, "keep_backups"))
            else:
                say(bad(f"  backup failed: {berr}"))
                log(f"backup failed: {berr}")
                if not interactive or not ask("  Continue without a snapshot or backup?", default=False):
                    return "skipped", f"no snapshot ({err}) or backup ({berr})"

    def step(script, env=None, on_line=None):
        live = interactive and gtype == "lxc"
        code, out, err = guest_exec(gtype, gid, script, env=env, interactive=live,
                                    on_line=None if live or gtype != "lxc" else on_line)
        if not live:
            log(out + err)
            if interactive:
                say(out[-3000:] + err[-1000:])
        return code

    say("  updating OS packages...")
    progress(PROGRESS_OS[0], "os")
    npkg = (load_status()["guests"].get(gid) or {}).get("packages") or 1
    code = step(g.get("os_cmd") or OS_UPGRADE, {"AUTOREMOVE": 1 if setting(cfg, gid, "autoremove") else 0},
                apt_counter(npkg, PROGRESS_OS, progress, "os"))
    if code != 0:
        say(bad(f"  OS update failed (exit {code})"))
        return "failed", f"OS update failed (exit {code})"
    say(ok("  OS packages updated"))

    app = g.get("app")
    if app and not opts.no_app:
        say(f"  running app update: {app['cmd']}")
        progress(PROGRESS_APP[0], "app")
        raise_memory(cfg, gtype, gid)
        try:
            # PHS_SILENT=1 makes community-scripts `update` skip its menu.
            code = step(app["cmd"], None if interactive else {"PHS_SILENT": 1})
        finally:
            restore_memory(gid)
        if code != 0:
            say(bad(f"  app update failed (exit {code})"))
            return "failed", f"app update failed (exit {code})"
        say(ok("  app updated"))

    progress(PROGRESS_APP[1], "finishing")
    _, out, _ = guest_exec(gtype, gid, REBOOT_CHECK)
    if out.strip() == "yes":
        say(warn("  reboot required"))
        return "reboot", "reboot required"
    return "ok", ""


def cmd_update(cfg, args):
    ids = args.ids
    if not ids and not args.non_interactive:
        pending = cmd_check(cfg, argparse.Namespace(ids=[], verbose=False, json=False))
        candidates = [gid for gid in sorted(cfg["guests"], key=int)
                      if gid in pending and is_pending(pending[gid])]
        if not candidates:
            say("\nNothing to update.")
            return
        print("\nWhich guests do you want to update? IDs separated by spaces, `all`, or empty to cancel.")
        sel = input("> ").strip()
        if not sel:
            return
        ids = candidates if sel == "all" else sel.replace(",", " ").split()
    elif not ids:
        sys.exit("Give guest IDs, `all` or `pending` when running non-interactively.")
    ids = pick_ids(cfg, ids)
    if not ids:
        say("Nothing to update.")
        return
    names = ", ".join("%s (%s)" % (i, cfg["guests"][i]["name"]) for i in ids)
    say(f"\nWill update: {names}")
    if not (args.yes or args.non_interactive) and not ask("Proceed?"):
        return

    lock = acquire_lock()
    st = load_status()
    results = {}
    try:
        _update_all(cfg, args, ids, st, results)
    finally:
        st.pop("activity", None)
        st.pop("updating", None)
        st.pop("queue", None)
        st.pop("progress", None)
        st.pop("step", None)
        save_json(STATUS_PATH, st)
        lock.close()

    say("\nSummary:")
    for gid, (res, detail) in results.items():
        color = {"ok": ok, "reboot": warn, "failed": bad}.get(res, dim)
        label = {"reboot": "updated, reboot required"}.get(res, res if not detail or res == "ok" else f"{res}: {detail}")
        say(f"  {gid:<5} {cfg['guests'][gid]['name']:<25} {color(label)}")
    if getattr(args, "json", False):
        print(json.dumps(status_summary(cfg, st), indent=2))
    if any(r == "failed" for r, _ in results.values()):
        sys.exit(1)


def _update_all(cfg, args, ids, st, results):
    for i, gid in enumerate(ids):
        # Progress for Home Assistant: which guest is updating, which are queued.
        st.update(activity="updating", updating=gid, queue=ids[i + 1:], progress=0, step="starting")
        save_json(STATUS_PATH, st)
        last = [0.0]

        def progress(percent, step):
            percent = int(percent)
            # Save at most every 2 seconds, or when the step changes.
            if step != st.get("step") or (percent != st.get("progress") and time.monotonic() - last[0] > 2):
                st.update(progress=percent, step=step)
                save_json(STATUS_PATH, st)
                last[0] = time.monotonic()

        entry = st["guests"].setdefault(gid, {"name": cfg["guests"][gid]["name"], "type": cfg["guests"][gid]["type"]})
        result, detail = update_guest(cfg, gid, args, progress, entry)
        results[gid] = (result, detail)
        entry.update(last_update=now(), last_result=result, last_detail=detail,
                     reboot_required=(result == "reboot"))
        if result in ("ok", "reboot"):
            entry.update(check_guest(cfg, gid))  # refresh pending counts
        save_json(STATUS_PATH, st)
        log(f"result: {result} {detail}")


def status_summary(cfg, st, full=False):
    """Compact status for Home Assistant and scripts."""
    guests = {}
    for gid in sorted(cfg["guests"], key=int):
        e = st["guests"].get(gid, {})
        app = e.get("app") or {}
        guests[gid] = {
            "name": cfg["guests"][gid]["name"],
            "type": cfg["guests"][gid]["type"],
            "state": e.get("state", "unchecked"),
            "os": e.get("os"),
            "os_latest": e.get("os_latest"),
            "packages": e.get("packages", 0),
            "app": app.get("label"),
            "app_installed": app.get("installed"),
            "app_latest": app.get("latest"),
            "app_update": bool(app.get("update_available")),
            "pending": is_pending(e),
            "checked_at": e.get("checked_at"),
            "last_update": e.get("last_update"),
            "last_result": e.get("last_result"),
            "last_detail": e.get("last_detail"),
            "reboot_required": bool(e.get("reboot_required")),
        }
        if e.get("error"):
            guests[gid]["error"] = e["error"]
        if full:
            guests[gid]["package_names"] = e.get("package_names", [])
    running = is_locked()
    return {
        "version": VERSION,
        "checked_at": st.get("checked_at"),
        "running": running,
        "activity": st.get("activity") if running else None,
        "updating": st.get("updating") if running else None,
        "queue": st.get("queue", []) if running else [],
        # Progress of the guest being updated: percent and step
        # (snapshot, backup, os, app, finishing).
        "progress": st.get("progress") if running and st.get("updating") else None,
        "step": st.get("step") if running and st.get("updating") else None,
        "pending_guests": sum(1 for g in guests.values() if g["pending"]),
        "pending_ids": " ".join(gid for gid, g in guests.items() if g["pending"]),
        "guests": guests,
    }


def cmd_status(cfg, args):
    summary = status_summary(cfg, load_status(), full=args.verbose)
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    print(f"Last check: {summary['checked_at'] or 'never'}" + ("  (run in progress)" if summary["running"] else ""))
    for gid, g in summary["guests"].items():
        app = ""
        if g["app_installed"]:
            app = f"  |  {g['app']} {g['app_installed']}" + (f" -> {g['app_latest']}" if g["app_update"] else "")
        last = f"  |  last update {g['last_result']} {g['last_update']}" if g["last_update"] else ""
        print(f"  {gid:<5} {g['name']:<25} {g['state']:<9} {g['packages']:>4} pkgs{app}{last}")


def cmd_set(cfg, args):
    if args.id == "default":
        g = cfg["defaults"]  # applies to every guest without its own setting
    elif args.id in cfg["guests"]:
        g = cfg["guests"][args.id]
    else:
        sys.exit(f"{args.id} is not tracked (run `track` first)")
    if args.backup_fallback is not None:
        g["backup_fallback"] = args.backup_fallback == "on"
    if args.backup_storage is not None:
        g["backup_storage"] = args.backup_storage or None
    if args.keep_backups is not None:
        g["keep_backups"] = args.keep_backups
    if args.build_memory is not None:
        g["build_memory"] = args.build_memory
    if args.snapshot is not None:
        g["snapshot"] = args.snapshot == "on"
    if args.keep is not None:
        g["keep_snapshots"] = args.keep
    if args.os_cmd is not None:
        if args.os_cmd:
            g["os_cmd"] = args.os_cmd
        else:
            g.pop("os_cmd", None)
    if args.app_cmd is not None:
        if args.app_cmd:
            g.setdefault("app", {})["cmd"] = args.app_cmd
        else:
            g.pop("app", None)
    if "app" in g and args.id != "default":
        for key in ("version_cmd", "github", "preset"):
            val = getattr(args, key)
            if val is not None:
                g["app"][key] = val
    save_config(cfg)
    print(json.dumps({args.id: g}, indent=2))


# ---------------------------------------------------------------- HTTP API

def load_token(create=False):
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    if not create:
        return None
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def spawn(*cli_args):
    """Run pveupdate in the background, detached from the API server."""
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), *cli_args],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class ApiHandler(BaseHTTPRequestHandler):
    token = ""
    start_lock = threading.Lock()
    last_start = 0.0
    server_version = "pveupdate"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else ""
        if given and hmac.compare_digest(given, self.token):
            return True
        self._send(401, {"error": "invalid or missing token"})
        return False

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(min(n, 65536)))
        except ValueError:
            return None

    def do_GET(self):
        if not self._authorized():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            self._send(200, status_summary(load_config(), load_status(), full=True))
        elif path == "/api/log":
            try:
                with open(LOG_PATH) as f:
                    lines = f.readlines()[-200:]
            except OSError:
                lines = []
            self._send(200, {"log": "".join(lines)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        path = self.path.split("?", 1)[0]
        if path not in ("/api/check", "/api/update"):
            self._send(404, {"error": "not found"})
            return
        with ApiHandler.start_lock:
            # The started process takes the run lock a moment later, so also
            # refuse a second start within a few seconds of the last one.
            if is_locked() or time.monotonic() - ApiHandler.last_start < 5:
                self._send(409, {"error": "a check or update is already running"})
                return
            self._start(path)

    def _start(self, path):
        if path == "/api/check":
            spawn("check", "--json")
            ApiHandler.last_start = time.monotonic()
            self._send(202, {"started": "check"})
            return
        body = self._body()
        guests = (body or {}).get("guests")
        cfg = load_config()
        if guests in ("all", "pending"):
            ids = [guests]
        elif isinstance(guests, list) and guests and all(str(g) in cfg["guests"] for g in guests):
            ids = [str(g) for g in guests]
        else:
            self._send(400, {"error": "guests must be 'all', 'pending' or a list of tracked guest IDs"})
            return
        spawn("update", "--non-interactive", *ids)
        ApiHandler.last_start = time.monotonic()
        self._send(202, {"started": "update", "guests": ids})


def cmd_serve(cfg, args):
    token = load_token(create=True)
    ApiHandler.token = token
    server = ThreadingHTTPServer((args.bind, args.port), ApiHandler)
    print(f"pveupdate API listening on {args.bind}:{args.port} (token in {TOKEN_PATH})", flush=True)
    server.serve_forever()


def cmd_token(cfg, args):
    if args.new and os.path.exists(TOKEN_PATH):
        os.remove(TOKEN_PATH)
    print(load_token(create=True))


def menu(cfg):
    while True:
        print("\n1) Check for updates   2) Update guests   3) Choose tracked guests   4) List tracked   q) Quit")
        choice = input("> ").strip().lower()
        if choice == "1":
            cmd_check(cfg, argparse.Namespace(ids=[], verbose=True, json=False))
        elif choice == "2":
            cmd_update(cfg, argparse.Namespace(ids=[], yes=False, no_snapshot=False, no_app=False,
                                               non_interactive=False, json=False))
        elif choice == "3":
            cmd_track(cfg, None)
        elif choice == "4":
            cmd_list(cfg, None)
        elif choice in ("q", "quit", ""):
            return


def main():
    global QUIET
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")
    pt = sub.add_parser("track", help="choose which guests to track (or give IDs to add)")
    pt.add_argument("ids", nargs="*")
    pun = sub.add_parser("untrack", help="stop tracking guests")
    pun.add_argument("ids", nargs="+")
    sub.add_parser("list", help="show tracked guests")
    pc = sub.add_parser("check", help="show pending updates (read-only)")
    pc.add_argument("ids", nargs="*")
    pc.add_argument("-v", "--verbose", action="store_true", help="list package names")
    pc.add_argument("--json", action="store_true", help="print status JSON instead of text")
    pu = sub.add_parser("update", help="update guests")
    pu.add_argument("ids", nargs="*", help="guest IDs, `all`, or `pending` (from the last check)")
    pu.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    pu.add_argument("--non-interactive", action="store_true",
                    help="no prompts, output to the log; for remote/Home Assistant use")
    pu.add_argument("--no-snapshot", action="store_true")
    pu.add_argument("--no-app", action="store_true", help="only OS packages")
    pu.add_argument("--json", action="store_true", help="print status JSON instead of text")
    psv = sub.add_parser("serve", help="run the HTTP API for the Home Assistant integration")
    psv.add_argument("--bind", default="0.0.0.0")
    psv.add_argument("--port", type=int, default=8765)
    ptk = sub.add_parser("token", help="print the API token (created on first use)")
    ptk.add_argument("--new", action="store_true", help="replace the token with a new one")
    pst = sub.add_parser("status", help="show the last check/update results")
    pst.add_argument("--json", action="store_true")
    pst.add_argument("-v", "--verbose", action="store_true", help="include package names")
    ps = sub.add_parser("set", help="change a tracked guest's settings")
    ps.add_argument("id", help="guest ID, or `default` for all guests")
    ps.add_argument("--snapshot", choices=["on", "off"])
    ps.add_argument("--backup-fallback", choices=["on", "off"],
                    help="take a vzdump backup when a snapshot isn't possible (default on)")
    ps.add_argument("--backup-storage", help="storage for fallback backups ('' = auto)")
    ps.add_argument("--keep-backups", type=int, help="how many pveupdate backups to keep (default 1)")
    ps.add_argument("--build-memory", type=int, metavar="MB",
                    help="RAM an LXC gets during its app update (default 2048, 0 = don't change)")
    ps.add_argument("--keep", type=int, help="how many pveupdate snapshots to keep")
    ps.add_argument("--os-cmd", help="replace the OS update command ('' to reset)")
    ps.add_argument("--app-cmd", help="app update command run after OS packages ('' to remove)")
    ps.add_argument("--version-cmd", help="command printing the installed app version")
    ps.add_argument("--github", help="owner/repo to compare the app version against")
    ps.add_argument("--preset", help=f"known app preset ({', '.join(APP_PRESETS)})")
    args = p.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run as root on the Proxmox host.")
    QUIET = bool(getattr(args, "json", False) or getattr(args, "non_interactive", False))
    cfg = load_config()
    handlers = {"track": cmd_track, "untrack": cmd_untrack, "list": cmd_list, "check": cmd_check, "update": cmd_update,
                "status": cmd_status, "set": cmd_set, "serve": cmd_serve, "token": cmd_token}
    try:
        if args.command:
            handlers[args.command](cfg, args)
        else:
            menu(cfg)
    except KeyboardInterrupt:
        print("\nAborted.")


if __name__ == "__main__":
    main()
