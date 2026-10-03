# pveupdate

Check and update selected Proxmox LXCs and VMs, only when you ask. Runs on the Proxmox host as root, Python 3 standard library only.

## Install

```bash
# on the Proxmox host
cp pveupdate.py /usr/local/bin/pveupdate && chmod +x /usr/local/bin/pveupdate
pveupdate track      # pick the guests to manage
```

## Use

```bash
pveupdate            # menu: check / update / track / list
pveupdate check -v   # pending OS packages + app versions, changes nothing
pveupdate update     # check, then asks which guests to update
pveupdate update 101 105
```

Each update does, per guest:

1. Snapshot `pveupd-<date>` (keeps the last 3 by default; asks before continuing if the storage can't snapshot).
2. OS packages: `apt-get update && apt-get dist-upgrade` (keeps your config files), then `autoremove`. Alpine uses `apk upgrade`.
3. App step, if configured. Output goes to your terminal so you can answer prompts.
4. Reports whether a reboot is needed.

Stopped guests are skipped. VMs need the QEMU guest agent installed and enabled.

## App updates

- **community-scripts containers** are detected when you track them (they have `/usr/bin/update`), and `update` is run after the OS packages.
- **Your own installs**: save the command once:

```bash
pveupdate set 104 --app-cmd 'systemctl stop myapp && cd /opt/myapp && git pull && npm ci && systemctl start myapp'
```

- **Version hint in `check`**: give a command printing the installed version and the GitHub repo, or use a preset:

```bash
pveupdate set 101 --preset zigbee2mqtt
pveupdate set 104 --version-cmd 'cat /opt/myapp/VERSION' --github owner/myapp
```

MariaDB and other apt-installed apps are covered by the OS step.

## Other settings

```bash
pveupdate set 102 --snapshot off     # e.g. storage without snapshot support
pveupdate set 102 --keep 5
pveupdate set 102 --os-cmd '...'     # replace the OS step; '' resets it
pveupdate set 104 --app-cmd ''       # remove the app step
```

Config lives in `/etc/pveupdate.json` (override with `PVEUPDATE_CONFIG`).
