"""
route_format.py - build the `route.bin` files consumed by map_tiles >= 2.0.0.

The device does no parsing worth the name: every record is fixed size and
little-endian, so `map_route_load()` reads the arrays straight into PSRAM and
uses them in place. This module owns the writer half of that contract; the
reader half is `map_route.c` in the map_tiles component. Keep the two in step.

Layout (see map_route.h for the authoritative comment):

    Header, 64 bytes
      0   char[4]  "MRT1"
      4   uint16   version
      6   uint16   header_size (64)
      8   uint32   point_count
      12  uint32   maneuver_count
      16  uint32   string_bytes
      20  uint32   total_distance_m
      24  uint32   total_duration_s
      28  int32    min_lat_e7
      32  int32    min_lon_e7
      36  int32    max_lat_e7
      40  int32    max_lon_e7
      44  uint32   name_off
      48  uint8    min_zoom
      49  uint8    max_zoom
      50  uint16   flags            (bit0 = closed loop)
      52  uint8    kind             (0 = drive, 1 = exercise)
      53  uint8    reserved
      54  uint16   reserved
      56  uint32   tile_folder_off  (into the string pool; empty = device default)
      60  uint32   reserved

    Point, 16 bytes   int32 lat_e7, int32 lon_e7, uint32 cum_dist_m,
                      uint8 min_zoom, uint8 flags, uint16 bearing_cd
    Maneuver, 20 bytes uint32 point_index, uint32 dist_from_start_m,
                      uint32 name_off, uint32 instr_off,
                      uint8 type, uint8 modifier, uint16 bearing_after_cd
    String pool       NUL-terminated UTF-8; offset 0 is always ""

Can also be used on its own:

    python route_format.py --gpx ride.gpx --out route.bin
"""

import argparse
import math
import struct
import xml.etree.ElementTree as ET

MAGIC = b"MRT1"
FORMAT_VERSION = 1
HEADER_SIZE = 64
POINT_SIZE = 16
MANEUVER_SIZE = 20
NO_STRING = 0xFFFFFFFF
NO_BEARING = 0xFFFF

# Route-wide flags, header offset 50. Must match MAP_ROUTE_FLAG_* in map_route.h.
FLAG_LOOP = 1 << 0

# Route kinds, header offset 52. Must match map_route_kind_t in map_route.h.
KIND_DRIVE = 0
KIND_EXERCISE = 1
KINDS = {"drive": KIND_DRIVE, "exercise": KIND_EXERCISE}

EARTH_RADIUS_M = 6371008.8
TILE_SIZE = 256

# Must match map_maneuver_type_t in map_route.h, in order.
MANEUVER_TYPES = [
    "none", "depart", "arrive", "continue",
    "turn_slight_left", "turn_left", "turn_sharp_left",
    "turn_slight_right", "turn_right", "turn_sharp_right",
    "uturn", "roundabout", "merge",
    "fork_left", "fork_right", "exit_left", "exit_right",
]
MANEUVER_ID = {name: i for i, name in enumerate(MANEUVER_TYPES)}

# How far the simplified line may stray from the real one, in screen pixels.
# Two pixels is invisible on a 720x720 panel and cuts most routes by 10x.
DEFAULT_TOLERANCE_PX = 2.0


# --------------------------------------------------------------------- geometry

def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def world_px(lat, lon, zoom):
    """Slippy-map world pixel coordinates, matching map_geo.c."""
    n = TILE_SIZE * (2 ** zoom)
    lat = max(-85.05112877980659, min(85.05112877980659, lat))
    rad = math.radians(lat)
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n
    return x, y


