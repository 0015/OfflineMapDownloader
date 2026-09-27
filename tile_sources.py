"""
tile_sources.py - where the navigation packager gets its tiles from.

Why this module exists
----------------------

`https://tile.openstreetmap.org` is run on donated hardware for people looking
at maps in a browser. Its usage policy forbids bulk downloading, and it enforces
that: once you trip it you start receiving "Access Blocked - App is not
following the tile usage policy" images instead of map tiles. Those get written
to the SD card looking like ordinary tiles, so the failure is silent until you
see it on the device.

There is no version of this tool that should try to get around that. Changing
the User-Agent or routing through proxies is evading the enforcement of a policy
that says, plainly, do not do this.

What there is instead is a choice of sources that permit exactly what we are
doing. Pick one and the problem disappears for good, along with the rate limits:

  self-hosted  Render the tiles yourself from an OSM extract. What the OSM
               Foundation itself points you at for anything like this, and the
               only option with no quota at all.
  mbtiles/dir  Point at a .mbtiles file or a folder you already have. Fully
               offline, nothing is fetched.
  keyed APIs   MapTiler, Thunderforest, Stadia, Geoapify. All have free tiers.
               Check what your plan says about caching and redistribution -
               it differs per provider and per plan, and it is your call.

Configuration goes through `settings`, which reads what was typed into the page
first and the environment second, so these still work when nothing is saved:

    export MAPTILER_KEY=...          # or THUNDERFOREST_KEY / STADIA_KEY /
                                     #    GEOAPIFY_KEY
    export TILE_LOCAL_URL='http://localhost:8080/styles/basic-preview/{z}/{x}/{y}.png'
    export TILE_MBTILES=/path/to/region.mbtiles
    export TILE_DIR=/path/to/tiles   # <dir>/{z}/{x}/{y}.png
    export TILE_CONTACT='you@example.com'   # goes in the User-Agent

Call `reload()` after the settings change; `app_nav` does that on every save.

Attribution is not optional for any of these. Each source carries the string it
requires, and the packager writes it into the SD card README.
"""

import os
import random
import sqlite3
import threading
import time

import requests

import settings


def user_agent():
    """Rebuilt on every call: the contact address is editable in the browser."""
    contact = settings.get("TILE_CONTACT") or "no contact set"
    return f"OfflineNavigationPackager/2.0 (+{contact})"


#: Kept as a module attribute for callers that just want the current string.
USER_AGENT = user_agent()

REQUEST_TIMEOUT = 15
MAX_RETRIES = 3


class TileSource:
    """A place tiles come from."""

    #: Whether this service's own terms allow pre-downloading a corridor.
    bulk_ok = True
    #: What those terms actually say, verbatim enough to act on.
    policy = None
    #: Cap the terms put on an offline cache, in bytes. None for no stated cap.
    bulk_limit_bytes = None
    #: Needs an API key or a path that has not been configured.
    unavailable_reason = None
    #: The settings field that configures this source, for the page to offer
    #: right under it. None for a source with nothing to set.
    setting = None

    def __init__(self, ident, label, attribution, max_zoom=19):
        self.id = ident
        self.label = label
        self.attribution = attribution
        self.max_zoom = max_zoom

    @property
    def available(self):
        return self.unavailable_reason is None

    def fetch(self, z, x, y):
        """Return image bytes, or None when the tile does not exist."""
        raise NotImplementedError

    def as_json(self):
        return {
            "id": self.id,
            "label": self.label,
            "attribution": self.attribution,
            "max_zoom": self.max_zoom,
            "bulk_ok": self.bulk_ok,
            "policy": self.policy,
            "bulk_limit_bytes": self.bulk_limit_bytes,
            "available": self.available,
            "reason": self.unavailable_reason,
            "setting": self.setting,
        }


