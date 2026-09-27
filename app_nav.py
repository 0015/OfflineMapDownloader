"""
Offline Navigation Packager (OfflineMapDownloader v2)
-----------------------------------------------------

`app.py` answers "give me every tile in this rectangle". That is the right tool
for a map viewer and the wrong one for navigation: a 20 km route inside its own
bounding box is mostly tiles you will never see, and it still leaves you to
produce turn instructions by hand.

This app is the navigation half:

  * plan a route through waypoints - with OSRM when driving, and with Valhalla
    when walking or cycling, so those take footpaths, trails and cycleways,
  * select only the tiles within a corridor around that route,
  * convert them straight to the LVGL v9 RGB565 `.bin` the device reads
    (no separate run of `lvgl_map_tile_converter.py`),
  * emit `route.bin` with geometry, per-zoom level of detail and turn
    instructions, in the format `map_route.c` reads,
  * hand back one zip you unpack onto the SD card.

With no route at all it builds the same card from every tile in a rectangle
instead - a map on its own, which the device opens without anything to follow.

`app.py` is untouched and still does what it always did; run this one with
`python app_nav.py` (port 5001) and both can be up at once.

Notice
------
Routes are planned by the public OSRM demo server and, for walking, cycling and
routes that avoid tolls or highways, the public FOSSGIS Valhalla server - both
for personal and educational use, and neither a production backend. Tiles never
come from the public OpenStreetMap servers; see tile_sources.py. If you are
doing this at any scale, host your own.
"""

import io
import math
import os
import random
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import hashlib
import re

import requests
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image

import route_format as rf
import settings
import tile_sources

app = Flask(__name__)

# Driving goes to OSRM, and nothing else does. The public demo at
# router.project-osrm.org has just the car profile deployed: asked for /bike or
# /foot it answers 200 with the very same car route - across Central Park, 1.9
# km on foot in four minutes, round the transverse road. And an osrm-routed
# serves whichever single profile its data was built with, so even a
# self-hosted one needs an instance per profile before the name in the URL
# means anything. Walking and cycling go to Valhalla instead; see PROFILES.
OSRM_DEMO_BASE = "https://router.project-osrm.org"


def osrm_base():
    """Read on every call, since it is editable in the browser."""
    return (settings.get("OSRM_BASE_URL") or OSRM_DEMO_BASE).rstrip("/")


def osrm_is_demo():
    return osrm_base() == OSRM_DEMO_BASE


# Valhalla plans everything OSRM cannot.
#
# Walking and cycling, because its pedestrian and bicycle models know the
# footpaths, park paths, steps, trails and cycleways a car route never touches.
#
# And driving that avoids tolls or highways. Neither public OSRM server builds
# its car profile with excludable classes - every `exclude=` is answered with
# InvalidValue - and even where one does, a hard exclude fails outright as soon
# as a waypoint sits on a toll road. Valhalla's use_tolls and use_highways are
# penalties: it goes round where there is a reasonable way round and still
# arrives where there is not.
#
# Asked for `format=osrm` it answers in OSRM's shape, so route_from_osrm reads
# it unchanged.
VALHALLA_DEMO_BASE = "https://valhalla1.openstreetmap.de"


def valhalla_base():
    """Read on every call, since it is editable in the browser."""
    return (settings.get("VALHALLA_BASE_URL") or VALHALLA_DEMO_BASE).rstrip("/")


# What the UI offers to avoid: the Valhalla costing option that penalises it,
# the intersection class that shows the route used it anyway, and how to say so.
AVOID = {
    "tolls":    ("use_tolls",    "toll",     "a toll road"),
    "highways": ("use_highways", "motorway", "a highway"),
}

# What the UI calls a profile, and the Valhalla costing model for it. Driving
# only reaches Valhalla when something is being avoided; otherwise it is OSRM.
PROFILES = {
    "driving": "auto",
    "cycling": "bicycle",
    "walking": "pedestrian",
}

# The hardest hiking trail a walking route may use, on OSM's sac_scale: 1
# hiking, 2 mountain hiking, 3 demanding mountain hiking, and up to 6 for
# alpine routes. Valhalla's own default is 1, which leaves out every trail
# tagged as mountain hiking; 2 lets those in without sending anyone up a
# scramble. Only the "trails" and "shortest" styles raise it.
TRAIL_MAX_SAC_SCALE = 2

