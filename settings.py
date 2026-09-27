"""
settings.py - the handful of values the packager needs, editable in the browser.

API keys and the contact address used to live only in environment variables,
which meant restarting the app to change one and re-exporting them in every new
shell. They are now typed into the page and kept in a small file.

Where they are kept, and why not next to the code: `~/.config/` is outside the
repository, so a key cannot be committed by accident. The file is written
0600 - owner only.

Precedence is saved value first, then the environment. Anyone already exporting
these keeps working, and saving in the UI takes over from that point.

Nothing here is a secret store. It is a local convenience file on a machine you
control, in the same spirit as a `.netrc`. Keys are never written to the log,
and only ever sent back to the browser masked.
"""

import json
import os
import threading

APP_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "offline-map-downloader")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

#: Every value the UI may set, with a short label and whether it is a secret.
FIELDS = {
    "TILE_CONTACT":      ("Contact email", False),
    "GEOAPIFY_KEY":      ("Geoapify API key", True),
    "MAPTILER_KEY":      ("MapTiler API key", True),
    "THUNDERFOREST_KEY": ("Thunderforest API key", True),
    "STADIA_KEY":        ("Stadia Maps API key", True),
    "TILE_LOCAL_URL":    ("Self-hosted tile URL", False),
    "TILE_MBTILES":      ("Local .mbtiles path", False),
    "TILE_DIR":          ("Local tile directory", False),
    "OSRM_BASE_URL":     ("Driving router (OSRM)", False),
    "VALHALLA_BASE_URL": ("Valhalla router (walking, cycling, avoid)", False),
}

_lock = threading.Lock()
_cache = None


def _read():
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cache = {k: v for k, v in data.items() if k in FIELDS and isinstance(v, str)}
    except (OSError, ValueError):
        _cache = {}
    return _cache


def get(name, default=None):
    """Saved value, else the environment, else @p default."""
    saved = _read().get(name)
    if saved:
        return saved
    return os.environ.get(name) or default


def source_of(name):
    """Where the value in use came from, for the UI to show."""
    if _read().get(name):
        return "saved"
    if os.environ.get(name):
        return "environment"
    return "unset"


def set_many(values):
    """Save or clear several fields at once. An empty string removes one."""
    with _lock:
        data = dict(_read())
        for name, value in values.items():
            if name not in FIELDS:
                continue
            value = (value or "").strip()
            if value:
                data[name] = value
            else:
                data.pop(name, None)

        os.makedirs(APP_DIR, mode=0o700, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SETTINGS_PATH)   # atomic, so a crash cannot truncate it

        global _cache
        _cache = data
    return data


def mask(value, secret):
    """What the browser is allowed to see."""
    if not value:
        return ""
    if not secret:
        return value
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "…" + value[-4:]


def describe():
    """Every field, with a masked value and where it came from."""
    out = {}
    for name, (label, secret) in FIELDS.items():
        value = get(name) or ""
        out[name] = {
            "label": label,
            "secret": secret,
            "set": bool(value),
            "masked": mask(value, secret),
            "source": source_of(name),
        }
    return out
