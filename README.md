# pveupdate

Check and update selected Proxmox LXCs and VMs, only when you ask. Runs on the Proxmox host as root, Python 3 standard library only. It can be triggered from the host's shell or remotely from Home Assistant.

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

The recommended way is the **[Proxmox Guest Updates](https://github.com/Niiikoc/ha-pveupdate)** integration (installable through HACS). Each tracked guest shows up as a Home Assistant update entity with an Install button. It talks to `pveupdate serve`, a small token-protected API on the host:

```bash
base=https://raw.githubusercontent.com/Niiikoc/Pveupdate/main
curl -fsSL $base/systemd/pveupdate-serve.service -o /etc/systemd/system/pveupdate-serve.service
systemctl daemon-reload && systemctl enable --now pveupdate-serve
pveupdate token      # enter this in the integration
```

The API (port 8765) accepts only: read status, start a check, update tracked guests, read the log. Every request needs the token. Keep the port on your LAN, and replace the token with `pveupdate token --new`.

### Without the integration (SSH + YAML)

This uses an SSH key that can **only** run pveupdate (`status`, `check`, `update`, `log`), nothing else.

**1. On the Proxmox host**, install the remote wrapper and a timer that checks every 6 hours (checking is read-only; updates still only happen when you press the button):

```bash
base=https://raw.githubusercontent.com/Niiikoc/Pveupdate/main
curl -fsSL $base/pveupdate-remote -o /usr/local/bin/pveupdate-remote && chmod +x /usr/local/bin/pveupdate-remote
curl -fsSL $base/systemd/pveupdate-check.service -o /etc/systemd/system/pveupdate-check.service
curl -fsSL $base/systemd/pveupdate-check.timer -o /etc/systemd/system/pveupdate-check.timer
systemctl daemon-reload && systemctl enable --now pveupdate-check.timer
```

**2. In Home Assistant** (Terminal & SSH add-on), create a key:

```bash
mkdir -p /config/.ssh && ssh-keygen -t ed25519 -N "" -f /config/.ssh/pveupdate -C homeassistant
cat /config/.ssh/pveupdate.pub
```

**3. On the Proxmox host**, allow that key, restricted to the wrapper. Add this as one line to `/root/.ssh/authorized_keys`, with your key after `restrict`:

```
command="/usr/local/bin/pveupdate-remote",restrict ssh-ed25519 AAAA... homeassistant
```

**4. In Home Assistant**, copy [`homeassistant/pveupdate.yaml`](homeassistant/pveupdate.yaml) to `/config/packages/`, replace `192.168.1.10` with your host, enable packages in `configuration.yaml` if you haven't:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Then restart Home Assistant. Test from the HA terminal first (this also saves the host key):

```bash
ssh -i /config/.ssh/pveupdate -o UserKnownHostsFile=/config/.ssh/known_hosts root@192.168.1.10 status
```

You get:

- `sensor.proxmox_updates`: number of guests with pending updates; per-guest details (packages, app versions, last result, reboot needed) as attributes.
- `script.pveupdate_check`, `script.pveupdate_update_pending`, and `script.pveupdate_update_guest` (takes IDs like `101 105`).
- An automation that notifies you when updates are available.
- A dashboard card: [`homeassistant/dashboard-card.yaml`](homeassistant/dashboard-card.yaml) (Add card > Manual).

Updates started from Home Assistant run in the background without prompts. Their output goes to `/var/log/pveupdate.log` on the host (also readable with `ssh ... log`). If neither a snapshot nor a backup works, that guest is skipped instead of asking.

**Why not GitHub Actions?** GitHub's runners are on the internet and can't reach your Proxmox host unless you expose SSH publicly or run a self-hosted runner on your network. Home Assistant is already inside your network, so it's the safer trigger.

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