# How a walking or cycling route trades distance against everything else, as
# Valhalla costing options per costing model. Driving has AVOID instead.
STYLES = {
    # The router's own judgement. Footpaths, park paths, steps and cycleways
    # are already fair game, weighed against hills, surfaces and traffic.
    "balanced": {},
    # Off the street wherever there is a reasonable path, even a longer one.
    "trails": {
        "pedestrian": {
            "walkway_factor": 0.5,        # footways, paths and trails cost half,
            "sidewalk_factor": 2.0,       # a pavement beside traffic twice
            "use_tracks": 1.0,            # unpaved tracks are welcome
            "max_hiking_difficulty": TRAIL_MAX_SAC_SCALE,
        },
        "bicycle": {
            "bicycle_type": "Mountain",   # dirt and rough surfaces are rideable
            "use_roads": 0.0,             # stay off roads shared with traffic
            "avoid_bad_surfaces": 0.0,
        },
    },
    # Distance and nothing else: every cut-through the mode may legally use.
    "shortest": {
        "pedestrian": {"shortest": True,
                       "max_hiking_difficulty": TRAIL_MAX_SAC_SCALE},
        "bicycle": {"shortest": True},
    },
}


def slugify(text, fallback="route"):
    """A filename the device can list and a human can still recognise."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug or fallback)[:40]


CACHE_DIR = "tiles_v2"             # per-source cache of the original images
ABSENT_CACHE_SECONDS = 7 * 24 * 3600   # remember "no such tile" for a week
MAX_TILE_COUNT = 20000
MAX_WORKERS = 4
TILE_PX = 256
TILE_BIN_BYTES = TILE_PX * TILE_PX * 2 + 12

# A tile server that has decided it does not like you tends to answer with an
# error image rather than an error code, and that image lands on the SD card
# looking like any other tile. Identical bytes repeating across many different
# coordinates is the signature - unless the tile is one flat colour, which is
# what open sea and empty land look like; see is_flat().
BLOCKED_MIN_SAMPLES = 20
BLOCKED_ABORT_RATIO = 0.5

routes = {}     # route_id -> planned route
jobs = {}       # job_id   -> build progress


# ------------------------------------------------------------------ geo utils

def deg2num(lat, lon, zoom):
    n = 2.0 ** zoom
    lat_rad = math.radians(lat)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def meters_per_pixel(lat, zoom):
    return (2 * math.pi * rf.EARTH_RADIUS_M * math.cos(math.radians(lat))) / (TILE_PX * 2 ** zoom)


def tiles_along_route(coords, zoom, corridor_m):
    """
    Every tile within `corridor_m` of the route at this zoom.

    Walking the line at half-tile steps and stamping a square around each sample
    is far cheaper than a true buffer polygon, and since the result is rounded
    up to whole tiles anyway the difference never shows.
    """
    tiles = set()
    if not coords:
        return tiles

    mpp = meters_per_pixel(coords[len(coords) // 2][0], zoom)
    radius_tiles = max(1, math.ceil((corridor_m / mpp) / TILE_PX))
    step_px = TILE_PX / 2.0

    def stamp(lat, lon):
        tx, ty = deg2num(lat, lon, zoom)
        for dx in range(-radius_tiles, radius_tiles + 1):
            for dy in range(-radius_tiles, radius_tiles + 1):
                tiles.add((tx + dx, ty + dy))

    stamp(*coords[0])
    for (lat1, lon1), (lat2, lon2) in zip(coords, coords[1:]):
        x1, y1 = rf.world_px(lat1, lon1, zoom)
        x2, y2 = rf.world_px(lat2, lon2, zoom)
        seg_px = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, int(seg_px / step_px))
        for i in range(1, steps + 1):
            t = i / steps
            stamp(lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t)

    n = 2 ** zoom
    return {(x % n, y) for x, y in tiles if 0 <= y < n}


# Web Mercator stops here; deg2num has no answer past it.
MAX_LAT = 85.05112878


def tiles_in_area(area, zoom):
    """Every tile the rectangle touches at this zoom - no corridor, no margin."""
    n = 2 ** zoom
    x1, y1 = deg2num(min(area["north"], MAX_LAT), area["west"], zoom)
    x2, y2 = deg2num(max(area["south"], -MAX_LAT), area["east"], zoom)
    x1, x2 = max(0, x1), min(n - 1, x2)       # lon 180 is tile n, one past the end
    y1, y2 = max(0, y1), min(n - 1, y2)
    return {(x, y) for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)}


# --------------------------------------------------------------- tile fetching

def fetch_tile(source, z, x, y):
    """
    Fetch one tile through a source, with a per-source disk cache.

    Absences are cached too, for a while. A corridor always clips a few tiles
    the server has nothing for - sea, or past the edge of the provider's
    coverage - and without this every rebuild of the same route spends an API
    call on each of them all over again.
    """
    base = _cache_dir(source, z, x)
    path = os.path.join(base, f"{y}.img")
    if os.path.exists(path):
        return open(path, "rb").read()

    absent = os.path.join(base, f"{y}.absent")
    if os.path.exists(absent):
        if time.time() - os.path.getmtime(absent) < ABSENT_CACHE_SECONDS:
            return None
        os.remove(absent)        # old enough to be worth asking again

    data = source.fetch(z, x, y)
    os.makedirs(base, exist_ok=True)
    if data:
        with open(path, "wb") as f:
            f.write(data)
    else:
        open(absent, "wb").close()
    return data


def _cache_dir(source, z, x):
    """Keyed by source, not by URL: repointing a source means clearing it."""
    return os.path.join(CACHE_DIR, source.id, str(z), str(x))


def forget_tile(source, z, x, y):
    """Drop a cached tile, so the next build asks the server for it again."""
    try:
        os.remove(os.path.join(_cache_dir(source, z, x), f"{y}.img"))
    except FileNotFoundError:
        pass


def is_flat(image_bytes):
    """
    One colour and nothing else: open sea, empty land, a tile with no data.

    Those legitimately come back byte-identical for many coordinates - an area
    on the coast can be half sea. An error picture has writing on it, so it is
    never one colour.
    """
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return False
    return im.getcolors(1) is not None


def image_to_lvgl_bin(image_bytes):
    """
    PNG or JPEG -> the 12-byte-header RGB565 blob `map_tile_cache.c` expects.

    Byte-identical to what `script/lvgl_map_tile_converter.py` produces, just
    done in memory so there is no intermediate folder of images to convert.
    """
    im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if im.size != (TILE_PX, TILE_PX):
        im = im.resize((TILE_PX, TILE_PX))

    w, h = im.size
    header = bytes([0x19, 0x12]) + (0).to_bytes(2, "little") \
        + w.to_bytes(2, "little") + h.to_bytes(2, "little") \
        + (w * 2).to_bytes(2, "little") + (0).to_bytes(2, "little")

    body = bytearray(w * h * 2)
    px = im.load()
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            body[i] = v & 0xFF
            body[i + 1] = v >> 8
            i += 2

    return header + bytes(body)


# ------------------------------------------------------------------- endpoints

@app.route("/")
def index():
    return render_template("nav_index.html")


class RoutingError(Exception):
    """A routing failure worth showing as it is, with the HTTP status for it."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def _route_osrm(waypoints):
    """A car route. OSRM is only ever asked for driving; see OSRM_DEMO_BASE."""
    coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    url = f"{osrm_base()}/route/v1/driving/{coord_str}"

    try:
        r = requests.get(url, params={
            "overview": "full",
            "geometries": "geojson",
            "steps": "true",
        }, headers={"User-Agent": tile_sources.user_agent()}, timeout=25)
    except requests.RequestException as e:
        raise RoutingError(f"Could not reach the routing server: {e}", 502)

    if r.status_code != 200:
        raise RoutingError(f"Routing server returned HTTP {r.status_code}", 502)

    payload = r.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RoutingError(f"No route found ({payload.get('code')})", 400)
    return payload["routes"][0]