class HttpTileSource(TileSource):
    """An XYZ endpoint, with an optional key substituted into the template."""

    def __init__(self, ident, label, attribution, url_template,
                 key_env=None, url_env=None, max_zoom=19, bulk_ok=True, note=None,
                 policy=None, bulk_limit_bytes=None):
        super().__init__(ident, label, attribution, max_zoom)
        self.url_template = url_template
        self.key_env = key_env
        self.url_env = url_env
        self.bulk_ok = bulk_ok
        self.note = note
        self.policy = policy
        self.bulk_limit_bytes = bulk_limit_bytes

        self.setting = key_env or url_env
        self.key = settings.get(key_env) if key_env else None
        if key_env and not self.key:
            self.unavailable_reason = (
                f"enter the API key under the tile source, or set {key_env} "
                f"in the environment")
        elif url_env and not settings.get(url_env):
            # Without this a built-in localhost fallback would look configured
            # and the UI would preselect a tile server that is not running.
            self.unavailable_reason = (
                f"enter a URL under the tile source, e.g. "
                f"'http://localhost:8080/styles/basic-preview/{{z}}/{{x}}/{{y}}.png'")

        # One polite gap between requests, shared across the worker threads.
        self._gate = threading.Lock()
        self._next_allowed = 0.0
        self.min_interval = 0.0 if key_env else 0.1

    def _throttle(self):
        if self.min_interval <= 0:
            return
        with self._gate:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self.min_interval

    def url_for(self, z, x, y):
        return self.url_template.format(z=z, x=x, y=y, key=self.key or "")

    def fetch(self, z, x, y):
        url = self.url_for(z, x, y)
        headers = {"User-Agent": user_agent()}

        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.RequestException:
                if attempt == MAX_RETRIES - 1:
                    return None
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                continue

            if r.status_code == 200:
                return r.content
            if r.status_code in (404, 204):
                return None                     # genuinely no tile here
            if r.status_code in (401, 403):
                raise TileSourceRefused(
                    f"{self.label} refused the request (HTTP {r.status_code}). "
                    f"Check the API key and what your plan allows.")
            if r.status_code == 429:
                time.sleep((2 ** attempt) + random.uniform(0, 1))
                continue
            if attempt == MAX_RETRIES - 1:
                return None
            time.sleep((2 ** attempt) + random.uniform(0, 0.5))

        return None


