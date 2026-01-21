"""
Offline Tile Downloader (Flask-based Web App)
---------------------------------------------

This Python Flask application allows users to download OpenStreetMap tiles for offline use,
based on a selected geographic bounding box and zoom levels.

Key Features:
- Supports two download formats: ZIP and MBTiles.
- Automatically fetches and stores map tiles from the public OSM tile server.
- Caches downloaded tiles to avoid redundant requests.
- Includes an adjustable TILE_MARGIN option to download extra rows/columns of tiles around the selected area, 
  useful to prevent missing edge tiles on display devices.
- Enforces a maximum tile count to prevent excessive server load (default: 20,000).

Endpoints:
- `/`: Renders the HTML UI.
- `/preview_tile_count`: Calculates how many tiles will be downloaded (with margin).
- `/download_tiles`: Downloads the tiles in the specified format (ZIP or MBTiles).

Note:
- Be respectful to the OpenStreetMap tile server (includes custom User-Agent).
- If using this for heavy downloads, consider setting up your own tile server.

"""
import os
import math
import sqlite3
import requests
import zipfile
import io
import uuid
import threading
import time
import tempfile
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, send_file, render_template, jsonify

app = Flask(__name__)

TILE_SERVERS = {
    "map": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "satellite": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "bing": "http://ecn.t{s}.tiles.virtualearth.net/tiles/a{quadkey}.jpeg?g=1"
}

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
MAX_TILE_COUNT = 20000
TILE_MARGIN = 1
REQUEST_DELAY = 0.1  # 100ms delay between requests
MAX_WORKERS = 4  # Number of concurrent download threads

download_progress = {}

def is_valid_image_data(data, map_style):
    """Validate that downloaded data is a valid image."""
    if not data or len(data) < 10:
        return False
    
    # Check for JPEG format (Bing Maps uses JPEG)
    if map_style == "bing":
        # JPEG files start with FF D8 FF
        if data[:3] == b'\xff\xd8\xff':
            return True
        return False
    else:
        # PNG files start with 89 50 4E 47
        if data[:4] == b'\x89PNG':
            return True
        return False

def download_tile_with_retry(url, headers, max_retries=3, map_style="map"):
    """Download a tile with exponential backoff retry logic."""
    for attempt in range(max_retries):
        try:
            # Add base delay plus jitter to avoid thundering herd
            jitter = random.uniform(0, 0.1)
            time.sleep(REQUEST_DELAY + jitter)
            
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                content = r.content
                # Validate that the content is actually an image
                if is_valid_image_data(content, map_style):
                    return content
                else:
                    print(f"Invalid image data from {url} (size: {len(content)} bytes)")
                    if attempt < max_retries - 1:
                        wait_time = (2 ** attempt) + random.uniform(0, 1)
                        time.sleep(wait_time)
                    continue
            elif r.status_code == 429:  # Too Many Requests
                wait_time = (2 ** attempt) + random.uniform(0, 1)
                print(f"Rate limited on {url}, waiting {wait_time:.2f}s before retry {attempt + 1}")
                time.sleep(wait_time)
                continue
            elif r.status_code == 404:
                # Tile doesn't exist, no point retrying
                print(f"Tile not found: {url}")
                return None
            else:
                print(f"HTTP {r.status_code} for {url}")
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait_time)
                continue
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                print(f"Failed to download {url} after {max_retries} attempts: {e}")
                return None
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            print(f"Request failed for {url}, waiting {wait_time:.2f}s before retry {attempt + 1}: {e}")
            time.sleep(wait_time)
    
    return None

def download_single_tile(z, x, y, map_style, job_id):
    """Download a single tile and return the tile info and data."""
    tile_base_path = f'tiles/{map_style}'
    tile_path = f'{tile_base_path}/{z}/{x}/{y}.png'
    
    # Check if tile already exists in cache
    if os.path.exists(tile_path):
        with open(tile_path, 'rb') as f:
            return (z, x, y, f.read(), True)  # True indicates cached
    
    # Download tile
    url_template = TILE_SERVERS.get(map_style, TILE_SERVERS["map"])
    
    # Handle Bing Maps tiles (uses QuadKey encoding)
    if map_style == "bing":
        quadkey = tile_to_quadkey(x, y, z)
        # Use subdomain based on tile coordinates for load balancing (0-3)
        subdomain = (x + y) % 4
        url = url_template.format(s=subdomain, quadkey=quadkey)
    else:
        url = url_template.format(z=z, x=x, y=y)
    
    headers = {"User-Agent": USER_AGENT}
    
    tile_data = download_tile_with_retry(url, headers, map_style=map_style)
    if tile_data:
        # Save to cache
        os.makedirs(os.path.dirname(tile_path), exist_ok=True)
        with open(tile_path, 'wb') as f:
            f.write(tile_data)
        return (z, x, y, tile_data, False)  # False indicates downloaded
    else:
        return (z, x, y, None, False)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/preview_tile_count', methods=['POST'])