def _valhalla_costing(profile, avoid, style):
    """The Valhalla costing model for this request, and its options."""
    costing = PROFILES[profile]
    if profile == "driving":
        return costing, {AVOID[a][0]: 0 for a in avoid}
    return costing, dict(STYLES[style].get(costing, {}))


def _route_valhalla(waypoints, costing, options):
    """A route for the @p costing model, in OSRM's shape."""
    body = {
        "locations": [{"lat": lat, "lon": lon} for lat, lon in waypoints],
        "costing": costing,
        "costing_options": {costing: options},
        "format": "osrm",
        "shape_format": "geojson",
    }
    try:
        r = requests.post(f"{valhalla_base()}/route", json=body,
                          headers={"User-Agent": tile_sources.user_agent()}, timeout=25)
    except requests.RequestException as e:
        raise RoutingError(f"Could not reach the Valhalla routing server: {e}", 502)
    try:
        payload = r.json()
    except ValueError:
        raise RoutingError(f"Valhalla routing server returned HTTP {r.status_code}", 502)

    if payload.get("code") == "Ok" and payload.get("routes"):
        return payload["routes"][0]
    # Valhalla says why there is no route, in OSRM's `message` or its own `error`.
    reason = payload.get("message") or payload.get("error") or f"HTTP {r.status_code}"
    raise RoutingError(f"No route found ({reason})", 400 if r.status_code == 400 else 502)