class MBTilesSource(TileSource):
    """An .mbtiles file. Nothing is fetched; nothing can be rate limited."""

    def __init__(self, ident, label, path_env):
        super().__init__(ident, label, "See the source of your .mbtiles", 22)
        self.policy = "A file you already hold. Nothing is fetched."
        self.setting = path_env
        self.path = settings.get(path_env)
        if not self.path:
            self.unavailable_reason = ("enter the path to a raster .mbtiles file "
                                       "under the tile source")
        elif not os.path.exists(self.path):
            self.unavailable_reason = f"{self.path} does not exist"
        self._local = threading.local()

        if self.available:
            self.unavailable_reason = self._reject_if_vector()

    def _reject_if_vector(self):
        """
        Most .mbtiles published today hold vector tiles, not pictures.

        The device draws RGB565 bitmaps, so a vector set cannot be used here
        however it is converted in this tool. Say so up front rather than
        failing 400 tiles into a build.
        """
        try:
            conn = sqlite3.connect(self.path)
            fmt = None
            try:
                row = conn.execute(
                    "SELECT value FROM metadata WHERE name='format'").fetchone()
                fmt = (row[0] or "").lower() if row else None
            except sqlite3.Error:
                pass                                # no metadata table

            if fmt in ("pbf", "mvt"):
                conn.close()
                return (f"{os.path.basename(self.path)} holds vector tiles "
                        f"(format={fmt}). Serve it with tileserver-gl and use the "
                        f"self-hosted source instead - see README_NAV.md")

            if fmt is None:
                # Sniff a blob: PNG starts 89 50 4E 47, JPEG FF D8 FF, and a
                # gzipped vector tile starts 1F 8B.
                row = conn.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
                if row and row[0]:
                    head = bytes(row[0])[:4]
                    if head[:2] == b"\x1f\x8b":
                        conn.close()
                        return (f"{os.path.basename(self.path)} looks like gzipped "
                                f"vector tiles. Serve it with tileserver-gl and use "
                                f"the self-hosted source instead")
            conn.close()
        except sqlite3.Error as e:
            return f"{self.path} is not a readable SQLite database ({e})"

        return None

    def _conn(self):
        # SQLite connections are not shareable across threads.
        if getattr(self._local, "conn", None) is None:
            self._local.conn = sqlite3.connect(self.path, check_same_thread=False)
        return self._local.conn

    def fetch(self, z, x, y):
        # MBTiles stores rows bottom-up (TMS); XYZ counts from the top.
        tms_y = (2 ** z - 1) - y
        cur = self._conn().execute(
            "SELECT tile_data FROM tiles "
            "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, tms_y))
        row = cur.fetchone()
        return bytes(row[0]) if row else None


class DirectorySource(TileSource):
    """A folder already laid out as <dir>/{z}/{x}/{y}.<ext>."""

    def __init__(self, ident, label, path_env):
        super().__init__(ident, label, "See the source of your tiles", 22)
        self.policy = "Tiles you already hold. Nothing is fetched."
        self.setting = path_env
        self.root = settings.get(path_env)
        if not self.root:
            self.unavailable_reason = ("enter the path to a tile directory "
                                       "under the tile source")
        elif not os.path.isdir(self.root):
            self.unavailable_reason = f"{self.root} is not a directory"

    def fetch(self, z, x, y):
        for ext in ("png", "jpg", "jpeg", "webp"):
            p = os.path.join(self.root, str(z), str(x), f"{y}.{ext}")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return f.read()
        return None


class TileSourceRefused(Exception):
    """The service declined, and retrying will not help."""


OSM_ATTRIBUTION = "(c) OpenStreetMap contributors"


def build_registry():
    """
    Every source, in the order the UI should offer them.

    The `policy` line on each one is what that service's own terms say about
    pre-downloading tiles for offline use. They differ a great deal and several
    of the obvious names say no, so read it before picking. Checked against the
    published terms in September 2026; terms change, so re-check yours.
    """
    sources = [
        HttpTileSource(
            "local", "Self-hosted tile server",
            OSM_ATTRIBUTION + " - rendered locally",
            settings.get("TILE_LOCAL_URL") or "http://localhost:8080/tile/{z}/{x}/{y}.png",
            url_env="TILE_LOCAL_URL", max_zoom=20,
            policy="Your server, your rules. Keep the ODbL attribution.",
            note="No quota and no rate limit. What the OSM Foundation points you "
                 "at for bulk use.",
        ),
        MBTilesSource("mbtiles", "Local .mbtiles file", "TILE_MBTILES"),
        DirectorySource("dir", "Local tile directory", "TILE_DIR"),

        HttpTileSource(
            "geoapify", "Geoapify OSM Bright",
            "Powered by Geoapify, " + OSM_ATTRIBUTION,
            "https://maps.geoapify.com/v1/tile/osm-bright/{z}/{x}/{y}.png?apiKey={key}",
            key_env="GEOAPIFY_KEY", max_zoom=20,
            policy="Allows caching, storing and redistributing generated tiles. "
                   "The most permissive of the keyed providers for this use."),
        HttpTileSource(
            "geoapify-positron", "Geoapify Positron",
            "Powered by Geoapify, " + OSM_ATTRIBUTION,
            "https://maps.geoapify.com/v1/tile/positron/{z}/{x}/{y}.png?apiKey={key}",
            key_env="GEOAPIFY_KEY", max_zoom=20,
            policy="Allows caching, storing and redistributing generated tiles."),

        HttpTileSource(
            "stadia", "Stadia Outdoors",
            "(c) Stadia Maps (c) OpenMapTiles " + OSM_ATTRIBUTION,
            "https://tiles.stadiamaps.com/tiles/outdoors/{z}/{x}/{y}.png?api_key={key}",
            key_env="STADIA_KEY", max_zoom=20,
            # The terms say 100MB; counted in decimal megabytes, so a build
            # the size of the cap never ends up over it.
            bulk_limit_bytes=100 * 1000 * 1000,
            policy="Bulk download is allowed only to cache up to 100 MB per "
                   "device for offline use in a mobile application - check "
                   "that yours fits. Larger builds are refused here."),

        HttpTileSource(
            "thunderforest", "Thunderforest Atlas",
            "Maps (c) Thunderforest, Data " + OSM_ATTRIBUTION,
            "https://tile.thunderforest.com/atlas/{z}/{x}/{y}.png?apikey={key}",
            key_env="THUNDERFOREST_KEY", max_zoom=22,
            policy="Pre-downloading needs the Small Business plan or higher. "
                   "Explicitly prohibited below that."),

        HttpTileSource(
            "maptiler", "MapTiler streets", "(c) MapTiler " + OSM_ATTRIBUTION,
            "https://api.maptiler.com/maps/streets-v2/{z}/{x}/{y}.png?key={key}",
            key_env="MAPTILER_KEY", max_zoom=20, bulk_ok=False,
            policy="MapTiler Cloud terms prohibit batch or bulk tile download "
                   "and prohibit exporting map content for use outside the "
                   "Service. Only a temporary personal cache is allowed.",
            note="Needs a written agreement with MapTiler, or MapTiler Server "
                 "for self-hosting. With one, set bulk_ok=True here."),
        HttpTileSource(
            "maptiler-sat", "MapTiler satellite", "(c) MapTiler " + OSM_ATTRIBUTION,
            "https://api.maptiler.com/tiles/satellite-v2/{z}/{x}/{y}.jpg?key={key}",
            key_env="MAPTILER_KEY", max_zoom=20, bulk_ok=False,
            policy="Same MapTiler Cloud terms as above.",
            note="Needs a written agreement with MapTiler, or MapTiler Server."),

        HttpTileSource(
            "osm", "OpenStreetMap standard", OSM_ATTRIBUTION,
            "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            max_zoom=19, bulk_ok=False,
            policy="The OSM tile usage policy forbids bulk downloading, and the "
                   "servers enforce it by returning 'Access Blocked' images.",
            note="Browsing only."),
    ]
    return {s.id: s for s in sources}


REGISTRY = build_registry()


def reload():
    """Rebuild after the settings changed, so a key typed in the browser takes
    effect without restarting the app."""
    global REGISTRY, USER_AGENT
    REGISTRY = build_registry()
    USER_AGENT = user_agent()
    return REGISTRY


def get(source_id):
    return REGISTRY.get(source_id)


def default_source_id():
    """First source that is both configured and allowed for bulk use."""
    for s in REGISTRY.values():
        if s.available and s.bulk_ok:
            return s.id
    return "local"
