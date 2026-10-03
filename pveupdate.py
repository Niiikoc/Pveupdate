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
  pveupdate.py set ID [options] change a guest's settings (see `set --help`)

For Home Assistant / remote use see pveupdate-remote and the README.
"""

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import urllib.request

CONFIG_PATH = os.environ.get("PVEUPDATE_CONFIG", "/etc/pveupdate.json")
STATUS_PATH = os.environ.get("PVEUPDATE_STATUS", "/var/lib/pveupdate/status.json")
LOG_PATH = os.environ.get("PVEUPDATE_LOG", "/var/log/pveupdate.log")
LOCK_PATH = os.environ.get("PVEUPDATE_LOCK", "/run/pveupdate.lock")
SNAP_PREFIX = "pveupd"
EXEC_TIMEOUT = 3600

DEFAULTS = {"snapshot": True, "keep_snapshots": 3, "autoremove": True}

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


def guest_exec(gtype, gid, script, env=None, interactive=False):
    """Run a shell script inside a guest. Returns (exit_code, stdout, stderr).

    interactive=True (LXC only) connects the command to your terminal so its
    output streams live and prompts can be answered.
    """
    if env:
        script = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items()) + script
    if gtype == "lxc":
        cmd = ["pct", "exec", gid, "--", "sh", "-c", script]
        if interactive:
            return subprocess.call(cmd), "", ""
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
    _, out, _ = guest_exec(g["type"], gid, REBOOT_CHECK)
    res["reboot_required"] = out.strip() == "yes"
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
    say("Checking (read-only)...\n")
    results = {}
    for gid in ids:
        res = check_guest(cfg, gid)
        results[gid] = res
        prev = st["guests"].get(gid, {})
        st["guests"][gid] = {**{k: v for k, v in prev.items() if k.startswith("last_")}, **res}
        if not QUIET:
            print_check_line(gid, res, args.verbose)
    st["checked_at"] = now()
    save_json(STATUS_PATH, st)
    lock.close()
    if getattr(args, "json", False):
        print(json.dumps(status_summary(cfg, st), indent=2))
    return results


def update_guest(cfg, gid, opts):
    """Returns (result, detail). result: ok | reboot | failed | skipped."""
    g = cfg["guests"][gid]
    gtype = g["type"]
    interactive = not opts.non_interactive
    say(f"\n== {gid} {g['name']} ==")
    log(f"\n== {now()} {gid} {g['name']} ==")
    if not is_running(gtype, gid):
        say(dim("  stopped, skipped"))
        return "skipped", "stopped"

    if setting(cfg, gid, "snapshot") and not opts.no_snapshot:
        snap, err = take_snapshot(gtype, gid)
        if snap:
            say(ok(f"  snapshot {snap}"))
            log(f"snapshot {snap}")
            prune_snapshots(gtype, gid, setting(cfg, gid, "keep_snapshots"))
        else:
            say(bad(f"  snapshot failed: {err}"))
            log(f"snapshot failed: {err}")
            if not interactive or not ask("  Continue without a snapshot?", default=False):
                return "skipped", f"snapshot failed: {err}"

    def step(script, env=None):
        live = interactive and gtype == "lxc"
        code, out, err = guest_exec(gtype, gid, script, env=env, interactive=live)
        if not live:
            log(out + err)
            if interactive:
                say(out[-3000:] + err[-1000:])
        return code

    say("  updating OS packages...")
    code = step(g.get("os_cmd") or OS_UPGRADE, {"AUTOREMOVE": 1 if setting(cfg, gid, "autoremove") else 0})
    if code != 0:
        say(bad(f"  OS update failed (exit {code})"))
        return "failed", f"OS update failed (exit {code})"
    say(ok("  OS packages updated"))

    app = g.get("app")
    if app and not opts.no_app:
        say(f"  running app update: {app['cmd']}")
        # PHS_SILENT=1 makes community-scripts `update` skip its menu.
        code = step(app["cmd"], None if interactive else {"PHS_SILENT": 1})
        if code != 0:
            say(bad(f"  app update failed (exit {code})"))
            return "failed", f"app update failed (exit {code})"
        say(ok("  app updated"))

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
    for gid in ids:
        result, detail = update_guest(cfg, gid, args)
        results[gid] = (result, detail)
        entry = st["guests"].setdefault(gid, {"name": cfg["guests"][gid]["name"], "type": cfg["guests"][gid]["type"]})
        entry.update(last_update=now(), last_result=result, last_detail=detail,
                     reboot_required=(result == "reboot"))
        if result in ("ok", "reboot"):
            entry.update(check_guest(cfg, gid))  # refresh pending counts
        save_json(STATUS_PATH, st)
        log(f"result: {result} {detail}")
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
            "packages": e.get("packages", 0),
            "app": app.get("label"),
            "app_installed": app.get("installed"),
            "app_latest": app.get("latest"),
            "app_update": bool(app.get("update_available")),
            "pending": is_pending(e),
            "checked_at": e.get("checked_at"),
            "last_update": e.get("last_update"),
            "last_result": e.get("last_result"),
            "reboot_required": bool(e.get("reboot_required")),
        }
        if e.get("error"):
            guests[gid]["error"] = e["error"]
        if full:
            guests[gid]["package_names"] = e.get("package_names", [])
    return {
        "checked_at": st.get("checked_at"),
        "running": is_locked(),
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
    if args.id not in cfg["guests"]:
        sys.exit(f"{args.id} is not tracked (run `track` first)")
    g = cfg["guests"][args.id]
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
    if "app" in g:
        for key in ("version_cmd", "github", "preset"):
            val = getattr(args, key)
            if val is not None:
                g["app"][key] = val
    save_config(cfg)
    print(json.dumps({args.id: g}, indent=2))


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
    pst = sub.add_parser("status", help="show the last check/update results")
    pst.add_argument("--json", action="store_true")
    pst.add_argument("-v", "--verbose", action="store_true", help="include package names")
    ps = sub.add_parser("set", help="change a tracked guest's settings")
    ps.add_argument("id")
    ps.add_argument("--snapshot", choices=["on", "off"])
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
                "status": cmd_status, "set": cmd_set}
    try:
        if args.command:
            handlers[args.command](cfg, args)
        else:
            menu(cfg)
    except KeyboardInterrupt:
        print("\nAborted.")


if __name__ == "__main__":
    main()