def _still_uses(osrm_route, avoid):
    """
    Which of @p avoid the route could not stay off.

    A penalty is not a ban: when the only way in is a toll road, Valhalla takes
    it. Every intersection lists the classes of the road leaving it, so this is
    where that shows.
    """
    used = set()
    for leg in osrm_route.get("legs", []):
        for step in leg.get("steps", []):
            for crossing in step.get("intersections", []):
                used.update(crossing.get("classes", []))
    return [a for a in avoid if AVOID[a][1] in used]


@app.route("/api/plan", methods=["POST"])
def api_plan():
    data = request.get_json(force=True)
    waypoints = list(data.get("waypoints") or [])
    profile = data.get("profile", "driving")
    name = (data.get("name") or "").strip()
    kind = data.get("kind", "drive")
    close_loop = bool(data.get("close_loop"))
    wanted = set(data.get("avoid") or [])
    style = data.get("style") or "balanced"

    if profile not in PROFILES:
        return jsonify({"error": f"Unknown profile {profile}"}), 400
    if kind not in ("drive", "exercise"):
        return jsonify({"error": f"Unknown route kind {kind}"}), 400
    if wanted - set(AVOID):
        return jsonify({"error": f"Unknown avoid option(s): "
                                 f"{', '.join(sorted(wanted - set(AVOID)))}"}), 400
    if wanted and profile != "driving":
        return jsonify({"error": "Avoiding tolls and highways only applies "
                                 "to driving."}), 400
    if style not in STYLES:
        return jsonify({"error": f"Unknown route style {style}"}), 400
    if style != "balanced" and profile == "driving":
        return jsonify({"error": "Route styles apply to walking and cycling; "
                                 "driving has the avoid switches."}), 400
    avoid = [a for a in AVOID if a in wanted]

    if close_loop:
        # A circuit is just a route that is told to come back. Every waypoint in
        # between stays a via point, so the ride goes the way you drew it rather
        # than the way the router would have preferred.
        if len(waypoints) < 2:
            return jsonify({"error": "A loop needs a start and at least one "
                                     "point to go round."}), 400
        waypoints = waypoints + [waypoints[0]]
    elif len(waypoints) < 2:
        return jsonify({"error": "Pick at least a start and a destination."}), 400

    try:
        if profile == "driving" and not avoid:
            osrm_route, router = _route_osrm(waypoints), "osrm"
        else:
            costing, options = _valhalla_costing(profile, avoid, style)
            osrm_route, router = _route_valhalla(waypoints, costing, options), "valhalla"
    except RoutingError as e:
        return jsonify({"error": str(e)}), e.status

    coords, maneuvers, duration_s = rf.route_from_osrm(osrm_route)

    warning = None
    unavoided = _still_uses(osrm_route, avoid)
    if unavoided:
        warning = (f"Part of this route is still on "
                   f"{' and '.join(AVOID[a][2] for a in unavoided)}: "
                   f"there is no reasonable way round it here.")

    loop = close_loop or rf.is_closed_loop(coords)

    route_id = str(uuid.uuid4())
    routes[route_id] = {
        "coords": coords,
        "maneuvers": maneuvers,
        "duration_s": duration_s,
        "name": name or ("Circuit" if loop else "Route"),
        "distance_m": int(osrm_route.get("distance", 0)),
        "kind": kind,
        "loop": loop,
        "slug": slugify(name, "circuit" if loop else "route"),
        "profile": profile,
        "style": style,
        "avoid": avoid,
        "router": router,
    }

    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]

    return jsonify({
        "route_id": route_id,
        "geometry": [[lat, lon] for lat, lon in coords],
        "distance_m": routes[route_id]["distance_m"],
        "duration_s": duration_s,
        "point_count": len(coords),
        "kind": kind,
        "loop": loop,
        "slug": routes[route_id]["slug"],
        "profile": profile,
        "style": style,
        "avoid": avoid,
        "router": router,
        "warning": warning,
        "bounds": [[min(lats), min(lons)], [max(lats), max(lons)]],
        "steps": [
            {
                "index": m["point_index"],
                "type": m["type"],
                "street": m["street"],
                "lat": coords[m["point_index"]][0],
                "lon": coords[m["point_index"]][1],
            }
            for m in maneuvers
        ],
    })


