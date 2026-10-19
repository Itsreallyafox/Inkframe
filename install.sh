#!/usr/bin/env bash
# Inkframe installer for the Raspberry Pi (Raspberry Pi OS Lite 64-bit, Trixie).
#
# Runs ON THE PI as user inkframe.  Always invoke it as "bash install.sh ..."
# (a Windows checkout may not keep the executable bit).  Root steps use
# "sudo -n" so nothing ever waits for a password.  Every mode is idempotent.
#
#   bash ~/install.sh --bootstrap   first install, from a standalone copy
#   bash install.sh --update        after a push (run by deploy/post-receive-main)
#   bash install.sh --health        health check only (shared with the hook)
#   bash install.sh                 full install / recovery from a checkout
#
# Runbook: docs/DEPLOY.md.  Nothing below main() runs for --help, so
# "bash install.sh --help" is safe anywhere, including Git Bash on Windows.
set -euo pipefail

APP_ROOT=/opt/inkframe
REPO=$APP_ROOT/repo.git
APP=$APP_ROOT/app
VENV=$APP_ROOT/.venv
DATA=$APP_ROOT/data
SERVICE=inkframe
# The invoking user and the service user.  Not called USER so the login
# environment's $USER is left alone.
SVC_USER=inkframe
UNIT_SRC=$APP/deploy/inkframe.service
UNIT_DST=/etc/systemd/system/$SERVICE.service
SUDOERS_DST=/etc/sudoers.d/inkframe
BOOT_CONFIG=/boot/firmware/config.txt
# deploy/inkframe.service sets INKFRAME_PORT=80
BASE_URL=http://127.0.0.1:80
HEALTH_TIMEOUT=30
JOURNAL_LINES=40
# libc6-dev is listed explicitly: gcc cannot link libdither.so without it.
APT_PACKAGES=(git python3-venv python3-pip python3-dev python3-spidev python3-gpiozero
  python3-lgpio libopenjp2-7 gcc libc6-dev avahi-daemon curl)

NO_APT=0
NEED_REBOOT=0
NEED_RELOGIN=0
CURRENT_STEP=""
# "reboot" / "usage" mark the deliberate non-zero exits; anything else is a failure.
EXIT_KIND=error
WORK=""

usage() {
  cat <<'EOF'
Usage: bash install.sh [--bootstrap | --update | --health] [--no-apt]
       bash install.sh --help

Inkframe installer.  Run on the Pi as user inkframe; root steps use sudo -n.

Modes:
  --bootstrap  First install from a standalone copy (scp'd to ~), before any
               checkout exists: apt packages, spi/gpio groups, SPI, the
               /opt/inkframe dirs, bare repo + post-receive hook, venv and the
               sudoers drop-in.  Does not install the service (the first push
               does that).
  --update     After a push (run by the post-receive hook): pip install -r
               requirements.txt, build libdither.so, install/refresh the
               systemd unit, restart, health check.
  --health     Wait up to 30 s for GET /api/health, then print /api/status.
  (no mode)    Full install / recovery from a checkout: everything --bootstrap
               does, then everything --update does.

Options:
  --no-apt     Skip apt-get (faster re-runs).
  --help       Show this text and exit.

Exit status: 0 ok, 1 failure, 2 usage error, 3 finished but a reboot is
required (SPI was just enabled).
EOF
}

# ---------------------------------------------------------------- helpers

step() {
  CURRENT_STEP=$*
  printf '==> %s\n' "$*"
}

info() {
  printf '    %s\n' "$*"
}

warn() {
  printf 'WARNING: %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage_error() {
  printf 'install.sh: %s\n' "$*" >&2
  printf 'Try: bash install.sh --help\n' >&2
  EXIT_KIND=usage
  exit 2
}

on_exit() {
  local rc=$?
  if [ -n "$WORK" ]; then
    rm -rf "$WORK"
  fi
  if [ "$rc" -eq 0 ] || [ "$EXIT_KIND" != error ]; then
    return 0
  fi
  printf 'install.sh FAILED (exit %d) in step: %s\n' "$rc" "${CURRENT_STEP:-startup}" >&2
  # Exit status 3 means "reboot required" to callers (deploy/bootstrap.ps1
  # reboots the Pi on it), so a command that happened to fail with 3 must
  # not leak it.
  if [ "$rc" -eq 3 ]; then
    exit 1
  fi
}

# preflight full|service|none: refuse to run anywhere but on the Pi as
# $SVC_USER, and check that the sudo rights the mode needs are passwordless.
preflight() {
  [ "$(uname -s)" = Linux ] || die "install.sh runs on the Pi, not here ($(uname -s)); see docs/DEPLOY.md"
  [ "$(id -u)" -ne 0 ] || die "do not run install.sh as root; run it as $SVC_USER (root steps use sudo -n)"
  [ "$(id -un)" = "$SVC_USER" ] || die "run install.sh as user $SVC_USER (current user: $(id -un))"
  case "$1" in
    full)
      sudo -n true 2>/dev/null ||
        die "passwordless sudo is unavailable for $SVC_USER (sudo -n true failed). Raspberry Pi OS grants it to the first user by default; restore that, or run once with a cached password: ssh -t inkframe 'sudo -v && bash ~/install.sh --bootstrap'"
      ;;
    service)
      # The hook path only needs the commands in the sudoers drop-in, which
      # does not include "true", so accept either form of passwordless sudo.
      sudo -n true 2>/dev/null ||
        sudo -n -l /usr/bin/systemctl restart "$SERVICE" >/dev/null 2>&1 ||
        die "passwordless sudo for 'systemctl restart $SERVICE' is unavailable; reinstall the sudoers drop-in with a full install: ssh -t inkframe 'sudo -v && bash $APP/install.sh --no-apt'"
      ;;
    none) ;;
    *) die "internal: unknown preflight level $1" ;;
  esac
  WORK=$(mktemp -d -t inkframe-install.XXXXXX)
}

