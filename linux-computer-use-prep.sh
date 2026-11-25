#!/usr/bin/env bash
# linux-computer-use-prep.sh
# Host preparation for the DSH "Linux Computer Use" bundle on Ubuntu 24.04 / Wayland.
# Idempotent: safe to run more than once.
#
# Why each step is needed:
#   1. /dev/uinput is what lets the plugin inject real kernel input events on
#      Wayland (there is no SendInput equivalent). It ships as a built-in misc
#      device, owned root:root mode 0600, so a normal user cannot open it.
#   2. A udev rule makes it group "input", mode 0660, permanently, and the user
#      is added to that group so the desk server can open it.
#   3. wl-clipboard provides wl-copy / wl-paste for clipboard read/write.
#   4. ydotool (+ ydotoold) is the fallback injection path used when /dev/uinput
#      is unavailable; harmless if unused.
#
# Usage:
#   chmod +x linux-computer-use-prep.sh
#   sudo ./linux-computer-use-prep.sh
#   # then LOG OUT and LOG BACK IN (group membership needs a fresh session)
#   ./linux-computer-use-prep.sh --verify     # no sudo needed
#
# Undo:  sudo ./linux-computer-use-prep.sh --uninstall

set -uo pipefail

TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
SOCKET_PATH="/run/ydotoold.socket"
UNIT_PATH="/etc/systemd/system/ydotoold.service"
UDEV_RULE="/etc/udev/rules.d/60-uinput.rules"
LAUNCHER="/usr/local/bin/dsh-linux-desktop-mcp"
SHARE_DIR="$TARGET_HOME/.local/share/dsh-linux-computer-use"

c_ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
c_warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
c_bad()  { printf '  \033[31mMISS\033[0m %s\n' "$*"; }
say()    { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "This mode needs root. Re-run with: sudo $0 ${1:-}" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------- verify mode
if [ "${1:-}" = "--verify" ]; then
  say "Verification (user: $TARGET_USER)"
  printf 'session: %s / %s\n' "${XDG_SESSION_TYPE:-unknown}" "${XDG_CURRENT_DESKTOP:-unknown}"

  if [ -c /dev/uinput ]; then
    c_ok "/dev/uinput is present"
    ls -l /dev/uinput | sed 's/^/       /'
    if [ -w /dev/uinput ] && [ -r /dev/uinput ]; then
      c_ok "/dev/uinput readable+writable by you (input injection ready)"
    else
      c_warn "/dev/uinput NOT accessible to you yet"
      c_warn "  -> group 'input' only applies to a fresh login: log out and back in"
    fi
  else
    c_bad "/dev/uinput missing (kernel built without CONFIG_INPUT_UINPUT?)"
  fi

  if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx input; then
    c_ok "user $TARGET_USER is in group 'input'"
  else
    c_bad "user $TARGET_USER is NOT in group 'input'"
  fi

  command -v wl-copy >/dev/null && c_ok "wl-copy: $(command -v wl-copy)" || c_bad "wl-copy missing (apt install wl-clipboard)"
  command -v wl-paste >/dev/null && c_ok "wl-paste: $(command -v wl-paste)" || c_bad "wl-paste missing (apt install wl-clipboard)"
  command -v notify-send >/dev/null && c_ok "notify-send: $(command -v notify-send)" || c_warn "notify-send missing (apt install libnotify-bin)"

  if command -v ydotool >/dev/null && command -v ydotoold >/dev/null; then
    c_ok "ydotool fallback installed ($(command -v ydotool))"
    if systemctl is-active --quiet ydotoold; then
      c_ok "ydotoold service active"
    else
      c_warn "ydotoold service not active (fallback only, not required)"
    fi
    if [ -S "$SOCKET_PATH" ]; then
      ls -l "$SOCKET_PATH" | sed 's/^/       /'
      case "$(stat -c '%A' "$SOCKET_PATH")" in
        srw-------*) c_ok "socket mode is 0600 (private to you)" ;;
        *) c_warn "socket mode is not 0600 — other local users could inject input" ;;
      esac
    fi
  else
    c_warn "ydotool not installed (optional fallback; /dev/uinput is the primary path)"
  fi

  if [ -x "$LAUNCHER" ]; then
    c_ok "MCP launcher installed: $LAUNCHER"
    if [ -f "$SHARE_DIR/linux_desktop_mcp.py" ]; then
      c_ok "server copy present: $SHARE_DIR"
    else
      c_warn "server copy missing: $SHARE_DIR (it syncs when the plugin loads once)"
    fi
  else
    c_bad "MCP launcher missing: $LAUNCHER"
  fi

  say "Summary"
  if [ -r /dev/uinput ] && [ -w /dev/uinput ]; then
    c_ok "input injection: READY"
  else
    c_bad "input injection: not ready yet (see above)"
  fi
  exit 0
fi

# ------------------------------------------------------------- uninstall mode
if [ "${1:-}" = "--uninstall" ]; then
  require_root --uninstall
  say "Removing ydotoold unit and udev rule"
  systemctl disable --now ydotoold 2>/dev/null || true
  rm -f "$UNIT_PATH" "$UDEV_RULE" "$LAUNCHER"
  systemctl daemon-reload
  udevadm control --reload-rules 2>/dev/null || true
  echo "Kept installed packages and group membership."
  echo "Remove manually if wanted: apt-get remove ydotool wl-clipboard; gpasswd -d $TARGET_USER input"
  exit 0
fi

require_root ""

say "Target user: $TARGET_USER (uid $TARGET_UID)"