def _parse_area(data):
    """
    The rectangle a tiles-only build covers, and its name, or an error.

    An area is sent with every request rather than registered like a route:
    there is nothing to plan, so there is nothing for the server to keep.
    """
    a = data.get("area") or {}
    try:
        south, west, north, east = (float(a[k]) for k in ("south", "west", "north", "east"))
    except (KeyError, TypeError, ValueError):
        return None, "Draw the area on the map first."
    if not (-MAX_LAT <= south < north <= MAX_LAT) or not (-180.0 <= west < east <= 180.0):
        return None, ("That area does not fit on the map. Draw it again, "
                      "without crossing the 180th meridian.")

    name = (data.get("name") or "").strip()
    return {"south": south, "west": west, "north": north, "east": east,
            "name": name or "Area", "slug": slugify(name, "area")}, None


def _target(data):
    """What a request builds: (route, area, error), exactly one of the first two."""
    if data.get("area") is not None:
        area, error = _parse_area(data)
        return None, area, error
    route = routes.get(data.get("route_id"))
    if not route:
        return None, None, "Plan a route first."
    return route, None, None


def _selected_tiles(route, area, zooms, corridor_m):
    """Per zoom, a corridor round the route, or every tile the area touches."""
    if area:
        return {z: tiles_in_area(area, z) for z in zooms}
    return {z: tiles_along_route(route["coords"], z, corridor_m) for z in zooms}


@app.route("/api/estimate", methods=["POST"])
def api_estimate():
    data = request.get_json(force=True)
    route, area, error = _target(data)
    if error:
        return jsonify({"error": error}), 400

    zooms = sorted(set(int(z) for z in data.get("zooms", [])))
    corridor_m = float(data.get("corridor_m", 300))
    if not zooms:
        return jsonify({"error": "Pick at least one zoom level."}), 400

    per_zoom = _selected_tiles(route, area, zooms, corridor_m)
    breakdown = {str(z): len(t) for z, t in per_zoom.items()}
    total = sum(breakdown.values())

    return jsonify({
        "tile_count": total,
        "per_zoom": breakdown,
        "bytes_on_card": total * TILE_BIN_BYTES,
        "too_many": total > MAX_TILE_COUNT,
        "limit": MAX_TILE_COUNT,
    })


