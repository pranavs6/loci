# loci

Spoof GPS on a paired iPhone from macOS. iOS 17+, no jailbreak.

## Install

```bash
brew install uv
uv tool install pymobiledevice3 --python 3.13
./install.sh           # CLI only
./install.sh wifi      # + Wi-Fi tunnel at boot (sudo)   <- the one you want
./install.sh daemon    # + tunneld for USB (sudo)
./install.sh server    # + web UI at login
```

**Python 3.13+ is mandatory, and it must not be Homebrew's.** The Wi-Fi tunnel
does a TLS-PSK handshake. Python <3.13 falls back to the `sslpsk_pmd3` shim,
which fails `NO_CIPHERS_AVAILABLE` against OpenSSL 3.6. And Homebrew's
python@3.13 has a broken `pyexpat` (resolves to `/usr/lib/libexpat.1.dylib`,
missing `_XML_SetAllocTrackerActivationThreshold`), which breaks `plistlib`,
so `platform.mac_ver()` returns `''`, so pip's truststore dies on `int('')`.
uv's standalone build avoids all of it.

## Use

```bash
loci set london
loci set 12.9716 77.5946
loci status
loci clear
```

Presets: `london bangalore mumbai delhi nyc sf tokyo`

## Web UI

`http://<mac-lan-ip>:8787` — map picker, address search, presets. Open it on
the phone; no cable needed once the Wi-Fi tunnel is up.

Leaflet + OpenStreetMap tiles; search proxies Nominatim through Flask so the
lookup carries a real User-Agent. Debounced to respect the ~1 req/sec limit.

### Auth

Accounts live in SQLite at `~/.local/state/loci/loci.db` (mode 600). Create one
before the server will bind anything but loopback:

```bash
.venv/bin/python server/app.py adduser pranav
.venv/bin/python server/app.py users
.venv/bin/python server/app.py revoke     # kill every session
```

- passwords hashed with scrypt; unknown usernames are still hashed so timing
  does not leak which accounts exist
- sessions are opaque 32-byte tokens in an HttpOnly, SameSite=Lax cookie;
  only their SHA-256 is stored, so the database cannot be replayed as a login
- 10 failed attempts from an IP locks it out for 15 minutes
- every set / clear / login lands in an `audit` table, readable at `/audit`

Binding a non-loopback address with no accounts is refused outright.

## Needs

- Developer Mode on — Settings → Privacy & Security
- iPhone paired and trusted (one USB connection, once)
- `loci-tunnel` running as root for Wi-Fi, or `tunneld` for the USB fallback

## Why a holder process

`simulate-location set` never returns. It holds the DVT session open and the
spoof dies with the process, so `loci set` backgrounds a holder and tracks its
PID. `loci clear` kills it, then clears explicitly.

## Wi-Fi

Works. Cable only needed for the first pairing.

Enable once, over USB:

```bash
pymobiledevice3 lockdown wifi-connections on
```

Then `loci-tunnel` (installed by `./install.sh wifi`) keeps a classic
RemotePairing tunnel alive and publishes its RSD address to
`~/.local/state/loci/rsd`. `loci` prefers that, falling back to USB via
`tunneld`. `loci status` prints which link is in use.

Discovery returns the same device 3x — LAN IPv4, global IPv6, link-local.
`loci-tunnel` ranks LAN IPv4 first; the global IPv6 fails the QUIC handshake
and link-local is scope-bound.

What does *not* work: the no-root native tunnel over Wi-Fi
(`Connection was invalidated`), and `tunneld`'s own Wi-Fi discovery, which
stays `{}`. Use the classic tunnel.

## Multiple devices

Identity is the **UDID**, not a MAC address — `get_remote_pairing_tunnel_services(udid=...)`
is the only filter available.

`./install.sh wifi` pins one device into the daemon's environment. It uses
`IPHONE_UDID` if set, else `~/.config/loci/config`, else auto-detects — but
only when discovery finds exactly one device. With two paired phones and no
pin it refuses rather than guessing, because ranking sorts on address shape
alone and both would tie at rank 0.

```bash
mkdir -p ~/.config/loci
echo 'IPHONE_UDID=00008110-XXXXXXXXXXXXXXXX' >> ~/.config/loci/config
./install.sh wifi
```

The CLI behaves the same way on its USB fallback.

## Reboots

**Mac reboot** — everything comes back on its own. `com.loci.tunnel` starts at
boot, but discovery goes through `remotepairingd`, a per-user service, so it
logs `no tunnel services discovered` until you log in and then recovers on the
10s retry. The spoof itself does not survive: the holder process dies and the
location reverts. Re-set it from the page.

**Phone reboot** — the Developer Disk Image unmounts, so the DVT services
disappear and `loci set` fails until it is mounted again
(`pymobiledevice3 mounter auto-mount`). This is the one case that may still
want the cable.

**`wifi-connections`** persists across both.

## Layout

```
bin/iphone-loc        CLI
bin/loci-tunnel       Wi-Fi tunnel supervisor (root)
server/app.py         Flask app
server/templates/     map picker UI
launchd/              LaunchDaemon + LaunchAgent templates
install.sh            CLI / wifi / daemon / server
```
