#!/usr/bin/env python3
"""pveupdate - check and update selected Proxmox LXCs and VMs on demand.

Runs on the Proxmox host as root. Nothing happens automatically: you choose
which guests to track, check them for pending updates, and update the ones
you pick. Each update can take a snapshot first and can run an app-specific
update step (community-scripts `update`, or your own command) after the OS
packages.

Usage:
  pveupdate.py                 interactive menu
  pveupdate.py track           choose which guests to track
  pveupdate.py list            show tracked guests and their settings
  pveupdate.py check [ID ...]  show pending OS and app updates (changes nothing)
  pveupdate.py update [ID ...] snapshot + update the given guests (asks if no IDs)
  pveupdate.py set ID [options] change a guest's settings (see `set --help`)
"""

import argparse
import datetime as dt
import json
import os
import shlex
import socket
import subprocess
import sys
import urllib.request

CONFIG_PATH = os.environ.get("PVEUPDATE_CONFIG", "/etc/pveupdate.json")
SNAP_PREFIX = "pveupd"
EXEC_TIMEOUT = 3600

DEFAULTS = {"snapshot": True, "keep_snapshots": 3, "autoremove": True}

# Known apps: how to read the installed version and where releases live.
# Used for the "app update available" hint during `check`.
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

DETECT_APP = r"""
if [ -x /usr/bin/update ]; then
  echo "community $(grep -o 'ct/[A-Za-z0-9_.-]*\.sh' /usr/bin/update | head -n1 | sed 's#ct/##; s#\.sh##')"
fi
"""


# ---------------------------------------------------------------- helpers

def c(text, code):
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def ok(t): return c(t, "32")
def warn(t): return c(t, "33")
def bad(t): return c(t, "31")
def dim(t): return c(t, "2")


def run(cmd, check=True):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def ask(prompt, default=True):
    suffix = " [Y/n] " if default else " [y/N] "
    ans = input(prompt + suffix).strip().lower()
    return default if not ans else ans.startswith("y")


# ---------------------------------------------------------------- config

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {"defaults": dict(DEFAULTS), "guests": {}}
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault("defaults", {})
    for k, v in DEFAULTS.items():
        cfg["defaults"].setdefault(k, v)
    cfg.setdefault("guests", {})
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


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
    """Run a shell script inside a guest. Returns (exit_code, stdout, stderr)."""
    if env:
        script = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items()) + script
    if gtype == "lxc":
        cmd = ["pct", "exec", gid, "--", "sh", "-c", script]
        if interactive:
            return subprocess.call(cmd), "", ""
        r = run(cmd, check=False)
        return r.returncode, r.stdout, r.stderr
    # VM via QEMU guest agent (non-interactive only)
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
        print(dim(f"    removed old snapshot {name}"))


# ---------------------------------------------------------------- app version

def latest_github_release(repo):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "pveupdate"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)["tag_name"].lstrip("v")
    except Exception:
        return None


def app_version_status(gtype, gid, app):
    """Returns a short text like 'zigbee2mqtt 2.1.0 -> 2.2.1', or None."""
    preset = APP_PRESETS.get(app.get("preset", ""), {})
    version_cmd = app.get("version_cmd") or preset.get("version_cmd")
    repo = app.get("github") or preset.get("github")
    if not (version_cmd and repo):
        return None
    code, out, _ = guest_exec(gtype, gid, version_cmd)
    installed = out.strip().lstrip("v") if code == 0 else ""
    latest = latest_github_release(repo)
    label = app.get("preset") or repo.split("/")[-1]
    if not installed or not latest:
        return dim(f"{label}: version unknown")
    if installed == latest:
        return ok(f"{label} {installed} (latest)")
    return warn(f"{label} {installed} -> {latest} available")


# ---------------------------------------------------------------- commands

def cmd_track(cfg, args):
    guests = list_guests()
    if not guests:
        print("No guests found on this node.")
        return
    print("Guests on this node (* = tracked):\n")
    ids = list(guests)
    for i, gid in enumerate(ids, 1):
        g = guests[gid]
        mark = "*" if gid in cfg["guests"] else " "
        print(f"  {i:>2}. [{mark}] {gid:<5} {g['type']:<4} {g['name']:<25} {dim(g['status'])}")
    print("\nEnter numbers to toggle (e.g. `1 3 5`), `all`, or empty to keep as is.")
    sel = input("> ").strip()
    if not sel:
        return
    picks = ids if sel == "all" else [ids[int(x) - 1] for x in sel.replace(",", " ").split()]
    for gid in picks:
        g = guests[gid]
        if gid in cfg["guests"] and sel != "all":
            del cfg["guests"][gid]
            print(f"  untracked {gid} {g['name']}")
            continue
        entry = cfg["guests"].setdefault(gid, {"type": g["type"], "name": g["name"]})
        print(f"  tracking {gid} {g['name']}")
        if g["type"] == "lxc" and g["status"] == "running" and "app" not in entry:
            detect_app(entry, gid)
    save_config(cfg)
    print(f"\nSaved to {CONFIG_PATH}")