journal_tail() {
  printf -- '---- last %d journal lines for %s ----\n' "$JOURNAL_LINES" "$SERVICE"
  sudo -n /usr/bin/journalctl -u "$SERVICE" -n "$JOURNAL_LINES" --no-pager 2>/dev/null ||
    journalctl -u "$SERVICE" -n "$JOURNAL_LINES" --no-pager ||
    true
  printf -- '---- end of journal ----\n'
}

# ------------------------------------------------------- bootstrap steps

apt_packages() {
  if [ "$NO_APT" -eq 1 ]; then
    step "apt packages: skipped (--no-apt)"
    return 0
  fi
  step "apt-get update"
  sudo -n DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get update -q
  step "apt-get install ${APT_PACKAGES[*]}"
  sudo -n DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get install -y -q \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
    "${APT_PACKAGES[@]}"
}

ensure_groups() {
  step "groups spi and gpio for $SVC_USER"
  local group
  for group in spi gpio; do
    getent group "$group" >/dev/null || die "group $group does not exist (is this Raspberry Pi OS?)"
    case " $(id -nG "$SVC_USER") " in
      *" $group "*)
        info "already in $group"
        ;;
      *)
        sudo -n usermod -aG "$group" "$SVC_USER"
        NEED_RELOGIN=1
        info "added to $group"
        ;;
    esac
  done
}

enable_spi() {
  step "SPI"
  if [ -e /dev/spidev0.0 ]; then
    info "/dev/spidev0.0 present"
    return 0
  fi
  if command -v raspi-config >/dev/null 2>&1; then
    sudo -n /usr/bin/raspi-config nonint do_spi 0
    info "enabled with raspi-config"
    # raspi-config also applies dtparam spi=on to the running kernel; once
    # udev has created the device the change is live as well as persisted
    # in config.txt, so no reboot is needed.
    udevadm settle --timeout=10 2>/dev/null || true
    if [ -e /dev/spidev0.0 ]; then
      info "/dev/spidev0.0 present now; no reboot needed"
      return 0
    fi
  else
    [ -f "$BOOT_CONFIG" ] || die "raspi-config is missing and $BOOT_CONFIG does not exist; enable SPI by hand"
    if grep -Eq '^[[:space:]]*dtparam=spi=on' "$BOOT_CONFIG"; then
      info "dtparam=spi=on already in $BOOT_CONFIG"
    else
      printf '\n[all]\ndtparam=spi=on\n' | sudo -n tee -a "$BOOT_CONFIG" >/dev/null
      info "appended dtparam=spi=on to $BOOT_CONFIG"
    fi
  fi
  NEED_REBOOT=1
}