@app.route("/api/build", methods=["POST"])
def api_build():
    data = request.get_json(force=True)
    route, area, error = _target(data)
    if error:
        return jsonify({"error": error}), 400

    zooms = sorted(set(int(z) for z in data.get("zooms", [])))
    corridor_m = float(data.get("corridor_m", 300))
    folder = (data.get("folder") or "tiles1").strip("/") or "tiles1"

    source = tile_sources.get(data.get("source", ""))
    if source is None:
        return jsonify({"error": "Pick a tile source."}), 400
    if not source.bulk_ok:
        return jsonify({"error":
            f"{source.label} cannot be used to build a card. {source.note} "
            f"Pick a source that permits offline use - see README_NAV.md."}), 400
    if not source.available:
        return jsonify({"error":
            f"{source.label} is not configured: {source.unavailable_reason}."}), 400

    if not zooms:
        return jsonify({"error": "Pick at least one zoom level."}), 400
    if max(zooms) > source.max_zoom:
        return jsonify({"error":
            f"{source.label} only goes to z{source.max_zoom}."}), 400

    per_zoom = _selected_tiles(route, area, zooms, corridor_m)
    work = [(z, x, y) for z, tiles in per_zoom.items() for x, y in sorted(tiles)]
    if len(work) > MAX_TILE_COUNT:
        return jsonify({"error": f"{len(work)} tiles exceeds the {MAX_TILE_COUNT} limit. "
                                 f"Narrow the corridor or drop a zoom level."}), 400

    # Some providers put a number on how much you may cache offline. Refuse to
    # build past it rather than quietly handing the user a card that breaks the
    # terms they agreed to.
    if source.bulk_limit_bytes:
        on_card = len(work) * TILE_BIN_BYTES
        if on_card > source.bulk_limit_bytes:
            return jsonify({"error":
                f"{on_card / 1e6:.0f} MB of tiles exceeds the "
                f"{source.bulk_limit_bytes / 1e6:.0f} MB offline cache "
                f"{source.label} permits. {source.policy} "
                f"Narrow the corridor or drop a zoom level."}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"done": 0, "total": len(work), "missing": 0,
                    "finished": False, "error": None, "file": None,
                    "download_name": "navigation_sdcard.zip" if route
                                     else f"map-{area['slug']}_sdcard.zip"}

    threading.Thread(
        target=_build_worker,
        args=(job_id, route, area, work, zooms, source, folder),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "tile_count": len(work)})


def _build_worker(job_id, route, area, work, zooms, source, folder):
    """Fetch and convert the tiles, then add the route - or, for an area, just
    the note saying what the tiles are."""
    job = jobs[job_id]
    try:
        seen_hashes = {}
        flat = {}               # hash -> one flat colour, decoded once per picture
        blocked_hash = None
        blocked_note = None

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            # Tiles are already compressed image data; storing beats deflating.
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(fetch_tile, source, z, x, y): (z, x, y)
                           for z, x, y in work}
                for fut in as_completed(futures):
                    z, x, y = futures[fut]
                    job["done"] += 1
                    try:
                        img = fut.result()
                    except tile_sources.TileSourceRefused as e:
                        job["error"] = str(e)
                        break
                    except Exception:
                        img = None

                    if img is None:
                        job["missing"] += 1
                        continue

                    # Identical bytes across many coordinates means the server
                    # is handing out one error image, not map tiles.
                    h = hashlib.blake2b(img, digest_size=16).digest()
                    n = seen_hashes[h] = seen_hashes.get(h, 0) + 1
                    if n == 2:
                        flat[h] = is_flat(img)
                    if (job["done"] >= BLOCKED_MIN_SAMPLES and
                            n >= job["done"] * BLOCKED_ABORT_RATIO and not flat[h]):
                        blocked_hash = h
                        job["error"] = (
                            f"{n} of the first {job['done']} tiles came back "
                            f"byte-identical. {source.label} is almost certainly "
                            f"serving an error image such as 'Access Blocked' "
                            f"rather than map tiles. Nothing was written, and "
                            f"the copies already cached were deleted.")
                        break

                    zf.writestr(f"{folder}/{z}/{x}/{y}.bin", image_to_lvgl_bin(img))

                if job["error"]:
                    # A server that refused or is blocking us will not change
                    # its mind for the rest of the corridor, so stop asking.
                    pool.shutdown(cancel_futures=True)

            if blocked_hash:
                _forget_blocked(source, futures, blocked_hash)
            if job["error"]:
                return

            repeated = [n for h, n in seen_hashes.items() if not flat.get(h)]
            worst = max(repeated, default=0)
            if worst >= BLOCKED_MIN_SAMPLES:
                blocked_note = (f"{worst} tiles were byte-identical - check a "
                                f"few on the card before trusting them.")

            if route:
                blob = rf.build_route_bin(
                    route["coords"],
                    route["maneuvers"],
                    name=route["name"],
                    duration_s=route["duration_s"],
                    min_zoom=min(zooms),
                    max_zoom=max(zooms),
                    kind=route["kind"],
                    loop=route["loop"],
                    tile_folder=folder,
                )
                zf.writestr(f"routes/{route['slug']}.bin", blob)
                zf.writestr(f"routes/{route['slug']}.txt",
                            _route_note(route, zooms, folder, len(work),
                                        job["missing"], source, blocked_note))
            else:
                # Beside routes/ rather than in it: there is no route for the
                # note to sit next to. The device opens the tile folder itself.
                zf.writestr(f"areas/{area['slug']}.txt",
                            _area_note(area, zooms, folder, len(work),
                                       job["missing"], source, blocked_note))
            zf.writestr("README.txt", _card_readme())

        buf.seek(0)
        job["file"] = buf
        job["warning"] = blocked_note
        job["finished"] = True
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["finished"] = True