def detect_app(entry, gid):
    code, out, _ = guest_exec("lxc", gid, DETECT_APP)
    out = out.strip()
    if code == 0 and out.startswith("community"):
        name = out.split(" ", 1)[1] if " " in out else ""
        entry["app"] = {"cmd": "update", "interactive": True}
        if name in APP_PRESETS:
            entry["app"]["preset"] = name
        print(f"    found community-scripts app{(' ' + name) if name else ''}: will run `update` after OS packages")


def cmd_list(cfg, args):
    if not cfg["guests"]:
        print("No guests tracked yet. Run `pveupdate.py track`.")
        return
    for gid, g in sorted(cfg["guests"].items(), key=lambda kv: int(kv[0])):
        app = g.get("app")
        app_txt = f"app: `{app['cmd']}`{' (interactive)' if app.get('interactive') else ''}" if app else "no app step"
        snap = "snapshot" if setting(cfg, gid, "snapshot") else "no snapshot"
        print(f"  {gid:<5} {g['type']:<4} {g['name']:<25} {snap:<12} {app_txt}")


def pick_ids(cfg, ids):
    if not ids:
        return sorted(cfg["guests"], key=int)
    unknown = [i for i in ids if i not in cfg["guests"]]
    if unknown:
        sys.exit(f"Not tracked: {', '.join(unknown)} (run `track` first)")
    return ids


def check_guest(cfg, gid):
    g = cfg["guests"][gid]
    head = f"{gid:<5} {g['name']:<25}"
    if not is_running(g["type"], gid):
        print(f"{head} {dim('stopped, skipped')}")
        return None
    code, out, err = guest_exec(g["type"], gid, OS_CHECK)
    if code != 0:
        print(f"{head} {bad('check failed: ' + (out + err).strip()[:120])}")
        return None
    pkgs = [p for p in out.split() if p]
    os_txt = warn(f"{len(pkgs)} packages") if pkgs else ok("OS up to date")
    parts = [os_txt]
    if g.get("app"):
        v = app_version_status(g["type"], gid, g["app"])
        parts.append(v if v else dim("app: run update to check"))
    print(f"{head} " + "  |  ".join(parts))
    return pkgs


def cmd_check(cfg, args):
    ids = pick_ids(cfg, args.ids)
    if not ids:
        print("No guests tracked yet. Run `pveupdate.py track`.")
        return {}
    print("Checking (read-only)...\n")
    results = {}
    for gid in ids:
        pkgs = check_guest(cfg, gid)
        if pkgs is not None:
            results[gid] = pkgs
            if args.verbose and pkgs:
                print(dim("      " + " ".join(pkgs)))
    return results


def update_guest(cfg, gid, skip_snapshot=False, skip_app=False):
    g = cfg["guests"][gid]
    gtype = g["type"]
    print(f"\n== {gid} {g['name']} ==")
    if not is_running(gtype, gid):
        print(dim("  stopped, skipped"))
        return "skipped"

    if setting(cfg, gid, "snapshot") and not skip_snapshot:
        snap, err = take_snapshot(gtype, gid)
        if snap:
            print(ok(f"  snapshot {snap}"))
            prune_snapshots(gtype, gid, setting(cfg, gid, "keep_snapshots"))
        else:
            print(bad(f"  snapshot failed: {err}"))
            if not ask("  Continue without a snapshot?", default=False):
                return "skipped"

    os_cmd = g.get("os_cmd") or OS_UPGRADE
    print("  updating OS packages...")
    env = {"AUTOREMOVE": 1 if setting(cfg, gid, "autoremove") else 0}
    if gtype == "lxc":
        code, _, _ = guest_exec(gtype, gid, os_cmd, env=env, interactive=True)
    else:
        code, out, err = guest_exec(gtype, gid, os_cmd, env=env)
        print(out[-3000:] + err[-1000:])
    if code != 0:
        print(bad(f"  OS update failed (exit {code})"))
        return "failed"
    print(ok("  OS packages updated"))

    app = g.get("app")
    if app and not skip_app:
        print(f"  running app update: {app['cmd']}")
        if gtype == "lxc":
            # Output streams to your terminal, so prompts (community-scripts) can be answered.
            code, _, _ = guest_exec(gtype, gid, app["cmd"], interactive=True)
        else:
            code, out, err = guest_exec(gtype, gid, app["cmd"])
            print(out[-3000:] + err[-1000:])
        if code != 0:
            print(bad(f"  app update failed (exit {code})"))
            return "failed"
        print(ok("  app updated"))

    _, out, _ = guest_exec(gtype, gid, REBOOT_CHECK)
    if out.strip() == "yes":
        print(warn("  reboot required"))
        return "reboot"
    return "ok"


