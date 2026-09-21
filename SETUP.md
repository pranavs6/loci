# Setup

From nothing to a spoofing iPhone. Roughly ten minutes, most of it waiting on
downloads and one phone reboot.

Order matters in two places: Developer Mode needs a reboot before anything
else works, and `wifi-connections` has to be enabled over USB before the
cable can come out.

---

## 1. Python

**This is the step that breaks everything if you get it wrong**, so it comes
first. The Wi-Fi tunnel does a TLS-PSK handshake, which needs Python 3.13+
for native `ssl` PSK support — and it must not be Homebrew's build.

```bash
brew install uv
uv tool install pymobiledevice3 --python 3.13
```

Check it:

```bash
~/.local/share/uv/tools/pymobiledevice3/bin/python -c \
  'import ssl,sys; print(sys.version.split()[0], hasattr(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),"set_psk_client_callback"))'
# want: 3.13.x True
```

Why not the obvious routes:

| route | what happens |
|---|---|
| `pipx install pymobiledevice3` | defaults to 3.11 → falls back to the `sslpsk_pmd3` shim → `NO_CIPHERS_AVAILABLE` |
| Homebrew `python@3.13` | broken `pyexpat` (resolves to `/usr/lib/libexpat.1.dylib`, missing `_XML_SetAllocTrackerActivationThreshold`) → breaks `plistlib` → `platform.mac_ver()` returns `''` → pip's truststore dies on `int('')` |
| uv's standalone build | works |

## 2. The iPhone

Plug it into the Mac with a **data** cable — plenty of USB-C cables are
charge-only and the phone will simply not appear.

1. Unlock, tap **Trust This Computer**
2. **Settings → Privacy & Security → Developer Mode → on**, then reboot the
   phone and confirm after it restarts
3. Unlock again and confirm it is visible:

```bash
pymobiledevice3 usbmux list
```

Then enable network connections, **while still plugged in**:

```bash
pymobiledevice3 lockdown wifi-connections on
```

Note the `UniqueDeviceID` from the `usbmux list` output — you need it next.

## 3. Pin the device

One phone per daemon. `./install.sh wifi` auto-detects and writes the UDID to
the config for you when exactly one device is discoverable. With more than one
paired, set it yourself first:

```bash
mkdir -p ~/.config/loci
echo 'IPHONE_UDID=00008110-XXXXXXXXXXXXXXXX' >> ~/.config/loci/config
```

## Configuration

Everything lives in one file, `~/.config/loci/config`, read by the CLI, the
tunnel daemon and the web server alike. `KEY=value`, one per line, `#` for
comments. Edit it and restart the affected job — nothing is baked into the
launchd plists.

```ini
IPHONE_UDID=00008110-XXXXXXXXXXXXXXXX   # which phone. Required with >1 paired
LOCI_HOST=0.0.0.0                       # 127.0.0.1 to keep the UI off the LAN
LOCI_PORT=8787
LOCI_STATE=~/.local/state/loci          # rsd, holder pid, loci.db
LOCI_BIN=~/.local/bin/loci              # CLI the web server shells out to
LOCI_DB=~/.local/state/loci/loci.db     # override to move just the database
PMD=~/.local/bin/pymobiledevice3
TUNNELD=http://127.0.0.1:49151          # USB fallback only
```

| setting | CLI | tunnel daemon | web server |
|---|---|---|---|
| `IPHONE_UDID` | ✓ | ✓ | — |
| `LOCI_STATE` | ✓ | ✓ | ✓ |
| `LOCI_HOST` / `LOCI_PORT` | — | — | ✓ |
| `LOCI_BIN` / `LOCI_DB` | — | — | ✓ |
| `PMD` / `TUNNELD` | ✓ | — | — |

An environment variable beats the file, so a one-off run can override without
editing anything:

```bash
LOCI_HOST=127.0.0.1 .venv/bin/python server/app.py
```

Restart after editing:

```bash
sudo launchctl kickstart -k system/com.loci.tunnel
launchctl kickstart -k gui/$UID/com.loci.server
```

## 4. Install

```bash
git clone <this repo> && cd loci
./install.sh          # CLI at ~/.local/bin/loci
./install.sh wifi     # root LaunchDaemon: keeps the Wi-Fi tunnel alive (sudo)
./install.sh server   # login LaunchAgent: the web UI
```

Create an account — the server refuses to bind anything but loopback without
one:

```bash
.venv/bin/python server/app.py adduser <name>
```

You can now unplug the cable.

## 5. Check it

```bash
loci status                      # want: not spoofed [wifi]
cat ~/.local/state/loci/rsd      # the tunnel's RSD address
loci set london && loci status   # want: SPOOFED: ... [wifi]
loci clear
```

Open `http://<mac-lan-ip>:8787` on the phone. `ipconfig getifaddr en0` gives
the address.

---

## Requirements that do not go away

- **The Mac and the iPhone must share an L2 segment.** Discovery is mDNS and
  the tunnel connects to the phone's LAN IPv4. A VPN between them does not
  work: it is L3, and multicast does not cross it.
- **The Mac must be awake and running the daemons.** A sleeping laptop is a
  dropped tunnel.
- **The spoof is a live process.** It dies with the tunnel, and clears on
  reboot. Re-apply from the UI.

## When it stops working

| symptom | cause |
|---|---|
| `error: no iPhone reachable` | tunnel down. `cat ~/.local/state/loci/rsd` — missing means no tunnel. Check `/var/log/loci-tunnel.err` |
| `no tunnel services discovered`, repeatedly | phone asleep, locked or off Wi-Fi. The daemon exits after 30 rounds so launchd restarts it clean |
| worked, then stopped after a phone reboot | the Developer Disk Image unmounts on phone restart. `pymobiledevice3 mounter auto-mount`, cable may be needed |
| `NO_CIPHERS_AVAILABLE` | wrong Python. See step 1 |
| `QUIC protocol error` | the tunnel picked a global IPv6. `loci-tunnel` ranks LAN IPv4 first; this is only seen running `start-tunnel` by hand |
| phone invisible over USB | charge-only cable, or Developer Mode off |
| web UI unreachable | the Mac's DHCP lease moved. Re-check `ipconfig getifaddr en0` |

Restart a daemon:

```bash
sudo launchctl kickstart -k system/com.loci.tunnel    # tunnel
launchctl kickstart -k gui/$UID/com.loci.server       # web UI
```

Restarting the web agent **kills any active spoof** — the holder process is a
child of that job.

## Where things live

```
~/.local/bin/loci                CLI (symlink into the repo)
~/.local/state/loci/rsd          current tunnel address, written by the daemon
~/.local/state/loci/holder.pid   the process holding the spoof open
~/.local/state/loci/loci.db      users, sessions, saved locations, audit, geocache
~/.config/loci/config            IPHONE_UDID pin
/var/log/loci-tunnel.{log,err}   tunnel daemon
~/Library/Logs/loci-server.{log,err}   web UI
```

## A second iPhone

Repeat step 2 for the new phone, then change `IPHONE_UDID` in
`~/.config/loci/config` and restart the tunnel daemon — no reinstall needed,
since the plist no longer carries the UDID:

```bash
sudo launchctl kickstart -k system/com.loci.tunnel
```

One daemon serves one device. Two phones at once means two jobs with different
labels and state directories.