def _forget_blocked(source, futures, blocked_hash):
    """
    fetch_tile cached the error picture as it arrived. Drop every copy, or the
    next build would find it on disk and stop again without asking the server.
    """
    for fut, (z, x, y) in futures.items():
        if fut.cancelled() or fut.exception() is not None:
            continue
        img = fut.result()
        if img and hashlib.blake2b(img, digest_size=16).digest() == blocked_hash:
            forget_tile(source, z, x, y)


def _route_note(route, zooms, folder, tile_count, missing, source, warning):
    """What this particular build contains. Named after the route so that
    unzipping another one onto the same card cannot overwrite it."""
    mode = route["profile"]
    if route["profile"] != "driving":
        mode += f" ({route['style']})"
    lines = [
        f"{route['name']}",
        "=" * len(route['name']),
        "",
        f"File       : routes/{route['slug']}.bin",
        f"Tiles      : {folder}/  ({tile_count} tiles, {missing} unavailable)",
        f"Distance   : {route['distance_m'] / 1000:.2f} km",
        f"Planned    : {route['duration_s'] // 60} min",
        f"Points     : {len(route['coords'])}",
        f"Turns      : {len(route['maneuvers'])}",
        f"Kind       : {route['kind']}{' (closed loop)' if route['loop'] else ''}",
        f"Mode       : {mode}, routed by {route['router']}",
        f"Avoiding   : {', '.join(route['avoid']) or 'nothing'}",
        f"Zooms      : {', '.join(str(z) for z in zooms)}",
        f"Source     : {source.label}",
        "",
        "Attribution (you must keep this with the tiles):",
        f"  {source.attribution}",
    ]
    if warning:
        lines += ["", "WARNING", "-------", f"  {warning}"]
    return "\n".join(lines) + "\n"


def _area_note(area, zooms, folder, tile_count, missing, source, warning):
    """What a tiles-only build contains, named after the area for the same
    reason a route's note is named after the route."""
    mid_lat = (area["south"] + area["north"]) / 2
    width_km = rf.haversine_m(mid_lat, area["west"], mid_lat, area["east"]) / 1000
    height_km = rf.haversine_m(area["south"], area["west"], area["north"], area["west"]) / 1000
    lines = [
        f"{area['name']}",
        "=" * len(area['name']),
        "",
        f"Tiles      : {folder}/  ({tile_count} tiles, {missing} unavailable)",
        f"Area       : {area['south']:.5f},{area['west']:.5f} to "
        f"{area['north']:.5f},{area['east']:.5f}",
        f"Size       : {width_km:.1f} x {height_km:.1f} km",
        f"Zooms      : {', '.join(str(z) for z in zooms)}",
        f"Source     : {source.label}",
        "",
        "Tiles only, no route. The device lists the folder as a map of its own;",
        f"pick '{folder}' to open it.",
        "",
        "Attribution (you must keep this with the tiles):",
        f"  {source.attribution}",
    ]
    if warning:
        lines += ["", "WARNING", "-------", f"  {warning}"]
    return "\n".join(lines) + "\n"