ensure_dirs() {
  step "directories $APP_ROOT/{app,data}"
  sudo -n mkdir -p "$APP" "$DATA"
  sudo -n chown "$SVC_USER:$SVC_USER" "$APP_ROOT" "$APP" "$DATA"
}

# The bare repo's post-receive hook.  Deliberately small and stable: it only
# checks main out and hands over to deploy/post-receive-main, which ships
# with the code.  It changes only when bootstrap or a full install re-runs.
hook_script() {
  cat <<'HOOK'
#!/usr/bin/env bash
# Inkframe post-receive hook.  Installed by install.sh (--bootstrap or a full
# install); edit install.sh, not this copy.  Checks main out into the app dir,
# then runs the versioned deploy step deploy/post-receive-main OLD NEW.
set -euo pipefail
REPO=/opt/inkframe/repo.git
APP=/opt/inkframe/app
unset GIT_DIR GIT_WORK_TREE
# one deploy at a time; the lock is held until post-receive-main exits
exec 9>"$REPO/deploy.lock"
flock -w 600 9 || { echo "inkframe: another deploy is still running; nothing deployed"; exit 1; }
while read -r old new ref; do
  if [ "$ref" != refs/heads/main ]; then
    echo "inkframe: ignoring $ref (only main is deployed)"
    continue
  fi
  echo "inkframe: checking out main into $APP"
  git --git-dir="$REPO" --work-tree="$APP" checkout -f main
  if [ ! -f "$APP/deploy/post-receive-main" ]; then
    echo "inkframe: $APP/deploy/post-receive-main is missing; nothing deployed"
    exit 1
  fi
  exec bash "$APP/deploy/post-receive-main" "$old" "$new"
done
HOOK
}

setup_repo() {
  step "bare repo $REPO"
  if [ -f "$REPO/HEAD" ]; then
    info "exists"
  else
    git -c init.defaultBranch=main init -q --bare "$REPO"
    info "created"
  fi
  git --git-dir="$REPO" symbolic-ref HEAD refs/heads/main

  step "post-receive hook"
  local hook=$REPO/hooks/post-receive
  hook_script >"$hook.new"
  if cmp -s "$hook.new" "$hook"; then
    rm -f "$hook.new"
    info "unchanged"
  else
    chmod 0755 "$hook.new"
    mv -f "$hook.new" "$hook"
    info "installed"
  fi
}

# ensure_venv [upgrade-pip]
ensure_venv() {
  step "venv $VENV"
  if [ -x "$VENV/bin/python" ]; then
    info "exists ($("$VENV/bin/python" --version 2>&1))"
  else
    python3 -m venv --system-site-packages "$VENV"
    info "created ($("$VENV/bin/python" --version 2>&1))"
  fi
  if [ "${1:-}" = upgrade-pip ]; then
    step "upgrade pip in the venv"
    PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV/bin/python" -m pip install -q -U pip
  fi
}

# Identical to deploy/sudoers-inkframe (the reference copy); keep them in sync.
sudoers_content() {
  cat <<'SUDOERS'
# /etc/sudoers.d/inkframe: installed by install.sh (--bootstrap or a full
# install).  Reference copy: deploy/sudoers-inkframe.  Keep the two identical.
#
# Passwordless root commands for user inkframe, so the git post-receive hook
# (install.sh --update / --health) and everyday ops keep working even if
# Raspberry Pi OS's blanket NOPASSWD drop-in (010_pi-nopasswd) is removed.
# This is not a security boundary (apt-get and raspi-config are
# root-equivalent); the list is short so its purpose stays obvious.

# Service control: the hook restarts; ops start/stop/inspect; install.sh
# enables the unit and reloads systemd after changing it.
Cmnd_Alias INKFRAME_SERVICE = /usr/bin/systemctl start inkframe, \
                              /usr/bin/systemctl stop inkframe, \
                              /usr/bin/systemctl restart inkframe, \
                              /usr/bin/systemctl status inkframe, \
                              /usr/bin/systemctl enable inkframe, \
                              /usr/bin/systemctl daemon-reload

# install.sh --update copies the unit file from the checkout (this exact
# command line only).
Cmnd_Alias INKFRAME_UNIT = /usr/bin/install -m 0644 /opt/inkframe/app/deploy/inkframe.service /etc/systemd/system/inkframe.service

# Logs: journalctl for the inkframe unit, any further flags (-n, -f, ...).
Cmnd_Alias INKFRAME_LOGS = /usr/bin/journalctl -u inkframe*

# OS maintenance: packages and SPI (install.sh), and a plain reboot after SPI
# is enabled (deploy/bootstrap.ps1).
Cmnd_Alias INKFRAME_OS = /usr/bin/apt-get, /usr/bin/raspi-config, /usr/sbin/reboot ""

inkframe ALL=(root) NOPASSWD: INKFRAME_SERVICE, INKFRAME_UNIT, INKFRAME_LOGS

# SETENV: install.sh runs "sudo -n DEBIAN_FRONTEND=noninteractive apt-get ...".
# This file sorts after 010_pi-nopasswd, so this is the last rule matching
# apt-get, and without the tag sudo refuses the variable.
inkframe ALL=(root) NOPASSWD:SETENV: INKFRAME_OS
SUDOERS
}

