"""~/.config/loci/config: the one place settings live.

Read by the web server and the tunnel daemon; the CLI sources the same file.
KEY=value per line, `#` comments, optional quotes. An environment variable
beats the file, so a one-off run can override without editing it.
"""
from __future__ import annotations

import os
from pathlib import Path


def _load() -> dict[str, str]:
    path = Path(os.environ.get("LOCI_CONFIG") or (Path.home() / ".config/loci/config"))
    values: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


_VALUES = _load()


def setting(name: str, default=None):
    value = os.environ.get(name) or _VALUES.get(name)
    # Treat `KEY=` as unset. An empty string is not None, so it would slip past
    # loci-tunnel's refusal to guess between phones, and Path("") is the cwd.
    if not value:
        return default
    # Allow ~ in the file, since that is how people write paths by hand.
    return os.path.expanduser(value) if isinstance(value, str) else value


def state_dir() -> Path:
    return Path(setting("LOCI_STATE", Path.home() / ".local/state/loci"))