def _card_readme():
    """The card layout, which is the same for every build, so later builds
    overwriting this file lose nothing."""
    return """SD card contents for map_tiles navigation
=========================================

Unzip onto the root of a FAT32 SD card, so you end up with

  /<tile folder>/<zoom>/<x>/<y>.bin
  /routes/<route>.bin        the route itself
  /routes/<route>.txt        what that build contained
  /areas/<area>.txt          what a tiles-only build contained

TILES ONLY
----------
An area build has no route: just the tiles of a rectangle, for a map to look
at. The device lists each tile folder on the card as a map of its own, beside
the routes, and opens it with your position on it and nothing to follow. A
card with no routes and a single tile folder opens straight into that map.

MORE THAN ONE ROUTE
-------------------
Each build only contains the corridor of tiles around its own route, so a
second zip does NOT contain the first one's tiles - each has some the other
lacks.

You do not have to work out the difference. Unzip them onto the same card, in
any order, and they merge: the folder layout is identical, overlapping tiles
are byte-identical, and the card ends up with the union. The device then lists
everything in /routes/ and lets you pick.

  unzip -o first.zip  -d /Volumes/SDCARD
  unzip -o second.zip -d /Volumes/SDCARD

Use the -o flag, or ditto, so the extraction merges. If you extract by
double-clicking in a file manager, check afterwards that you have one tile
folder and not a second copy beside it with a number appended.

SHARING TILES BETWEEN ROUTES
----------------------------
Routes that cover the same ground should be built with the same tile folder
name; they then share those tiles instead of carrying a copy each, which is
what keeps a card with a dozen routes on it small. Give a route in another
region its own folder:

  /tiles_oc/...      /routes/sna-to-home.bin, /routes/sna-to-la.bin
  /tiles_tahoe/...   /routes/holiday.bin

Each route records the folder it belongs to, so there is nothing to configure
on the device.
"""


@app.route("/api/routing")
def api_routing():
    """Which router plans what, and what each profile can be asked for."""
    return jsonify({
        "base_url": osrm_base(),
        "is_demo": osrm_is_demo(),
        "valhalla_base_url": valhalla_base(),
        "profiles": sorted(PROFILES),
        "avoid": list(AVOID),
        "styles": list(STYLES),
        "trail_max_sac_scale": TRAIL_MAX_SAC_SCALE,
    })


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """
    Read or change the keys and paths the packager needs.

    Values come back masked: the browser needs to know whether a key is set and
    roughly which one it is, never the key itself.
    """
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        unknown = [k for k in data if k not in settings.FIELDS]
        if unknown:
            return jsonify({"error": f"Unknown setting(s): {', '.join(unknown)}"}), 400

        settings.set_many(data)
        tile_sources.reload()       # a key typed now should work now
        app.logger.info("Settings updated: %s", ", ".join(sorted(data)))

    return jsonify({
        "fields": settings.describe(),
        "path": settings.SETTINGS_PATH,
        "sources": [s.as_json() for s in tile_sources.REGISTRY.values()],
        "default_source": tile_sources.default_source_id(),
        "routing": {"base_url": osrm_base(), "is_demo": osrm_is_demo(),
                    "valhalla_base_url": valhalla_base()},
    })


@app.route("/api/providers")
def api_providers():
    """What the UI offers, and why anything unavailable is unavailable."""
    return jsonify({
        "sources": [s.as_json() for s in tile_sources.REGISTRY.values()],
        "default": tile_sources.default_source_id(),
    })


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    def stream():
        while True:
            job = jobs.get(job_id)
            if not job:
                yield "data: error\n\n"
                return
            yield f"data: {job['done']}/{job['total']}\n\n"
            if job["finished"]:
                # Said outright: a build that stops early never reaches its
                # total, and one that does still has the route to write.
                yield "data: finished\n\n"
                return
            time.sleep(0.4)

    return app.response_class(stream(), mimetype="text/event-stream")


@app.route("/api/result/<job_id>")
def api_result(job_id):
    """Outcome of a finished build, so the UI can say more than "done"."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "finished": job["finished"],
        "error": job.get("error"),
        "warning": job.get("warning"),
        "missing": job.get("missing", 0),
        "total": job.get("total", 0),
    })


@app.route("/api/download/<job_id>")
def api_download(job_id):
    job = jobs.get(job_id)
    if not job:
        return "Job not found", 404
    if job["error"]:
        return f"Build failed: {job['error']}", 500
    if not job["file"]:
        return "Not ready", 404

    job["file"].seek(0)
    return send_file(job["file"], as_attachment=True,
                     download_name=job["download_name"],
                     mimetype="application/zip")


if __name__ == "__main__":
    os.makedirs(CACHE_DIR, exist_ok=True)
    app.run(debug=True, port=5001)
