# 🧭 Offline Navigation Packager (v2)

`app.py` answers *"give me every tile in this rectangle"*. That is right for a
map viewer and wrong for navigation: a 20 km route inside its own bounding box
is mostly tiles you will never look at, and it still leaves you to produce turn
instructions by hand.

`app_nav.py` is the navigation half. It leaves v1 completely alone — different
app, different page, different requirements file, different tile cache, different
port. Run either, or both at once.

```bash
pip install -r requirements_nav.txt
python app_nav.py            # http://127.0.0.1:5001
```

What it produces is read by [`map_tiles`](https://github.com/0015/map_tiles)
**v2.0.0** or later — the version that added the navigation layer. Working
firmware, for a round ESP32-S3 AMOLED, is in
[`map_tiles_projects/v2`](https://github.com/0015/map_tiles_projects/tree/main/v2).

## What it does

1. **Plans a route** through the waypoints you click. Driving goes to the
   public OSRM demo server, optionally avoiding tolls and/or highways. Walking
   and cycling go to Valhalla, which uses footpaths, trails and cycleways, and
   can be told to prefer trails or to take the shortest way. The result appears
   on a card over the map, next to the line it describes — distance, planned
   time, turn count, geometry points, and the turn list folded away until you
   want it.
2. **Selects only a corridor** of tiles around that route, not its bounding box.
   For a 20 km route running diagonally, at z16–17 with a 300 m corridor, that
   is about 760 tiles against about 4,100 for its bounding box, and the gap
   widens with distance: the box grows with the square of it.
3. **Converts tiles in memory** to the LVGL v9 RGB565 `.bin` the device reads —
   byte-identical to `script/lvgl_map_tile_converter.py`, with no intermediate
   folder of PNGs to convert afterwards.
4. **Writes `route.bin`**: geometry, cumulative distances, per-zoom level of
   detail and turn instructions, in the format `map_route.c` reads.
5. **Hands back one zip** you unpack onto the root of the SD card.

```
navigation_sdcard.zip
├── tiles1/<zoom>/<x>/<y>.bin
├── routes/<route>.bin
├── routes/<route>.txt  what this build contained
└── README.txt          the card layout, and how to merge more builds onto it
```

Or, with no route at all, it builds the same card from every tile in a
rectangle — a map on its own. See [Tiles only, no route](#tiles-only-no-route).

## The window

```
+--------------------------------------+-------------------+
|                                      |  Route | Area     |
|                                      |  1 - Route        |
|              the map                 |    kind, profile  |
|                                      |    avoid or style |
|          (click to add               |    loop, name     |
|           waypoints)                 |    [Plan] [Clear] |
|                                      |  2 - Tiles        |
|                                      |    zooms          |
|  +--------------------------+        |    corridor       |
|  | Morning circuit   loop   |        |    source, folder |
|  |  13.5   17    31    385  |        |                   |
|  |  km/lap min  turns points|        |  3 - Build        |
|  | > Turn by turn           |        |    [Est] [Build]  |
|  +--------------------------+        |    result, status |
+--------------------------------------+-------------------+
```

The sidebar is the three steps you work through, in order. The explanations sit
behind the small **?** buttons — useful the first time, noise afterwards. The
switch at the top swaps step 1 for an area with no route; steps 2 and 3 stay
the same.

## Choosing zoom levels and corridor width

Every extra zoom level roughly doubles or triples a route's tile count, and a
tile is 131 KB on the card whatever its content. **z16–17 is the sweet spot
for driving**: z17 is readable at speed, z16 gives you context when you zoom
out.

The corridor is how far either side of the route to keep tiles. 300 m is enough
to see the surrounding blocks; widen it if you expect to pan around or to wander
off route and still want a map underneath you.

Hit **Estimate** before **Build** — it shows the tile count, the size on the
card, and how long the download will take at a rate that does not abuse the tile
server.

## Putting several routes on one card

Each build contains **only the corridor around its own route**, so a second zip
does not contain the first one's tiles. Measured on two real builds sharing
`tiles_oc`:

| | tiles |
|---|---|
| `sna-to-home.zip` | 130 |
| `sna-to-la.zip` | 749 |
| in both | 120 (92% of the first) |
| only in the first | **10** |
| only in the second | 629 |
| union on the card | 759 |

**You do not have to find that difference yourself.** Unzip both onto the card,
in any order, and they merge — the folder layout is identical and overlapping
tiles are byte-identical (0 of the 120 differed). Verified with `unzip -o` and
with `ditto`; both give 759 tiles and the same card whichever order you use.

```bash
unzip -o sna-to-home.zip -d /Volumes/SDCARD
unzip -o sna-to-la.zip   -d /Volumes/SDCARD
```

Use `-o`, or `ditto`. If you extract by double-clicking in a file manager,
check afterwards that you have one `tiles_oc` and not a second copy beside it
with a number appended.

Every build also drops `routes/<slug>.txt` recording what went into it. That is
named after the route rather than being a single `README.txt`, so a later build
cannot overwrite an earlier one's record.

Routes in a different region get their own folder and nothing is shared:

```
/tiles_oc/...      /routes/sna-to-home.bin, /routes/sna-to-la.bin
/tiles_tahoe/...   /routes/holiday.bin
```

## Tiles only, no route

For a map to look at rather than a route to follow, switch the top of the
sidebar to **Area · tiles only**. Click two opposite corners on the map, or
**Use this view** to take what is on screen, and drag a corner to adjust; a
third click starts a new rectangle. Zoom levels, tile source, folder, the
blocked-tile guard and the conversion to RGB565 are exactly as for a route.
There is no corridor: every tile the rectangle touches, at every zoom you pick.

```
map-<area>_sdcard.zip
├── tiles1/<zoom>/<x>/<y>.bin
├── areas/<area>.txt    what this build contained, with the attribution
└── README.txt
```

There is no `routes/` in it. The note goes in `areas/`, beside `routes/` rather
than in it, since there is no route for it to sit next to; the device opens the
tile folder itself. The zip unzips onto the same card as route builds and merges
the same way, and an area built into the same tile folder as routes over the
same ground shares their tiles.

On the device ([map_tiles_projects v2](https://github.com/0015/map_tiles_projects/tree/main/v2)),
every tile folder is listed as a map of its own beside the routes, and a card
with no routes and a single tile folder opens straight into that map: your
position, distance covered, time and speed, and nothing to follow.

An area grows with its size and fourfold with each zoom level added, so
**Estimate** first:

| Area | Zooms | Tiles | On the card |
|---|---|---|---|
| 2 × 2 km | z15–17 | 101 | 13 MB |
| 5 × 5 km | z13–16 | 138 | 18 MB |
| 5 × 5 km | z13–17 | 538 | 71 MB |
| 10 × 10 km | z12–16 | 575 | 75 MB |

The area travels with each Estimate and Build request rather than being kept on
the server, since there is nothing to plan. One that crosses the 180th meridian
is refused.

## Drive routes and exercise circuits

A route carries what it is for, and the device reads it from the file — there is
nothing to switch on the device itself. Two things are recorded, and they are
independent. The **kind** (Drive or Exercise) decides what the panel counts:

| | **Drive** | **Exercise** |
|---|---|---|
| Panel shows | remaining · arrive in · speed | distance · elapsed · speed |

Whether the route is a **closed loop** decides what happens at the end:

| | **Open route** | **Closed loop** |
|---|---|---|
| Reaching the end | "Arrived" | counts a lap and carries on |
| Banner sub-line | street name | `Lap 3 · street name` |

For a workout circuit, pick **Exercise** and tick the loop: elapsed time and
distance on the panel, laps on the banner. A loop left as Drive still laps, but
its panel counts down the distance and time left in the current lap.

**Every point you click is a via point**, not a suggestion. The router goes the
way you drew it, which is the whole point when you know the road you want to be
on. Tick **Return to the start (closed loop)** and the first waypoint is
appended as the last, so the route closes and the device laps it forever instead
of arriving.

A route that happens to end within 30 m of where it started is detected as a
loop even without the checkbox.

```bash
# The same from a GPX, without the web UI:
python route_format.py --gpx circuit.gpx --out route.bin --kind exercise --loop
```

### How a lap is counted

The start line of a closed loop is also its finish line, so a rider sitting on
it is equally close to the first segment of the route and the last — successive
fixes flip between "just started" and "nearly done". Anything that watches for
that transition counts a lap on every flip, and several before you have set off.

So the device requires the circuit to have actually been ridden: **start, then
the middle of the loop, then the end, then start again**. Wobble at the line
only ever flips between end and start, never through the middle, so it cannot
fake a lap. Verified against a real 17.7 km circuit at up to 20 m of GPS noise.

## Walking and cycling

Walking and cycling routes are planned by **Valhalla**, with its pedestrian and
bicycle models. Those know the footpaths, park paths, steps, trails and
cycleways a car route never touches, so a walk across a park goes through it
rather than round it.

They used to go to OSRM as `foot` and `bike`, but **the public OSRM demo server
only deploys the car profile** and answers every profile with the same car
route. Measured across Central Park, 5th Avenue to Central Park West at 72nd
Street:

| | Distance | Time | Goes |
|---|---|---|---|
| OSRM demo, `foot` | 1.89 km | 4 min | round the 65th Street Transverse, a road |
| Valhalla, walking | 1.17 km | 14 min | through the park |

With Walking or Cycling selected, three styles sit under the profile selector
where the avoid switches are for driving:

| Style | Walking | Cycling |
|---|---|---|
| **Balanced** | The router's own judgement: paths and streets, weighed against hills | Cycleways, bike trails and roads, for a hybrid bike |
| **Prefer trails** | Footpaths and trails cost half, a pavement beside traffic twice; unpaved tracks welcome | Plans for a mountain bike: dirt is rideable, roads shared with traffic are avoided |
| **Shortest** | Distance and nothing else: every cut-through, whatever the climb | Distance and nothing else, whatever the surface |

What they did on real routes:

| Route | Balanced | Prefer trails | Shortest |
|---|---|---|---|
| Central Park, walking | 1.17 km, 72nd Street Transverse | 1.37 km, Wallach Walk and the Lilac Walk | 1.14 km |
| Peters Canyon, walking | 3.58 km, Regional Park Connector Trail | 3.67 km, Willow Trail and Peters Canyon Trail | same as Balanced |
| Peters Canyon, cycling | 4.16 km, out onto Jamboree Road | same as Balanced | 4.15 km, through the canyon on Peters Canyon Trail |

Two things to know before relying on them:

- **Cycling "Prefer trails" often matches Balanced.** Valhalla has no way to ask
  for dirt over tarmac: planning for a mountain bike only makes rough surfaces
  acceptable and cheaper. Where a paved path runs alongside, the route stays on
  it, and the planned time grows because a mountain bike is slower. For the
  cut-through, use **Shortest**.
- **Hiking trails are capped.** By default Valhalla leaves out any trail harder
  than `sac_scale=hiking` (T1). Prefer trails and Shortest raise that to
  mountain hiking (T2), which lets most hill trails in without sending anyone
  up a scramble. The limit is `TRAIL_MAX_SAC_SCALE` in `app_nav.py`.

Switching style, or profile, with a route on the map plans it again straight
away, so the difference is right there on the card. The card and the build's
`routes/<route>.txt` both record the profile and style a route was planned with.

Driving routes go to OSRM exactly as before. Point either router at your own
server in **Settings**, or:

```bash
export OSRM_BASE_URL='http://localhost:5000'        # driving
export VALHALLA_BASE_URL='http://localhost:8002'    # walking, cycling, avoiding
```

## Avoiding tolls and highways

With the **Driving** profile, two switches sit under the profile selector:
**Avoid tolls** and **Avoid highways**. Tick either, or both, and plan. Once a
route is on the map, flipping a switch plans it again straight away, so the
difference in distance and time is right there on the card.

Those routes are planned by **Valhalla**, not OSRM. OSRM can only leave roads
out when the server's profile was built with those classes marked excludable.
Neither public OSRM server does that: both answer every `exclude=` with
`InvalidValue`. The public FOSSGIS Valhalla server that openstreetmap.org uses
takes `use_tolls` / `use_highways` as a penalty instead of a ban. It goes round
wherever there is a reasonable way round, and still arrives when a waypoint
sits on a toll road. A hard ban answers "impossible route" in that case.

When the route still has to use a toll road or highway somewhere, the card
says so in orange. The route itself is kept, not refused. The build's
`routes/<route>.txt` records what was avoided and which router planned it.

Point it at your own Valhalla in **Settings**, or:

```bash
export VALHALLA_BASE_URL='http://localhost:8002'
```

Driving routes without an avoid switch still go to OSRM exactly as before.

## Caching

Tiles are cached on disk per source, under `tiles_v2/<source>/<z>/<x>/<y>.img`,
so rebuilding the same route costs nothing. **Absences are cached too**, for a
week: a corridor always clips a few tiles the server has nothing for, and
without that every rebuild would spend an API call on each of them again.

Measured on an 82-tile corridor: 82 requests cold, **0 on every rebuild**.

Delete `tiles_v2/` to force a refresh. The cache is keyed by source, not by
URL, so after pointing **Self-hosted** at a different server or style, delete
`tiles_v2/local/` too — otherwise the old style's tiles, and a week of "no such
tile" from a wrong URL, are served from disk.

## Using a GPX you already have

```bash
python route_format.py --gpx ride.gpx --out route.bin --name "Sunday loop"
```

It prints how many points survive at each zoom, which is a good sanity check
that the level of detail is doing its job:

```
route.bin: 1200 points, 4.24 km, 19277 bytes, drive
points introduced per zoom: z12:10, z14:4, z16:12, z17:13, z19:1161
```

A GPX has no turn data, so you get the line to follow and the distance remaining
but no turn banner. Plan through the web UI if you want instructions.

## `route.bin`

Fixed-size little-endian records, so the device reads the arrays straight into
PSRAM and uses them in place — no XML, no JSON, no allocation per point.

| Section | Size | Contents |
|---|---|---|
| Header | 64 B | `"MRT1"`, counts, total distance and duration, bounding box |
| Points | 16 B each | `lat_e7`, `lon_e7`, `cum_dist_m`, `min_zoom`, `flags`, `bearing_cd` |
| Maneuvers | 20 B each | point index, distance from start, street and instruction offsets, type, modifier, bearing |
| Strings | variable | NUL-terminated UTF-8 |

The interesting field is **`min_zoom`**: the lowest zoom at which that vertex
still has to be drawn. Drawing at zoom Z is then one filtered pass over the
array — no second copy of the geometry, and no simplification work on the
device. It is computed here with a Douglas-Peucker hierarchy whose significance
values are capped by their parent's, which makes the levels nest properly; run
plain DP once per zoom and vertices flicker in and out as you zoom.

The reader is `map_route.c` in the map_tiles component. The two files are a
contract — change one and you must change the other.

## Where the tiles come from

`https://tile.openstreetmap.org` runs on donated hardware for people looking at
maps in a browser. **Its usage policy forbids bulk downloading, and the servers
enforce it**: once you trip the limit you stop receiving map tiles and start
receiving an "Access Blocked — App is not following the tile usage policy"
image, with HTTP 200, which lands on the SD card looking like a perfectly
ordinary tile. You find out on the device.

So v2 will not build a card from it.

**Nor will a random API key make this legitimate.** The providers differ a great
deal on whether pre-downloading tiles for offline use is allowed at all, and two
of the best-known ones say no on their standard plans. Checked against the
published terms in September 2026 — re-check yours, terms move:

| Source | Set up with | Pre-downloading a corridor for an SD card |
|---|---|---|
| **Self-hosted tile server** | `TILE_LOCAL_URL` | Your server, your rules. No quota. Keep the ODbL attribution. |
| **Local `.mbtiles`** | `TILE_MBTILES` | A file you already hold. Nothing is fetched. Must be **raster**. |
| **Local directory** | `TILE_DIR` | Tiles you already hold. |
| **Geoapify** | `GEOAPIFY_KEY` | **Allowed.** Permits caching, storing and redistributing generated tiles. The most permissive keyed provider for this. |
| Stadia Maps | `STADIA_KEY` | Allowed only to cache **up to 100 MB per device** for offline use **in a mobile application** — check that yours fits. The builder refuses anything larger. |
| Thunderforest | `THUNDERFOREST_KEY` | Needs the **Small Business plan or higher**. Explicitly prohibited below that. |
| MapTiler Cloud | `MAPTILER_KEY` | **Not allowed.** Bulk download is prohibited and so is exporting map content for use outside the Service; only a temporary personal cache. Needs a written agreement, or MapTiler Server for self-hosting. Disabled in the dropdown. |
| OpenStreetMap standard | — | **Not allowed.** Browsing only. |

So: **Geoapify for a free key that fits this use, or self-host.** The dropdown
shows each source's policy under it, greys out the ones whose terms do not
permit this, marks the ones still waiting for a key or a path, and refuses a
build that would exceed a stated cache cap.

```bash
python app_nav.py           # then set the key in the page
```

**Everything is configured in the browser.** Pick a tile source; if it needs a
key, a URL or a path, the field for it appears directly underneath, with a
**Save** button, and stays there once set so a wrong key can be replaced in the
same place. It takes effect immediately — no restart, no re-exporting in every
new shell.

The **Settings** disclosure holds the values that are not tied to one source:
your contact email, which goes in the User-Agent the tile servers see, the
OSRM server that plans driving, and the Valhalla server that plans walking,
cycling and avoid-tolls/highways routes.

Where it is kept:

```
~/.config/offline-map-downloader/settings.json      # 0600, dir 0700
```

Outside the repository, so a key cannot be committed by accident. Keys are
only ever sent back to the page **masked** (`abcd…ijkl`), and the log records
which fields changed, never what they changed to.

Environment variables still work and are used when nothing is saved, so an
existing `export GEOAPIFY_KEY=...` setup keeps running. Saving in the page takes
over from then on; saving a field empty removes it, and the environment takes
over again. The page shows which of the two each value came from.

**Self-hosted needs a URL**, e.g.
`http://localhost:8080/styles/basic-preview/{z}/{x}/{y}.png`. Until you give it
one the entry stays unconfigured rather than pointing hopefully at a localhost
port with nothing behind it.

A key that is wrong, or restricted to an HTTP referrer, fails the build loudly:

```
Geoapify OSM Bright refused the request (HTTP 401). Check the API key and
what your plan allows.
```

This tool fetches server-side, so an origin-restricted key will not work. Leave
the key unrestricted for local use, or restrict it by IP.

The attribution string each source requires is written into the build's note on
the card, `routes/<route>.txt` or `areas/<area>.txt` — keep it with the tiles.
The policy summaries above are a starting point, not legal advice: your plan is
between you and the provider.

Sources for the table: [MapTiler Cloud terms](https://www.maptiler.com/terms/cloud/),
[Thunderforest terms](https://www.thunderforest.com/terms/),
[Stadia Maps terms](https://stadiamaps.com/terms-of-service/),
[Geoapify map tiles](https://www.geoapify.com/map-tiles/),
[OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/).

### Self-hosting, in short

The fully independent route, and the only one with no quota:

```bash
# 1. vector tiles for a region. --download fetches the Southern California
#    extract from Geofabrik (~670 MB), plus the ocean and Natural Earth data
#    the OpenMapTiles profile needs (~1 GB, once).
docker run -e JAVA_TOOL_OPTIONS="-Xmx1g" -v "$PWD/data:/data" \
    ghcr.io/onthegomap/planetiler:latest \
    --download --area=socal --output=/data/socal.mbtiles

# 2. serve them as raster XYZ, drawn with tileserver-gl's bundled style
docker run -p 8080:8080 -v "$PWD/data:/data" maptiler/tileserver-gl \
    --file /data/socal.mbtiles

export TILE_LOCAL_URL='http://localhost:8080/styles/basic-preview/{z}/{x}/{y}.png'
```

Then pick "Self-hosted tile server" and build as usual.

> A `.mbtiles` from planetiler or MapTiler Data holds **vector** tiles, which
> the device cannot draw. Serve them through tileserver-gl as above, which
> rasterises them. `TILE_MBTILES` is for raster `.mbtiles` only, and the tool
> checks and tells you if you point it at the wrong kind.

### The blocked-tile guard

However good the source, a service that decides it dislikes you tends to answer
with an error picture rather than an error code. The builder hashes every tile
as it arrives, and if the same bytes keep coming back for different coordinates
it stops and says so rather than writing several hundred copies of a "computer
says no" graphic to your card. It also stops asking the server for the rest,
and deletes the copies it had already cached, so the next build asks afresh:

```
20 of the first 20 tiles came back byte-identical. Geoapify OSM Bright is
almost certainly serving an error image such as 'Access Blocked' rather than
map tiles. Nothing was written, and the copies already cached were deleted.
```

A tile of one flat colour is allowed to repeat: that is what open sea and
empty land look like, and an area on the coast can be half sea. An error
picture has writing on it, so it never is.

## Notice

Driving still uses the public OSRM demo server, and walking, cycling and routes
that avoid tolls or highways use the public FOSSGIS Valhalla server. Both are
for personal, educational or experimental use and neither is a production
backend. Run your own if you are doing this at any scale.

`app.py` (v1) is untouched and still fetches from the public OSM servers. The
policy above applies to it too.