def _perp_px(p, a, b):
    """Perpendicular distance from p to segment a-b, all in pixels."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span))
    return math.hypot(px - (ax + dx * t), py - (ay + dy * t))


def douglas_peucker_significance(points_px):
    """
    For each vertex, the tolerance at which Douglas-Peucker would first drop it.

    Plain DP run once per zoom gives sets that are not guaranteed to nest, which
    would make a vertex flicker in and out as you zoom. Capping each vertex's
    significance by its parent's makes the hierarchy monotone, so a single
    `min_zoom` byte per point is enough to drive every level of detail.
    """
    n = len(points_px)
    if n == 0:
        return []
    sig = [0.0] * n
    sig[0] = float("inf")
    sig[-1] = float("inf")

    stack = [(0, n - 1, float("inf"))]
    while stack:
        i, j, parent = stack.pop()
        if j <= i + 1:
            continue
        best_d, best_k = -1.0, -1
        for k in range(i + 1, j):
            d = _perp_px(points_px[k], points_px[i], points_px[j])
            if d > best_d:
                best_d, best_k = d, k
        if best_k < 0:
            continue
        s = min(best_d, parent)
        sig[best_k] = s
        stack.append((i, best_k, s))
        stack.append((best_k, j, s))

    return sig


def assign_min_zoom(coords, min_zoom, max_zoom, tolerance_px=DEFAULT_TOLERANCE_PX,
                    forced_indices=()):
    """
    Lowest zoom at which each vertex still has to be drawn.

    Significance is measured once in world pixels at `max_zoom`; at zoom z the
    same ground distance is 2**(z - max_zoom) pixels, which inverts to a closed
    form instead of re-running DP per level.
    """
    pts_px = [world_px(lat, lon, max_zoom) for lat, lon in coords]
    sig = douglas_peucker_significance(pts_px)

    out = []
    for s in sig:
        if s == float("inf") or s <= 0.0:
            z = min_zoom if s == float("inf") else max_zoom
        else:
            z = max_zoom + math.log2(tolerance_px / s)
            z = int(math.ceil(z))
            z = max(min_zoom, min(max_zoom, z))
        out.append(z)

    # A turn must never be simplified away, or the banner points at nothing.
    for i in forced_indices:
        if 0 <= i < len(out):
            out[i] = min_zoom

    return out


# ----------------------------------------------------------------- string pool

class StringPool:
    """NUL-terminated UTF-8 blob; offset 0 is the empty string."""

    def __init__(self):
        self.buf = bytearray(b"\x00")
        self.index = {"": 0}

    def add(self, text):
        if not text:
            return 0
        if text in self.index:
            return self.index[text]
        off = len(self.buf)
        self.buf += text.encode("utf-8") + b"\x00"
        self.index[text] = off
        return off

    def __len__(self):
        return len(self.buf)


# ---------------------------------------------------------------------- writer

def is_closed_loop(coords, tolerance_m=30.0):
    """Does the route end close enough to where it began to be lapped?"""
    if len(coords) < 3:
        return False
    return haversine_m(*coords[0], *coords[-1]) <= tolerance_m


def build_route_bin(coords, maneuvers=None, name="", duration_s=0,
                    min_zoom=12, max_zoom=19, tolerance_px=DEFAULT_TOLERANCE_PX,
                    kind="drive", loop=None, tile_folder=""):
    """
    Serialise a route.

    coords     : [(lat, lon), ...] in order, at least 2
    maneuvers  : [{point_index, type, modifier, street, instruction}, ...]
    duration_s : planner's estimate; the device falls back to it when stopped
    kind       : "drive" or "exercise"; changes what the device puts on screen
    loop       : True/False, or None to decide from the geometry
    tile_folder: which folder on the card holds this route's tiles. Leave empty
                 and the device uses its configured default.
    """
    if len(coords) < 2:
        raise ValueError("a route needs at least two points")

    maneuvers = list(maneuvers or [])

    # Cumulative distance and per-segment bearing.
    cum = [0]
    bearings = []
    total = 0.0
    for i in range(len(coords) - 1):
        (lat1, lon1), (lat2, lon2) = coords[i], coords[i + 1]
        total += haversine_m(lat1, lon1, lat2, lon2)
        cum.append(int(round(total)))
        bearings.append(bearing_deg(lat1, lon1, lat2, lon2))
    bearings.append(None)  # last point has no "next"

    forced = [m["point_index"] for m in maneuvers]
    zooms = assign_min_zoom(coords, min_zoom, max_zoom, tolerance_px, forced)

    maneuver_at = set(forced)

    pool = StringPool()
    name_off = pool.add(name) if name else NO_STRING
    if tile_folder:
        pool.add(tile_folder)

    points = bytearray()
    for i, (lat, lon) in enumerate(coords):
        b = bearings[i]
        bearing_cd = NO_BEARING if b is None else int(round(b * 100)) % 36000
        flags = 1 if i in maneuver_at else 0
        points += struct.pack(
            "<iiIBBH",
            int(round(lat * 1e7)),
            int(round(lon * 1e7)),
            cum[i],
            zooms[i],
            flags,
            bearing_cd,
        )

    def intern(text):
        return pool.add(text) if text else NO_STRING

    man_bytes = bytearray()
    for m in sorted(maneuvers, key=lambda x: x["point_index"]):
        idx = max(0, min(len(coords) - 1, int(m["point_index"])))
        after = m.get("bearing_after")
        if after is None:
            after = bearings[idx] if bearings[idx] is not None else 0.0
        man_bytes += struct.pack(
            "<IIIIBBH",
            idx,
            cum[idx],
            intern(m.get("street", "")),
            intern(m.get("instruction", "")),
            MANEUVER_ID.get(m.get("type", "none"), 0),
            int(m.get("modifier", 0)) & 0xFF,
            int(round(after * 100)) % 36000,
        )

    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]

    header = bytearray(HEADER_SIZE)
    struct.pack_into("<4sHH", header, 0, MAGIC, FORMAT_VERSION, HEADER_SIZE)
    struct.pack_into("<III", header, 8, len(coords), len(maneuvers), len(pool))
    struct.pack_into("<II", header, 20, int(round(total)), int(duration_s))
    struct.pack_into("<iiii", header, 28,
                     int(round(min(lats) * 1e7)), int(round(min(lons) * 1e7)),
                     int(round(max(lats) * 1e7)), int(round(max(lons) * 1e7)))
    if loop is None:
        loop = is_closed_loop(coords)
    flags = FLAG_LOOP if loop else 0

    struct.pack_into("<I", header, 44, name_off)
    struct.pack_into("<BBH", header, 48, min_zoom, max_zoom, flags)
    struct.pack_into("<B", header, 52, KINDS.get(kind, KIND_DRIVE))
    struct.pack_into("<I", header, 56,
                     pool.add(tile_folder) if tile_folder else NO_STRING)

    return bytes(header) + bytes(points) + bytes(man_bytes) + bytes(pool.buf)


def read_route_bin(blob):
    """Decode a route.bin, mirroring map_route.c. Used by the self-test."""
    if blob[0:4] != MAGIC:
        raise ValueError("bad magic")
    version, header_size = struct.unpack_from("<HH", blob, 4)
    if version != FORMAT_VERSION or header_size != HEADER_SIZE:
        raise ValueError(f"unsupported version {version}/{header_size}")

    point_count, maneuver_count, string_bytes = struct.unpack_from("<III", blob, 8)
    total_distance_m, total_duration_s = struct.unpack_from("<II", blob, 20)
    min_lat, min_lon, max_lat, max_lon = struct.unpack_from("<iiii", blob, 28)
    name_off, = struct.unpack_from("<I", blob, 44)
    min_zoom, max_zoom, route_flags = struct.unpack_from("<BBH", blob, 48)
    kind, = struct.unpack_from("<B", blob, 52)
    tile_folder_off, = struct.unpack_from("<I", blob, 56)

    off = HEADER_SIZE
    points = []
    for _ in range(point_count):
        lat_e7, lon_e7, cum_m, mz, flags, bcd = struct.unpack_from("<iiIBBH", blob, off)
        points.append(dict(lat=lat_e7 / 1e7, lon=lon_e7 / 1e7, cum_dist_m=cum_m,
                           min_zoom=mz, flags=flags, bearing_cd=bcd))
        off += POINT_SIZE

    maneuvers = []
    for _ in range(maneuver_count):
        pi, dist, noff, ioff, mtype, mod, bac = struct.unpack_from("<IIIIBBH", blob, off)
        maneuvers.append(dict(point_index=pi, dist_from_start_m=dist, name_off=noff,
                              instr_off=ioff, type=mtype, modifier=mod,
                              bearing_after_cd=bac))
        off += MANEUVER_SIZE

    pool = blob[off:off + string_bytes]
    if len(pool) != string_bytes:
        raise ValueError("truncated string pool")

    def s(o):
        if o == NO_STRING or o >= string_bytes:
            return ""
        end = pool.index(b"\x00", o)
        return pool[o:end].decode("utf-8")

    for m in maneuvers:
        m["street"] = s(m["name_off"])
        m["instruction"] = s(m["instr_off"])

    return dict(
        name=s(name_off), points=points, maneuvers=maneuvers,
        total_distance_m=total_distance_m, total_duration_s=total_duration_s,
        bounds=(min_lat / 1e7, min_lon / 1e7, max_lat / 1e7, max_lon / 1e7),
        min_zoom=min_zoom, max_zoom=max_zoom,
        flags=route_flags, loop=bool(route_flags & FLAG_LOOP), kind=kind,
        tile_folder=s(tile_folder_off),
        expected_size=HEADER_SIZE + point_count * POINT_SIZE
                      + maneuver_count * MANEUVER_SIZE + string_bytes,
    )


# ------------------------------------------------------------------ OSRM glue

def osrm_maneuver_to_type(step_maneuver):
    """Map an OSRM maneuver onto map_maneuver_type_t."""
    t = (step_maneuver.get("type") or "").lower()
    mod = (step_maneuver.get("modifier") or "").lower()

    if t == "depart":
        return "depart"
    if t == "arrive":
        return "arrive"
    if t in ("roundabout", "rotary", "roundabout turn"):
        return "roundabout"
    if t in ("exit roundabout", "exit rotary"):
        return "continue"
    if t == "merge":
        return "merge"
    if t == "fork":
        return "fork_left" if "left" in mod else "fork_right"
    if t == "on ramp":
        return "exit_left" if "left" in mod else "exit_right"
    if t == "off ramp":
        return "exit_left" if "left" in mod else "exit_right"

    # turn / end of road / new name / continue, all decided by the modifier
    if mod == "uturn":
        return "uturn"
    if mod == "sharp left":
        return "turn_sharp_left"
    if mod == "left":
        return "turn_left"
    if mod == "slight left":
        return "turn_slight_left"
    if mod == "sharp right":
        return "turn_sharp_right"
    if mod == "right":
        return "turn_right"
    if mod == "slight right":
        return "turn_slight_right"
    return "continue"


def route_from_osrm(osrm_route, name=""):
    """
    Flatten an OSRM `route` object into (coords, maneuvers, duration_s).

    OSRM gives the geometry once for the whole route and again per step; we keep
    the whole-route geometry and locate each step in it by its maneuver location,
    so the indices always refer to the array we actually ship.
    """
    geometry = osrm_route["geometry"]["coordinates"]       # [lon, lat]
    coords = [(lat, lon) for lon, lat in geometry]

    def nearest_index(lat, lon, start=0):
        """
        Closest vertex at or after `start`.

        The geometry is ordered and steps come in order, so the search only ever
        moves forward, and it gives up once it has walked well past the best
        candidate rather than scanning the rest of a long route.
        """
        best_i, best_d = start, float("inf")
        for i in range(start, len(coords)):
            d = (coords[i][0] - lat) ** 2 + (coords[i][1] - lon) ** 2
            if d < best_d:
                best_d, best_i = d, i
            elif i - best_i > 64:
                break
        return best_i

    maneuvers = []
    cursor = 0
    for leg in osrm_route.get("legs", []):
        for step in leg.get("steps", []):
            man = step.get("maneuver", {})
            loc = man.get("location")
            if not loc:
                continue
            idx = nearest_index(loc[1], loc[0], cursor)
            cursor = idx
            maneuvers.append({
                "point_index": idx,
                "type": osrm_maneuver_to_type(man),
                "modifier": int(man.get("exit", 0)) & 0xFF,
                "street": step.get("name", "") or "",
                "instruction": "",
                "bearing_after": man.get("bearing_after"),
            })

    return coords, maneuvers, int(osrm_route.get("duration", 0))


# ------------------------------------------------------------------ GPX input

def coords_from_gpx(path):
    """Read <trkpt>/<rtept>/<wpt> in document order. No turn data, so no maneuvers."""
    tree = ET.parse(path)
    root = tree.getroot()
    ns = {"gpx": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

    def find_all(tag):
        return root.iter("{%s}%s" % (ns["gpx"], tag)) if ns else root.iter(tag)

    for tag in ("trkpt", "rtept", "wpt"):
        pts = [(float(e.get("lat")), float(e.get("lon"))) for e in find_all(tag)]
        if len(pts) >= 2:
            return pts
    raise ValueError("no track, route or waypoint list with 2+ points in the GPX")


# ------------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(description="Build a route.bin for map_tiles.")
    ap.add_argument("--gpx", required=True, help="input GPX file")
    ap.add_argument("--out", default="route.bin", help="output file")
    ap.add_argument("--name", default="", help="route name shown on the device")
    ap.add_argument("--min-zoom", type=int, default=12)
    ap.add_argument("--max-zoom", type=int, default=19)
    ap.add_argument("--tolerance-px", type=float, default=DEFAULT_TOLERANCE_PX)
    ap.add_argument("--kind", choices=("drive", "exercise"), default="drive",
                    help="exercise shows elapsed time and laps instead of ETA")
    ap.add_argument("--loop", action="store_true",
                    help="force closed-loop; otherwise decided from the geometry")
    ap.add_argument("--tile-folder", default="",
                    help="tile folder on the card; empty uses the device default")
    args = ap.parse_args()

    coords = coords_from_gpx(args.gpx)
    blob = build_route_bin(coords, name=args.name,
                           min_zoom=args.min_zoom, max_zoom=args.max_zoom,
                           tolerance_px=args.tolerance_px,
                           kind=args.kind,
                           loop=True if args.loop else None,
                           tile_folder=args.tile_folder)
    with open(args.out, "wb") as f:
        f.write(blob)

    info = read_route_bin(blob)
    kept = {}
    for p in info["points"]:
        kept[p["min_zoom"]] = kept.get(p["min_zoom"], 0) + 1
    print(f"{args.out}: {len(coords)} points, {info['total_distance_m'] / 1000:.2f} km, "
          f"{len(blob)} bytes, {args.kind}{', loop' if info['loop'] else ''}")
    print("points introduced per zoom:",
          ", ".join(f"z{z}:{n}" for z, n in sorted(kept.items())))


if __name__ == "__main__":
    main()
