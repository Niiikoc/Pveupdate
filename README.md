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
```

Each update does, per guest:

1. Snapshot `pveupd-<date>` (keeps the last 3 by default; asks before continuing if the storage can't snapshot).
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

Home Assistant can show what's pending, notify you, and start updates with a button. It talks to the Proxmox host over SSH with a key that can **only** run pveupdate (`status`, `check`, `update`, `log`), nothing else.

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

Updates started from Home Assistant run in the background without prompts. Their output goes to `/var/log/pveupdate.log` on the host (also readable with `ssh ... log`). If a snapshot fails, that guest is skipped instead of asking.

**Why not GitHub Actions?** GitHub's runners are on the internet and can't reach your Proxmox host unless you expose SSH publicly or run a self-hosted runner on your network. Home Assistant is already inside your network, so it's the safer trigger.

## Other settings

```bash
pveupdate set 102 --snapshot off     # e.g. storage without snapshot support
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