def preview_tile_count():
    data = request.get_json()
    bounds = data.get('bounds')
    zoom_levels = data.get('zoom_levels')
    if not bounds or not zoom_levels:
        return jsonify({"error": "Missing bounds or zoom levels"}), 400

    total = 0
    for z in zoom_levels:
        x1, y1 = deg2num(bounds['north'], bounds['west'], z)
        x2, y2 = deg2num(bounds['south'], bounds['east'], z)
        x_min, x_max = sorted([x1, x2])
        y_min, y_max = sorted([y1, y2])
        total += (x_max - x_min + 1 + TILE_MARGIN * 2) * (y_max - y_min + 1 + TILE_MARGIN * 2)

        if total > MAX_TILE_COUNT:
            return jsonify({"error": f"Too many tiles: {total}"}), 400

    return jsonify({"tile_count": total})

@app.route('/download_tiles', methods=['POST'])
def download_tiles():
    data = request.get_json()
    bounds = data['bounds']
    zoom_levels = data['zoom_levels']
    fmt = data.get('format', 'zip')
    map_style = data.get('map_style', 'map')  #
    job_id = str(uuid.uuid4())
    download_progress[job_id] = {"progress": 0, "total": 1, "done": False, "error": None, "file": None}

    def worker():
        try:
            tiles = []
            for z in zoom_levels:
                x1, y1 = deg2num(bounds['north'], bounds['west'], z)
                x2, y2 = deg2num(bounds['south'], bounds['east'], z)
                x_min, x_max = sorted([x1, x2])
                y_min, y_max = sorted([y1, y2])
                for x in range(x_min - TILE_MARGIN, x_max + 1 + TILE_MARGIN):
                    for y in range(y_min - TILE_MARGIN, y_max + 1 + TILE_MARGIN):
                        tiles.append((z, x, y))

            download_progress[job_id]["total"] = len(tiles)
            
            print(f"Starting download of {len(tiles)} tiles for job {job_id}")
            if fmt == "mbtiles":
                result = create_mbtiles(tiles, job_id, map_style, bounds, zoom_levels)
            else:
                result = create_zip(tiles, job_id, map_style)

            if result:
                download_progress[job_id]["done"] = True
                download_progress[job_id]["file"] = result
                download_progress[job_id]["format"] = fmt
                download_progress[job_id]["style"] = map_style
                print(f"Download completed for job {job_id}")
            else:
                download_progress[job_id]["error"] = "Failed to create file"
                print(f"Failed to create file for job {job_id}")

        except Exception as e:
            error_msg = f"Download failed: {str(e)}"
            download_progress[job_id]["error"] = error_msg
            print(f"Exception in worker for job {job_id}: {error_msg}")

    threading.Thread(target=worker).start()
    return jsonify({"job_id": job_id})

@app.route('/progress/<job_id>')
def progress(job_id):
    def generate():
        while True:
            prog = download_progress.get(job_id)
            if not prog:
                yield "data: error\n\n"
                break

            yield f"data: {prog['progress']} / {prog['total']}\n\n"
            if prog["done"] or prog["error"]:
                break
            time.sleep(0.5)

    return app.response_class(generate(), mimetype='text/event-stream')

@app.route('/get_file/<job_id>')
def get_file(job_id):
    prog = download_progress.get(job_id)
    if not prog:
        print(f"get_file: Job {job_id} not found")
        return "Job not found", 404
    if not prog.get("file"):
        print(f"get_file: File for job {job_id} not ready")
        return "File not ready", 404

    file_obj = prog["file"]
    file_type = prog.get("format", "zip")
    style = prog.get("style", "map")

    style_prefix = "map" if style == "map" else "satellite"
    filename = f"{style_prefix}_tiles.{file_type}"

    return send_file(
        file_obj,
        as_attachment=True,
        download_name=filename,
        mimetype="application/octet-stream"
    )

