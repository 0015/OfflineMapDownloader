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
    "satellite": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
}

USER_AGENT = "OfflineTileDownloader/1.0 (+mailto:you@example.com)"
MAX_TILE_COUNT = 20000
TILE_MARGIN = 1
REQUEST_DELAY = 0.1  # 100ms delay between requests
MAX_WORKERS = 4  # Number of concurrent download threads

download_progress = {}

def download_tile_with_retry(url, headers, max_retries=3):
    """Download a tile with exponential backoff retry logic."""
    for attempt in range(max_retries):
        try:
            # Add base delay plus jitter to avoid thundering herd
            jitter = random.uniform(0, 0.1)
            time.sleep(REQUEST_DELAY + jitter)
            
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                return r.content
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
    url = url_template.format(z=z, x=x, y=y)
    headers = {"User-Agent": USER_AGENT}
    
    tile_data = download_tile_with_retry(url, headers)
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
                result = create_mbtiles(tiles, job_id, map_style)
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

def create_mbtiles(tiles, job_id, map_style):
    tmpfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mbtiles")
    conn = sqlite3.connect(tmpfile.name)
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
        CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
    """)
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("name", "Offline Map"))
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("type", "baselayer"))
    cursor.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", ("format", "png"))

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


def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y

if __name__ == '__main__':
    app.run(debug=True)