# 1. packages ---------------------------------------------------------------
say "Installing wl-clipboard (required) and ydotool (optional fallback)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || c_warn "apt-get update failed; trying install anyway"
if apt-get install -y --no-install-recommends wl-clipboard; then
  c_ok "wl-clipboard $(dpkg-query -W -f='${Version}' wl-clipboard 2>/dev/null)"
else
  c_bad "could not install wl-clipboard — clipboard tools will be unavailable"
fi
if apt-get install -y --no-install-recommends ydotool; then
  c_ok "ydotool $(dpkg-query -W -f='${Version}' ydotool 2>/dev/null)"
else
  c_warn "could not install ydotool — continuing, /dev/uinput is the primary path"
fi
apt-get install -y --no-install-recommends libnotify-bin >/dev/null 2>&1 && c_ok "libnotify-bin present" || c_warn "libnotify-bin not installed (notifications optional)"

# 2. uinput device ----------------------------------------------------------
say "Preparing /dev/uinput"
# On Ubuntu 24.04 uinput is built into the kernel, so there is no module to
# load; try anyway for kernels that build it as a module, but never fail here.
modprobe uinput 2>/dev/null && c_ok "uinput module loaded" || c_ok "uinput is built into this kernel (no module needed)"
if [ -c /dev/uinput ]; then
  c_ok "/dev/uinput exists"
else
  c_bad "/dev/uinput does not exist; input injection cannot work on this kernel"
fi

# 3. udev rule --------------------------------------------------------------
say "Installing udev rule for /dev/uinput"
cat > "$UDEV_RULE" <<'EOF'
# Let the "input" group inject synthetic input through /dev/uinput (DSH Linux Computer Use).
KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", MODE="0660", OPTIONS+="watch"
EOF
udevadm control --reload-rules || c_warn "udevadm control failed"
udevadm trigger --name-match=uinput 2>/dev/null || c_warn "udevadm trigger failed"
# The device already exists, so the rule may not re-apply until reboot:
# fix permissions now as well, which makes the plugin usable in this boot.
if [ -c /dev/uinput ]; then
  chgrp input /dev/uinput && chmod 0660 /dev/uinput
  c_ok "$UDEV_RULE installed; current permissions applied"
  ls -l /dev/uinput | sed 's/^/       /'
fi

# 4. group membership -------------------------------------------------------
say "Adding $TARGET_USER to the 'input' group"
getent group input >/dev/null || groupadd --system input
if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx input; then
  c_ok "already a member"
else
  usermod -aG input "$TARGET_USER" && c_ok "added — takes effect after the next login"
fi

# 5. ydotoold unit (fallback path) ------------------------------------------
if command -v ydotoold >/dev/null; then
  say "Installing the ydotoold fallback unit"
  cat > "$UNIT_PATH" <<EOF
[Unit]
Description=ydotool input injection daemon (DSH Linux Computer Use fallback)
Documentation=https://github.com/ReimuNotMoe/ydotool
After=systemd-user-sessions.service

[Service]
Type=simple
User=$TARGET_USER
Group=input
UMask=0077
ExecStart=/usr/bin/ydotoold --socket-path $SOCKET_PATH --socket-perm 0600
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  if systemctl enable --now ydotoold 2>/dev/null; then
    c_ok "ydotoold running"
  else
    c_warn "ydotoold did not start (fallback only); journal:"
    journalctl -u ydotoold -n 15 --no-pager 2>/dev/null | sed 's/^/       /' || true
  fi
  if [ -S "$SOCKET_PATH" ]; then
    chown "$TARGET_USER:$TARGET_USER" "$SOCKET_PATH" 2>/dev/null || true
    chmod 0600 "$SOCKET_PATH" 2>/dev/null || true
    ls -l "$SOCKET_PATH" | sed 's/^/       /'
  fi
else
  c_warn "ydotoold not installed; skipping the fallback unit"
fi

# 6. MCP launcher (fixed path the cordis patch can reference) ---------------
say "Installing the MCP launcher"
install -d -m 0755 /usr/local/bin
cat > "$LAUNCHER" <<'LAUNCH'
#!/bin/sh
# Installed by dsh-plugin-linux-computer-use.
# Launches the bundle's stdio MCP server from the copy it syncs under the
# user's home, so the cordis patch never has to hard-code a package path.
DIR="${DSH_LINUX_CU_DIR:-$HOME/.local/share/dsh-linux-computer-use}"
if [ ! -f "$DIR/linux_desktop_mcp.py" ]; then
  echo "dsh-linux-desktop-mcp: no server copy in $DIR" >&2
  echo "enable the plugin once (it syncs the server there), then retry" >&2
  exit 1
fi
exec /usr/bin/python3 -u "$DIR/linux_desktop_mcp.py" "$@"
LAUNCH
chmod 0755 "$LAUNCHER"
c_ok "launcher: $LAUNCHER"

install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_GROUP" "$SHARE_DIR" 2>/dev/null \
  || install -d -m 0700 -o "$TARGET_USER" "$SHARE_DIR"
c_ok "server home: $SHARE_DIR"

# 7. summary ----------------------------------------------------------------
say "Done"
cat <<EOF
Next steps:
  1. LOG OUT and LOG BACK IN (or reboot) so the 'input' group takes effect.
  2. Run:  $0 --verify
  3. Tell the DSH agent it is done; it will verify and switch the plugin on.

Note: any process running as $TARGET_USER can now inject keyboard/mouse input.
That is the point of the feature. The ydotoold socket stays 0600 so other local
users cannot, and /dev/uinput is limited to the 'input' group.
EOF
