# pveupdate

Check and update selected Proxmox LXCs and VMs, only when you ask. Runs on the Proxmox host as root, Python 3 standard library only. Run it from the host's shell or from Home Assistant.

## Install

```bash
# on the Proxmox host
curl -fsSL https://raw.githubusercontent.com/Niiikoc/Pveupdate/main/pveupdate.py -o /usr/local/bin/pveupdate
chmod +x /usr/local/bin/pveupdate
pveupdate track      # pick the guests to manage
```

Run the same `curl` again to update pveupdate itself.

## Use

```bash
pveupdate              # menu: check / update / track / list
pveupdate check -v     # pending OS packages + app versions, changes nothing
pveupdate update       # check, then asks which guests to update
pveupdate update 101 105
pveupdate update pending   # every guest the last check found updates for
pveupdate status       # results of the last check and update, without re-checking
pveupdate serve        # HTTP API for the Home Assistant integration (see below)
```

Adding and removing guests:

```bash
pveupdate track        # list all guests (* = tracked), type numbers to toggle
pveupdate track 108    # start tracking a new guest by ID
pveupdate untrack 108  # stop tracking it
```

Guests you delete from Proxmox are untracked automatically the next time you run `track`.

Each update does, per guest:

1. Snapshot `pveupd-<date>` (keeps the last 3 by default). If the guest can't be snapshotted (directory storage, bind mounts), a `vzdump` backup is taken instead, to your backup storage (Proxmox Backup Server if you have one, otherwise the first active backup storage). Only the newest pveupdate backup per guest is kept; your other backups are never touched. If neither works, the guest is skipped.
2. OS packages: `apt-get update && apt-get dist-upgrade` (keeps your config files), then `autoremove`. Alpine uses `apk upgrade`.
3. App step, if configured. Output goes to your terminal so you can answer prompts.
4. Reports whether a reboot is needed.

Stopped guests are skipped. VMs need the QEMU guest agent installed and enabled. Only one check or update runs at a time.

## App updates

- **community-scripts containers** are detected when you track them (they have `/usr/bin/update`), and `update` is run after the OS packages. `check` shows the installed version (from `/root/.<app>`) and, for apps released on GitHub, the latest one.
- **Your own installs**: save the command once:

```bash
pveupdate set 104 --app-cmd 'systemctl stop myapp && cd /opt/myapp && git pull && npm ci && systemctl start myapp'
```

- **Version hint for your own installs**: give a command printing the installed version and the GitHub repo:

```bash
pveupdate set 104 --version-cmd 'cat /opt/myapp/VERSION' --github owner/myapp
```

MariaDB, InfluxDB and other apt-installed apps are covered by the OS step.

## Home Assistant

Use the **[Proxmox Guest Updates](https://github.com/Niiikoc/ha-pveupdate)** integration (installable through HACS). Each tracked guest shows up as a Home Assistant update entity with an Install button. It talks to `pveupdate serve`, a small token-protected API on the host:

```bash
base=https://raw.githubusercontent.com/Niiikoc/Pveupdate/main
curl -fsSL $base/systemd/pveupdate-serve.service -o /etc/systemd/system/pveupdate-serve.service
systemctl daemon-reload && systemctl enable --now pveupdate-serve
pveupdate token      # enter this in the integration
```

The API (port 8765) accepts only: read status, start a check, update tracked guests, read the log. Every request needs the token. Keep the port on your LAN, and replace the token with `pveupdate token --new`.

To also check for updates every 6 hours (read-only; updates still only happen when you press Install):

```bash
curl -fsSL $base/systemd/pveupdate-check.service -o /etc/systemd/system/pveupdate-check.service
curl -fsSL $base/systemd/pveupdate-check.timer -o /etc/systemd/system/pveupdate-check.timer
systemctl daemon-reload && systemctl enable --now pveupdate-check.timer
```

Updates started from Home Assistant run in the background without prompts. Their output goes to `/var/log/pveupdate.log` on the host. If neither a snapshot nor a backup works, that guest is skipped instead of asking.

## Other settings

```bash
pveupdate set 102 --snapshot off     # skip snapshot and backup for this guest
pveupdate set default --backup-storage pbs   # where fallback backups go (default: auto)
pveupdate set 140 --keep-backups 2   # keep more pveupdate backups for one guest
pveupdate set 140 --backup-fallback off
pveupdate set 102 --keep 5
pveupdate set 102 --os-cmd '...'     # replace the OS step; '' resets it
pveupdate set 104 --app-cmd ''       # remove the app step
```

Files on the host:

| Path | What |
|---|---|
| `/etc/pveupdate.json` | tracked guests and settings |
| `/var/lib/pveupdate/status.json` | last check and update results |
| `/var/log/pveupdate.log` | output of updates run without a terminal |
| `/etc/pveupdate.token` | API token for `pveupdate serve` |