def cmd_update(cfg, args):
    ids = args.ids
    if not ids:
        pending = cmd_check(cfg, argparse.Namespace(ids=[], verbose=False))
        candidates = [gid for gid in sorted(cfg["guests"], key=int) if gid in pending]
        if not candidates:
            return
        print("\nWhich guests do you want to update? IDs separated by spaces, `all`, or empty to cancel.")
        sel = input("> ").strip()
        if not sel:
            return
        ids = candidates if sel == "all" else sel.replace(",", " ").split()
    ids = pick_ids(cfg, ids)
    names = ", ".join("%s (%s)" % (i, cfg["guests"][i]["name"]) for i in ids)
    print(f"\nWill update: {names}")
    if not args.yes and not ask("Proceed?"):
        return
    results = {gid: update_guest(cfg, gid, args.no_snapshot, args.no_app) for gid in ids}
    print("\nSummary:")
    for gid, res in results.items():
        color = {"ok": ok, "reboot": warn, "failed": bad}.get(res, dim)
        label = {"reboot": "updated, reboot required"}.get(res, res)
        print(f"  {gid:<5} {cfg['guests'][gid]['name']:<25} {color(label)}")


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
        for key in ("interactive", "version_cmd", "github", "preset"):
            val = getattr(args, key)
            if val is not None:
                g["app"][key] = (val == "on") if key == "interactive" else val
    save_config(cfg)
    print(json.dumps({args.id: g}, indent=2))


def menu(cfg):
    while True:
        print("\n1) Check for updates   2) Update guests   3) Choose tracked guests   4) List tracked   q) Quit")
        choice = input("> ").strip().lower()
        if choice == "1":
            cmd_check(cfg, argparse.Namespace(ids=[], verbose=True))
        elif choice == "2":
            cmd_update(cfg, argparse.Namespace(ids=[], yes=False, no_snapshot=False, no_app=False))
        elif choice == "3":
            cmd_track(cfg, None)
        elif choice == "4":
            cmd_list(cfg, None)
        elif choice in ("q", "quit", ""):
            return


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")
    sub.add_parser("track", help="choose which guests to track")
    sub.add_parser("list", help="show tracked guests")
    pc = sub.add_parser("check", help="show pending updates (read-only)")
    pc.add_argument("ids", nargs="*")
    pc.add_argument("-v", "--verbose", action="store_true", help="list package names")
    pu = sub.add_parser("update", help="update guests")
    pu.add_argument("ids", nargs="*")
    pu.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    pu.add_argument("--no-snapshot", action="store_true")
    pu.add_argument("--no-app", action="store_true", help="only OS packages")
    ps = sub.add_parser("set", help="change a tracked guest's settings")
    ps.add_argument("id")
    ps.add_argument("--snapshot", choices=["on", "off"])
    ps.add_argument("--keep", type=int, help="how many pveupdate snapshots to keep")
    ps.add_argument("--os-cmd", help="replace the OS update command ('' to reset)")
    ps.add_argument("--app-cmd", help="app update command run after OS packages ('' to remove)")
    ps.add_argument("--interactive", choices=["on", "off"], help="run app command in your terminal")
    ps.add_argument("--version-cmd", help="command printing the installed app version")
    ps.add_argument("--github", help="owner/repo to compare the app version against")
    ps.add_argument("--preset", help=f"known app preset ({', '.join(APP_PRESETS)})")
    args = p.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run as root on the Proxmox host.")
    cfg = load_config()
    handlers = {"track": cmd_track, "list": cmd_list, "check": cmd_check, "update": cmd_update, "set": cmd_set}
    try:
        if args.command:
            handlers[args.command](cfg, args)
        else:
            menu(cfg)
    except KeyboardInterrupt:
        print("\nAborted.")


if __name__ == "__main__":
    main()