install_sudoers() {
  step "sudoers drop-in $SUDOERS_DST"
  local candidate=$WORK/sudoers-inkframe
  sudoers_content >"$candidate"
  sudo -n /usr/sbin/visudo -cqf "$candidate" || die "the embedded sudoers drop-in does not parse (visudo -c); not installed"
  if sudo -n cmp -s "$candidate" "$SUDOERS_DST"; then
    info "unchanged"
    return 0
  fi
  sudo -n install -m 0440 -o root -g root "$candidate" "$SUDOERS_DST"
  if ! sudo -n /usr/sbin/visudo -cq; then
    sudo -n rm -f "$SUDOERS_DST"
    die "sudo configuration failed visudo -c with the drop-in; removed $SUDOERS_DST again"
  fi
  info "installed"
}

write_marker() {
  step "bootstrap marker $APP_ROOT/.bootstrap-done"
  date -u +%Y-%m-%dT%H:%M:%SZ >"$APP_ROOT/.bootstrap-done"
}

# ---------------------------------------------------------- update steps

# Full mode only: if $APP has no checkout yet but main was pushed, check it
# out the same way the hook does.
ensure_checkout() {
  step "checkout $APP"
  if [ -f "$APP/install.sh" ]; then
    info "present"
    return 0
  fi
  git --git-dir="$REPO" rev-parse -q --verify refs/heads/main >/dev/null ||
    die "$APP is empty and nothing has been pushed yet: run deploy.ps1 on the desktop (or use --bootstrap)"
  git --git-dir="$REPO" --work-tree="$APP" checkout -f main
}

# compiled_packages PIP_LOG: names of the packages pip built from source
compiled_packages() {
  sed -nE 's/.*(Building wheel for|Running setup\.py [a-z_]+ for) ([^ ]+).*/\2/p' "$1" | sort -u | paste -sd ' ' -
}

pip_requirements() {
  step "pip install -r $APP/requirements.txt"
  local req=$APP/requirements.txt log=$WORK/pip.log built
  [ -f "$req" ] || die "missing $req (is $APP a checkout?)"
  if ! PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV/bin/python" -m pip install --prefer-binary -r "$req" 2>&1 | tee "$log"; then
    die "pip install failed"
  fi
  built=$(compiled_packages "$log")
  if [ -n "$built" ]; then
    printf '\n%s\n' '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!'
    printf 'WARNING: pip compiled something on the Pi: %s\n' "$built"
    printf 'WARNING: that is a bug (INKFRAME_PLAN.md section 3): pin a version with an aarch64 wheel.\n'
    printf '%s\n\n' '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!'
  fi
}

# The one place the native dither library is built.
build_native() {
  local src=$APP/inkframe/render/dither.c out=$APP/inkframe/render/libdither.so
  if [ ! -f "$src" ]; then
    step "build libdither.so: skipped (no inkframe/render/dither.c yet)"
    return 0
  fi
  step "build libdither.so"
  # build next to the target and rename, so a running process keeps its mapping
  gcc -O2 -shared -fPIC -o "$out.tmp" "$src" -lm
  mv -f "$out.tmp" "$out"
}