def create_zip(tiles, job_id, map_style):
    zip_buffer = io.BytesIO()
    completed_tiles = 0
    
    with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
        # Use ThreadPoolExecutor for concurrent downloads
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit all download tasks
            future_to_tile = {
                executor.submit(download_single_tile, z, x, y, map_style, job_id): (z, x, y)
                for z, x, y in tiles
            }
            
            # Process completed downloads
            for future in as_completed(future_to_tile):
                z, x, y = future_to_tile[future]
                completed_tiles += 1
                download_progress[job_id]["progress"] = completed_tiles
                
                try:
                    result_z, result_x, result_y, tile_data, was_cached = future.result()
                    if tile_data:
                        zip_file.writestr(f'{z}/{x}/{y}.png', tile_data)
                        if not was_cached:
                            print(f"Downloaded tile {z}/{x}/{y}")
                    else:
                        print(f"Failed to download tile {z}/{x}/{y} after retries")
                except Exception as e:
                    print(f"Exception downloading tile {z}/{x}/{y}: {e}")

    zip_buffer.seek(0)
    return io.BytesIO(zip_buffer.read())

def create_mbtiles(tiles, job_id, map_style, bounds, zoom_levels):
    tmpfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mbtiles")
    conn = sqlite3.connect(tmpfile.name)
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
        CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
    """)
    
    # Calculate zoom range
    minzoom = min(zoom_levels)
    maxzoom = max(zoom_levels)
    
    # Format bounds as "west,south,east,north" (MBTiles specification)
    bounds_str = f"{bounds['west']},{bounds['south']},{bounds['east']},{bounds['north']}"
    
    # Determine image format based on map style
    # Bing Maps uses JPEG, others use PNG
    image_format = "jpg" if map_style == "bing" else "png"
    
    # Insert required metadata for MBTiles specification
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("name", "Offline Map"))
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("format", image_format))
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("version", "1.1"))
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("minzoom", str(minzoom)))
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("maxzoom", str(maxzoom)))
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("bounds", bounds_str))
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("type", "baselayer"))

    completed_tiles = 0
    
    # Use ThreadPoolExecutor for concurrent downloads
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all download tasks
        future_to_tile = {
            executor.submit(download_single_tile, z, x, y, map_style, job_id): (z, x, y)
            for z, x, y in tiles
        }
        
        # Process completed downloads
        for future in as_completed(future_to_tile):
            z, x, y = future_to_tile[future]
            completed_tiles += 1
            download_progress[job_id]["progress"] = completed_tiles
            
            try:
                result_z, result_x, result_y, tile_data, was_cached = future.result()
                if tile_data:
                    # Convert to TMS y coordinate for MBTiles format
                    tms_y = (2 ** z - 1) - y
                    cursor.execute(
                        "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
                        (z, x, tms_y, sqlite3.Binary(tile_data))
                    )
                    if not was_cached:
                        print(f"Downloaded tile {z}/{x}/{y}")
                else:
                    print(f"Failed to fetch {z}/{x}/{y} after retries")
            except Exception as e:
                print(f"Exception downloading tile {z}/{x}/{y}: {e}")

    conn.commit()
    conn.close()

    with open(tmpfile.name, 'rb') as f:
        mb_data = f.read()
    tmpfile.close()
    os.unlink(tmpfile.name)

    return io.BytesIO(mb_data)


def normalize_longitude(lon):
    """Normalize longitude to [-180, 180) range."""
    while lon >= 180:
        lon -= 360
    while lon < -180:
        lon += 360
    return lon

def deg2num(lat_deg, lon_deg, zoom):
    """Convert latitude/longitude to tile coordinates with proper bounds checking."""
    # Normalize longitude to valid range [-180, 180)
    lon_deg = normalize_longitude(lon_deg)
    
    # Clamp latitude to valid Web Mercator range (approximately -85.05 to 85.05)
    lat_deg = max(-85.0511287798, min(85.0511287798, lat_deg))
    
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    
    # Ensure coordinates are in valid range [0, n-1] to prevent negative or out-of-range values
    max_coord = int(n - 1)
    x = max(0, min(max_coord, x))
    y = max(0, min(max_coord, y))
    
    return x, y

def tile_to_quadkey(x, y, z):
    """Convert tile coordinates (x, y, z) to Bing Maps QuadKey."""
    quadkey = ""
    for i in range(z, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if (x & mask) != 0:
            digit += 1
        if (y & mask) != 0:
            digit += 2
        quadkey += str(digit)
    return quadkey

def num2deg(x, y, zoom):
    """Convert tile coordinates to latitude/longitude (inverse of deg2num)."""
    n = 2.0 ** zoom
    lon_deg = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg

if __name__ == '__main__':
    app.run(debug=True)
