# loci

Spoof GPS on a paired iPhone from macOS. iOS 17+, no jailbreak.

Setting up from scratch or adding a phone: **[SETUP.md](SETUP.md)**.

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

The CLI presets are fixed. The web UI keeps its own per-user list in SQLite —
see Saved locations below.

## Web UI

`http://<mac-lan-ip>:8787` — map picker, address search, presets. Open it on
the phone; no cable needed once the Wi-Fi tunnel is up.

MapLibre GL + [OpenFreeMap](https://openfreemap.org) vector tiles (`liberty`
style) — no API key, no rate limit. Vector rather than raster so labels stay
crisp at any zoom. The style declares no attribution, so it is added by hand.
MapLibre v6 ships ESM only (`dist/maplibre-gl.mjs`), hence the module import.

Search proxies Nominatim through Flask so the lookup carries a real
User-Agent. Debounced to respect the ~1 req/sec limit.

Styled with [govuk-frontend](https://github.com/alphagov/govuk-frontend) 6.5.1
from a CDN — components only, no Crown logo, no GOV.UK wordmark, no Crown
copyright. The font files are not shipped, so it falls back to Arial as GDS
Transport is licensed for government use only.

Pages follow the GDS one-thing-per-page pattern, with Service Navigation as
the menu: **Set location**, **Saved locations**, **Activity**. Note that v6
dropped `govuk-header__service-name` and `govuk-header__content` — the service
name lives in Service Navigation now, and using the v5 markup renders as bare
unstyled links.

### Saved locations

Per user, in SQLite. Seeded with seven defaults on first sign-in.

- tap a saved place to apply it straight away
- set an unsaved point and the page offers to name and keep it
- `/locations` lists them; add and edit are map pages, not latitude/longitude
  boxes; delete asks for confirmation on its own page
- **soft delete** — rows keep a `deleted_at` stamp and are never removed, so a
  location named in the audit log always resolves back to a name. There is no
  restore button; `store.restore()` exists if you ever need one back
- names are unique per user among live rows; deleting frees the name, and
  re-adding it reuses the old row rather than orphaning it
- coordinates match to 4 decimal places (~11 m) when deciding 'is this saved?'

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
server/app.py         Flask app and routes
server/auth.py        users, sessions, lockout, audit
server/store.py       saved locations, soft delete
server/templates/     base + page templates
server/static/        shared map/helper JS and the few non-GDS styles
launchd/              LaunchDaemon + LaunchAgent templates
install.sh            CLI / wifi / daemon / server
```

## License

MIT — see [LICENSE](LICENSE).
