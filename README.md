# pveupdate

Check and update selected Proxmox LXCs and VMs, only when you ask. Runs on the Proxmox host as root, Python 3 standard library only. Run it from the host's shell or from Home Assistant.

## Install

```bash
# on the Proxmox host
curl -fsSL https://github.com/Niiikoc/Pveupdate/releases/latest/download/pveupdate.py -o /usr/local/bin/pveupdate
chmod +x /usr/local/bin/pveupdate
pveupdate track      # pick the guests to manage
```

Run the same `curl` again to update pveupdate itself. It always fetches the latest [release](https://github.com/Niiikoc/Pveupdate/releases), never unreleased work in progress.

## Use

```bash
pveupdate              # menu: check / update / track / list
pveupdate check -v     # pending OS packages + app versions, changes nothing
pveupdate update       # check, then asks which guests to update
pveupdate update 101 105
pveupdate update pending   # every guest the last check found updates for
pveupdate update --no-os 105    # only the app update
pveupdate update --no-app 105   # only the OS packages
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

- **community-scripts containers** are detected when you track them (they have `/usr/bin/update`), and `update` is run after the OS packages. `check` shows the installed version (from `/root/.<app>` or an older `/opt/<app>_version.txt`; Pi-hole and AdGuard Home are asked directly) and, for apps released on GitHub, the latest one. Apps installed with apt get both versions from apt, and their app update upgrades only that package instead of running `update`. Any other app without a version file shows as unknown until you give it a `--version-cmd`.
- **Other guests with an app from apt** are detected on `check` from their running services: if one non-system package provides them, or one matches the guest's name, that package is the app and its app update upgrades only that package. When it isn't clear which service is the app, the guest gets no app step; set one with `--app-cmd` and `--version-cmd`. Debian and Ubuntu guests with systemd only. Container runtimes only count when the guest is named after them. An app from apt is also an OS package, so updating the OS upgrades it too. `pveupdate set ID --app-cmd ''` turns the app step off for good.
- **Your own installs**: save the command once:

```bash
pveupdate set 104 --app-cmd 'systemctl stop myapp && cd /opt/myapp && git pull && npm ci && systemctl start myapp'
```

- **Version hint for your own installs**: give a command printing the installed version and the GitHub repo:

```bash
pveupdate set 104 --version-cmd 'cat /opt/myapp/VERSION' --github owner/myapp
```

## Home Assistant

Use the **[Proxmox Guest Updates](https://github.com/Niiikoc/ha-pveupdate)** integration (installable through HACS). Guests with an app show up under Settings > Updates when a new app version is out, and Install runs only the app update. OS packages get an "Update OS" button and a pending-packages sensor on each guest's device page, so they never nag you in Updates. You can choose the tracked guests under the integration's Configure. It talks to `pveupdate serve`, a small token-protected API on the host:

```bash
base=https://github.com/Niiikoc/Pveupdate/releases/latest/download
curl -fsSL $base/pveupdate-serve.service -o /etc/systemd/system/pveupdate-serve.service
systemctl daemon-reload && systemctl enable --now pveupdate-serve
pveupdate token      # enter this in the integration
```

The API (port 8765) accepts only: read status, list the node's guests, choose which guests are tracked, start a check, update tracked guests, read the log. Every request needs the token.

`pveupdate token` prints the token and the fingerprint of the API's self-signed certificate, joined by a dot. Home Assistant only trusts that certificate, so the token can't be read on the network or sent to anything pretending to be your host. Integrations set up with a token alone still connect over plain HTTP; paste the new output under the integration's Reconfigure to move them to HTTPS.

Keeping it locked down:

- Allow port 8765 only from Home Assistant's IP in the Proxmox firewall, and don't port-forward it.
- Replace a leaked token with `pveupdate token --new`. The old one stops working immediately.
- `pveupdate set default --remote-track off` stops the API from changing the tracked guests, so the token only reaches the guests you chose on the host.

Updating from before 0.8.0: run `systemctl restart pveupdate-serve` after the `curl` above, so the API picks up HTTPS.

To also check for updates every 6 hours (read-only; updates still only happen when you press Install):

```bash
curl -fsSL $base/pveupdate-check.service -o /etc/systemd/system/pveupdate-check.service
curl -fsSL $base/pveupdate-check.timer -o /etc/systemd/system/pveupdate-check.timer
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
pveupdate set default --remote-track off   # choose tracked guests only on the host
```

Files on the host:

| Path | What |
|---|---|
| `/etc/pveupdate.json` | tracked guests and settings |
| `/var/lib/pveupdate/status.json` | last check and update results |
| `/var/log/pveupdate.log` | output of updates run without a terminal |
| `/etc/pveupdate.token` | API token for `pveupdate serve` |
| `/etc/pveupdate.crt`, `/etc/pveupdate.key` | the API's self-signed certificate and private key |

## Contributing

Issues and pull requests are welcome. Changes reach `main` only through a pull request that passes the checks and is merged by the maintainer, and users only get them once they're in a release. A release is published automatically when a merge changes `VERSION` in `pveupdate.py`.