install_unit() {
  step "systemd unit $UNIT_DST"
  [ -f "$UNIT_SRC" ] || die "missing $UNIT_SRC"
  if cmp -s "$UNIT_SRC" "$UNIT_DST"; then
    info "unchanged"
  else
    # exactly the command line the sudoers drop-in allows
    sudo -n /usr/bin/install -m 0644 "$UNIT_SRC" "$UNIT_DST"
    sudo -n /usr/bin/systemctl daemon-reload
    info "installed; systemd reloaded"
  fi
  if systemctl is-enabled --quiet "$SERVICE"; then
    info "enabled"
  else
    sudo -n /usr/bin/systemctl enable "$SERVICE"
  fi
}

restart_service() {
  step "restart $SERVICE"
  if ! sudo -n /usr/bin/systemctl restart "$SERVICE"; then
    journal_tail
    die "systemctl restart $SERVICE failed"
  fi
}

health_check() {
  step "health check $BASE_URL/api/health (up to ${HEALTH_TIMEOUT}s)"
  local start=$SECONDS status
  until curl -fs -m 3 -o /dev/null "$BASE_URL/api/health"; do
    if [ $((SECONDS - start)) -ge "$HEALTH_TIMEOUT" ]; then
      # one more attempt with errors shown, so the reason is in the log
      curl -fsS -m 3 -o /dev/null "$BASE_URL/api/health" || true
      journal_tail
      die "$SERVICE did not answer $BASE_URL/api/health within ${HEALTH_TIMEOUT}s"
    fi
    sleep 1
  done
  info "healthy after $((SECONDS - start))s"
  step "GET /api/status"
  if status=$(curl -fsS -m 10 "$BASE_URL/api/status"); then
    printf '%s\n' "$status" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$status"
  else
    warn "GET $BASE_URL/api/status failed (the service is up; see the journal)"
  fi
}

update_steps() {
  ensure_venv
  pip_requirements
  build_native
  install_unit
  restart_service
  health_check
}

# ----------------------------------------------------------------- modes

bootstrap_steps() {
  apt_packages
  ensure_groups
  enable_spi
  ensure_dirs
  setup_repo
  ensure_venv upgrade-pip
  install_sudoers
  write_marker
}

# summary bootstrap|full: final report; exits 3 if a reboot is required.
summary() {
  step "summary"
  info "bare repo: $REPO (HEAD -> $(git --git-dir="$REPO" symbolic-ref HEAD))"
  info "checkout:  $APP"
  info "venv:      $VENV ($("$VENV/bin/python" --version 2>&1))"
  info "data:      $DATA"
  info "groups:    $(id -nG "$SVC_USER")"
  if [ -e /dev/spidev0.0 ]; then
    info "SPI:       /dev/spidev0.0 present"
  else
    info "SPI:       enabled, active after the reboot"
  fi
  if [ "$1" = bootstrap ]; then
    info "next:      on the desktop run deploy.ps1 (the first git push installs and starts the service)"
  else
    info "service:   $(systemctl is-active "$SERVICE" || true)"
  fi
  if [ "$NEED_RELOGIN" -eq 1 ]; then
    info "group membership changed: open a new ssh session for it to apply (the service gets it on start)"
  fi
  if [ "$NEED_REBOOT" -eq 1 ]; then
    printf '\nREBOOT REQUIRED (SPI enabled); run: sudo reboot\n'
    EXIT_KIND=reboot
    exit 3
  fi
  printf '==> done\n'
}

run_bootstrap() {
  preflight full
  bootstrap_steps
  summary bootstrap
}

run_full() {
  preflight full
  bootstrap_steps
  ensure_checkout
  update_steps
  summary full
}

run_update() {
  preflight service
  update_steps
  printf '==> done\n'
}

run_health() {
  preflight none
  health_check
}

main() {
  local mode=full mode_given=0 arg
  for arg in "$@"; do
    case "$arg" in
      -h | --help)
        usage
        exit 0
        ;;
      --no-apt)
        NO_APT=1
        ;;
      --bootstrap | --update | --health)
        [ "$mode_given" -eq 0 ] || usage_error "give at most one of --bootstrap, --update, --health"
        mode=${arg#--}
        mode_given=1
        ;;
      *)
        usage_error "unknown argument: $arg"
        ;;
    esac
  done
  trap on_exit EXIT
  case "$mode" in
    bootstrap) run_bootstrap ;;
    update) run_update ;;
    health) run_health ;;
    full) run_full ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
