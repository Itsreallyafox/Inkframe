# Deploying Inkframe to the Pi

Deploys are a `git push` from the desktop. The Pi keeps a bare repo, and a
post-receive hook checks out `main`, updates what changed, restarts the
service and runs a health check. Nothing is rsynced and nothing is built on
the desktop.

The desktop reaches the Pi as `ssh inkframe` (an alias in `~/.ssh/config`:
host `inkframe.local`, user `inkframe`, key auth). If the Pi's address
changes, edit `HostName` there; every script uses the alias.

Windows blocks unsigned scripts by default, so run the PowerShell scripts as
`powershell -NoProfile -ExecutionPolicy Bypass -File <script> [args]`.
The examples below shorten that to `.\deploy.ps1`.

## Where things live on the Pi

| Path | What |
|---|---|
| `/opt/inkframe/repo.git` | bare repo, the push target (`git remote pi`) |
| `/opt/inkframe/repo.git/hooks/post-receive` | small hook, written by `install.sh` |
| `/opt/inkframe/app` | checkout of `main` (the service's working directory) |
| `/opt/inkframe/.venv` | venv, `--system-site-packages` (spidev, gpiozero and lgpio come from apt) |
| `/opt/inkframe/data` | photos, database, cache, `last_frame.png`; deploys never touch it |
| `/opt/inkframe/last-deploy` | `<sha> running\|ok\|failed <utc time>`, written by the hook |
| `/opt/inkframe/.bootstrap-done` | marker with the time bootstrap last finished |
| `/etc/systemd/system/inkframe.service` | copy of `deploy/inkframe.service` |
| `/etc/sudoers.d/inkframe` | copy of `deploy/sudoers-inkframe` |

The service runs as user `inkframe`, `python -m inkframe` from the venv, on
port 80 (`AmbientCapabilities=CAP_NET_BIND_SERVICE`, no root). The web UI is
`http://inkframe.local/`.

## First install

From the repo root on the desktop, with `main` committed (including
`install.sh` and `deploy/`):

```powershell
.\deploy\bootstrap.ps1             # deploys main
.\deploy\bootstrap.ps1 -Snapshot   # deploys the working tree as it is, uncommitted files included
```

Before it touches the Pi, `bootstrap.ps1` checks that the deploy at its end
can succeed: a clean working tree (unless `-Snapshot` or `-Force`) and a
`main` that contains `install.sh` and `deploy/post-receive-main` (unless
`-Snapshot`). Otherwise it stops in seconds, before the bootstrap and the
reboot.

It copies `install.sh` to `~/install.sh` on the Pi and runs
`bash ~/install.sh --bootstrap`: apt packages, the `spi`/`gpio` groups, SPI,
the `/opt/inkframe` directories, the bare repo and hook, the venv and the
sudoers drop-in. If SPI had to be enabled, bootstrap exits 3, and the script
reboots the Pi, waits up to 180 s for it to come back (a new boot id), and
checks `/dev/spidev0.0`.
Then it runs `.\deploy.ps1 -SkipFrame` (plus `-Snapshot` or `-Force` if
given); that first push installs the systemd unit and starts the service.
Re-running `bootstrap.ps1` is safe: on a bootstrapped Pi it skips the apt
work quickly, does not reboot, and only deploys. If SPI had to be enabled
and the device appeared at once (raspi-config also enables it in the
running kernel), no reboot is needed and none is done.

The same by hand:

```powershell
scp install.sh inkframe:install.sh
ssh inkframe "bash ~/install.sh --bootstrap"   # exit 3 = reboot required
ssh inkframe "sudo reboot"                      # only after exit 3
git remote add pi inkframe:/opt/inkframe/repo.git
.\deploy.ps1
```

## Everyday deploy

Commit, then:

```powershell
.\deploy.ps1               # push main, show /api/status, fetch the last frame
.\deploy.ps1 -SkipFrame    # don't fetch data\pi_last_frame.png
.\deploy.ps1 -Snapshot     # deploy the working tree as it is, without committing
.\deploy.ps1 -Force        # deploy main even with uncommitted changes (they are not pushed)
.\deploy.ps1 -Branch exp   # push local branch exp to the Pi's main
```

`deploy.ps1` adds the `pi` remote if it is missing and refuses a dirty working
tree unless `-Snapshot` or `-Force` is given. It also refuses a commit without
`install.sh` and `deploy/post-receive-main`, because the Pi's hook could not
deploy it (the push would still succeed and leave the Pi's `main` on it). It
pushes, then reads `/opt/inkframe/last-deploy` because `git push` succeeds even
when the hook fails, prints `/api/status`, and copies
`/opt/inkframe/data/last_frame.png` to `.\data\pi_last_frame.png`. If the Pi
already has the commit, nothing is pushed and the hook does not run.
`-Branch` pushes without force, so a branch that has diverged from the Pi's
`main` is rejected; push it by hand with `git push pi +exp:main` if you mean it.

### Snapshots: deploying uncommitted work

`-Snapshot` deploys the working tree exactly as `git add --all` would stage
it (tracked files plus untracked ones that `.gitignore` does not exclude),
without committing anything. It builds the tree in a temporary index under
`.git/`, then a commit object whose parent is `HEAD` and whose message ends
with the trailer `Inkframe-Snapshot: <HEAD sha>`, and pushes that object to
the Pi's `main`. The real index, `HEAD`, the local branches and the git
config are not touched; the snapshot is on no local branch (the push moves
the remote-tracking ref `pi/main` to it, as any push does). The hook treats
it like any other push.

Because a snapshot is on no branch, nothing will ever descend from it.
Whenever the Pi's `main` is a snapshot (`deploy.ps1` reads the trailer on the
Pi), the next deploy, of a commit or of a newer snapshot, replaces it with a
force push; a commit on the Pi's `main` is only ever fast-forwarded. Running
`-Snapshot` again with an unchanged working tree pushes nothing, and a
working tree that matches `HEAD` deploys `HEAD` itself. The usual sequence is
`.\deploy.ps1 -Snapshot` while iterating, then commit and `.\deploy.ps1`,
which puts the real commit on the Pi (usually a restart only, because its
files match the snapshot).

## What the hook does

The bare repo's `hooks/post-receive` is a small, stable script embedded in
`install.sh`. It ignores every ref except `refs/heads/main`, takes a lock so
only one deploy runs at a time, runs
`git --git-dir=/opt/inkframe/repo.git --work-tree=/opt/inkframe/app checkout -f main`,
then runs `bash /opt/inkframe/app/deploy/post-receive-main OLD NEW`. That
second script ships with the code, so deploy logic changes take effect on the
push that carries them.

`post-receive-main` decides between two paths:

- **Full update**, when this is the first push, the unit is not installed
  yet, the previous deploy did not finish `ok`, or the push changes
  `requirements.txt`, `install.sh`, `deploy/inkframe.service`, a C source or
  header under `inkframe/render/`, or `scripts/build_dither.sh`. It runs
  `bash install.sh --update --no-apt`: pip install of `requirements.txt`
  (with a loud warning if pip built anything from source, which is a bug:
  pin a version with an aarch64 wheel; a pure-Python sdist would also show up
  there and is harmless, but the current pins, feedparser's
  `feedparser-sgmllib` included, all install from wheels). pip may also print
  `ERROR: pip's dependency resolver does not currently take into account all
  the packages that are installed`, about apt packages the venv sees through
  `--system-site-packages` (on Trixie: `types-seaborn` from `python3-typeshed`
  wanting matplotlib); that is Debian's metadata, not ours, and the install
  still succeeds. Then: gcc build of `inkframe/render/libdither.so` if
  `dither.c` exists, refresh of the unit file plus `daemon-reload` when it
  changed, `enable`, restart, and the health check.
- **Restart only**, for everything else: `sudo -n systemctl restart inkframe`
  plus the same health check (`bash install.sh --health`).

The health check waits up to 30 s for `GET http://127.0.0.1/api/health` to
succeed, then prints `/api/status`. If it fails, the hook prints the last 40
journal lines, records `failed` in `/opt/inkframe/last-deploy`, starts the
service again if it is not running, and exits 1. Every step prints a
`==> step` line, so the failing one is easy to find in the `remote:` output.

The hook itself is rewritten only by `install.sh --bootstrap` or a full
install. A change to the hook in `install.sh` needs one of those to run
again (`ssh inkframe "bash /opt/inkframe/app/install.sh --no-apt"`).

## Logs, status, restart

```powershell
ssh inkframe "journalctl -u inkframe -n 100 -f"      # follow the log (add sudo if it shows nothing)
ssh inkframe "systemctl status inkframe"
ssh inkframe "curl -s http://127.0.0.1/api/status"
ssh inkframe "sudo systemctl restart inkframe"
ssh inkframe "bash /opt/inkframe/app/install.sh --health"
```

The service cannot use sudo (`NoNewPrivileges=true`). An app-level restart
(`POST /api/system/restart`) has to exit the process and let
`Restart=always` start it again.

## Switching to the real panel (Phase 7)

The unit ships with `Environment=INKFRAME_DISPLAY=mock`, so nothing touches
the panel. To use the panel, change that line in `deploy/inkframe.service` to
`Environment=INKFRAME_DISPLAY=epd`, commit, and run `.\deploy.ps1`. A changed
unit file triggers the full update, which installs the new unit, reloads
systemd and restarts the service. To switch back, revert the line the same way.

## Recovery

Run a full install on the Pi. It repeats every bootstrap step, checks
`main` out again if `/opt/inkframe/app` is empty, then does a full update.
It needs passwordless sudo, as bootstrap does:

```powershell
ssh inkframe "bash /opt/inkframe/app/install.sh"           # with apt
ssh inkframe "bash /opt/inkframe/app/install.sh --no-apt"  # faster
```

If `/opt/inkframe/app` is broken, copy a fresh `install.sh` over first:
`scp install.sh inkframe:install.sh`, then
`ssh inkframe "bash ~/install.sh"`.

To re-run the last deploy without a new commit (for example after a failure
caused by the network), run `post-receive-main` by hand. `OLD=0` forces the
full update, and the result is recorded for `deploy.ps1`:

```powershell
ssh inkframe 'bash /opt/inkframe/app/deploy/post-receive-main 0 $(git --git-dir=/opt/inkframe/repo.git rev-parse main)'
```

`install.sh` exit codes: 0 ok, 1 failure, 2 usage error, 3 finished but a
reboot is required. `bash install.sh --help` lists the modes.

## Sudo

`install.sh` runs as `inkframe`, refuses to run as root, and uses `sudo -n`
for root steps, so it fails fast instead of waiting for a password.
Bootstrap and full installs need general passwordless sudo, which Raspberry Pi
OS grants the first user through `/etc/sudoers.d/010_pi-nopasswd`. If that
was removed, run once with a cached password:
`ssh -t inkframe "sudo -v && bash ~/install.sh --bootstrap"`.

Bootstrap also installs `/etc/sudoers.d/inkframe` (the same content as
`deploy/sudoers-inkframe`, embedded in `install.sh`; keep the two identical).
It allows exactly what the hook and everyday ops need without a password:
`systemctl start|stop|restart|status|enable inkframe`, `systemctl
daemon-reload`, copying the unit file into place, `journalctl -u inkframe...`,
`apt-get`, `raspi-config` and a plain `reboot`. These three carry `SETENV`, so
`sudo -n DEBIAN_FRONTEND=noninteractive apt-get ...` still works on re-runs.
Deploys therefore keep working if the blanket rule is removed. It is not a
security boundary, since `apt-get` and `raspi-config` are equivalent to root.

## Line endings and the executable bit

`.gitattributes` keeps every file LF (only `*.ps1`/`*.bat` are CRLF), so the
shell scripts and the unit reach the Pi intact; `bootstrap.ps1` refuses to copy
an `install.sh` that contains CR bytes. A Windows checkout may not keep the
executable bit, so the scripts are always run as `bash install.sh` and
`bash deploy/post-receive-main`, never as `./install.sh`.
