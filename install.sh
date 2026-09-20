#!/bin/bash
# loci installer — symlinks the CLI and installs tunneld as a LaunchDaemon.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PMD="$HOME/.local/bin/pymobiledevice3"
PLIST=/Library/LaunchDaemons/com.loci.tunneld.plist

[[ -x "$PMD" ]] || { echo "missing $PMD — run: pipx install pymobiledevice3"; exit 1; }

mkdir -p "$HOME/.local/bin"
ln -sf "$REPO/bin/iphone-loc" "$HOME/.local/bin/loci"
echo "linked: ~/.local/bin/loci"

case "${1:-}" in
  daemon)
    sudo -v || { echo "sudo failed — daemon NOT installed" >&2; exit 1; }
    # Any manually-started tunneld would hold the port and shadow the daemon.
    sudo pkill -f "remote tunneld" 2>/dev/null || true
    sed "s|__PMD__|$PMD|" "$REPO/launchd/com.loci.tunneld.plist.template" \
      | sudo tee "$PLIST" >/dev/null
    sudo chown root:wheel "$PLIST"
    sudo chmod 644 "$PLIST"
    [[ -f "$PLIST" ]] || { echo "plist did not land at $PLIST" >&2; exit 1; }
    sudo launchctl bootout system/com.loci.tunneld 2>/dev/null || true
    sudo launchctl bootstrap system "$PLIST"
    sleep 6
    if curl -sf --max-time 5 http://127.0.0.1:49151/ >/dev/null 2>&1; then
      echo "daemon installed and serving:"
      curl -s --max-time 5 http://127.0.0.1:49151/
      echo
    else
      echo "daemon installed but tunneld not answering — check /var/log/loci-tunneld.err" >&2
      exit 1
    fi
    ;;
  wifi)
    PMDPY="$HOME/.local/share/uv/tools/pymobiledevice3/bin/python"
    [[ -x "$PMDPY" ]] || { echo "missing $PMDPY — run: uv tool install pymobiledevice3 --python 3.13" >&2; exit 1; }
    "$PMDPY" -c 'import ssl,sys; assert sys.version_info >= (3,13); assert hasattr(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),"set_psk_client_callback")' \
      || { echo "that python lacks native TLS-PSK; the Wi-Fi tunnel needs python>=3.13" >&2; exit 1; }
    PLIST_W=/Library/LaunchDaemons/com.loci.tunnel.plist
    sudo -v || { echo "sudo failed — daemon NOT installed" >&2; exit 1; }
    # Pin the daemon to one device. Explicit config wins; otherwise auto-detect,
    # but only when discovery is unambiguous.
    UDID="${IPHONE_UDID:-}"
    [[ -z "$UDID" && -f "$HOME/.config/loci/config" ]] && \
      UDID=$(grep -E '^IPHONE_UDID=' "$HOME/.config/loci/config" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'"')
    if [[ -z "$UDID" ]]; then
      UDID=$("$PMDPY" - <<'DETECT'
import asyncio
from pymobiledevice3.remote.tunnel_service import get_remote_pairing_tunnel_services
async def m():
    ids = {s.remote_identifier for s in await get_remote_pairing_tunnel_services()}
    print(next(iter(ids)) if len(ids) == 1 else "")
asyncio.run(m())
DETECT
)
    fi
    if [[ -z "$UDID" ]]; then
      echo "could not pin a single device. Set it explicitly:" >&2
      echo "  mkdir -p ~/.config/loci && echo 'IPHONE_UDID=<udid>' >> ~/.config/loci/config" >&2
      exit 1
    fi
    echo "pinned device: $UDID"
    rm -f "$HOME/.local/state/loci/rsd"   # never accept a stale address as success
    sed -e "s|__PYTHON__|$PMDPY|" \
        -e "s|__SCRIPT__|$REPO/bin/loci-tunnel|" \
        -e "s|__STATE__|$HOME/.local/state/loci|" \
        -e "s|__HOME__|$HOME|" \
        -e "s|__UDID__|$UDID|" \
        "$REPO/launchd/com.loci.tunnel.plist.template" | sudo tee "$PLIST_W" >/dev/null
    sudo chown root:wheel "$PLIST_W"; sudo chmod 644 "$PLIST_W"
    sudo launchctl bootout system/com.loci.tunnel 2>/dev/null || true
    sudo launchctl bootstrap system "$PLIST_W"
    echo "waiting for the tunnel to come up..."
    for _ in $(seq 1 20); do
      [[ -r "$HOME/.local/state/loci/rsd" ]] && break
      sleep 2
    done
    if [[ -r "$HOME/.local/state/loci/rsd" ]]; then
      echo "wifi tunnel up — rsd $(cat "$HOME/.local/state/loci/rsd")"
    else
      echo "no tunnel yet — check /var/log/loci-tunnel.err" >&2; exit 1
    fi
    ;;
  server)
    VENV="$REPO/.venv"
    [[ -x "$VENV/bin/python" ]] || {
      echo "creating venv"; python3 -m venv "$VENV"; "$VENV/bin/pip" install --quiet flask; }
    AGENT="$HOME/Library/LaunchAgents/com.loci.server.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    sed -e "s|__PYTHON__|$VENV/bin/python|" \
        -e "s|__APP__|$REPO/server/app.py|" \
        -e "s|__LOCI__|$HOME/.local/bin/loci|" \
        -e "s|__LOG__|$HOME/Library/Logs|" \
        "$REPO/launchd/com.loci.server.plist.template" > "$AGENT"
    launchctl bootout "gui/$UID/com.loci.server" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$AGENT"
    sleep 4
    IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)
    if curl -sf --max-time 5 "http://127.0.0.1:8787/status" >/dev/null; then
      echo "server running — open on your phone:  http://$IP:8787"
    else
      echo "server not answering — check ~/Library/Logs/loci-server.err" >&2; exit 1
    fi
    ;;
  *)
    echo "usage: ./install.sh [wifi|daemon|server]"
    echo "  (no arg)  link the CLI only"
    echo "  wifi      Wi-Fi tunnel supervisor as a root LaunchDaemon"
    echo "  daemon    tunneld (USB) as a root LaunchDaemon"
    echo "  server    web UI as a login LaunchAgent"
    ;;
esac
