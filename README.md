# 🗺️ Offline Map Downloader


<div align="center">

[![Offline Map Downloader](./misc/demo.gif)](https://youtu.be/uJirSqlyhA4)
<p>Simple Python-Flask App</p>
</div>


This is a Flask web application that allows you to **select a geographic area on a map** and download OpenStreetMap or Satellite tiles as a `.zip` or `.mbtiles` file for offline use.

---

## ⚠️ Important Notice

This project is intended for **personal, educational, or experimental use only**.

It does **not use any API keys or authenticated tile services**, and fetches tiles directly from public endpoints like OpenStreetMap and ArcGIS. As such:

> **Do not use this tool for commercial applications or large-scale automated downloads.**  
> Please respect the tile providers' usage policies.

---

## 🔧 Features

- 📍 Select area with a rectangle on the map  
- 🔍 Choose zoom level range (10–19)  
- 🌐 Switch between OpenStreetMap and Satellite view  
- 🧮 Preview tile count before download  
- 🎨 Live preview of selected area using actual map tiles  
- 💾 Export to `.zip` or `.mbtiles`  

---

## 🚀 Getting Started

### 1. Clone the Repository

```bash
git clone https://github.com/0015/OfflineMapDownloader.git
cd OfflineMapDownloader
```

### 2. Create & Activate Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate  # macOS/Linux
# OR
.venv\Scripts\activate     # Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the App

```bash
python app.py
```

Then open your browser and go to:  
👉 [http://127.0.0.1:5000](http://127.0.0.1:5000)

---

## 📁 Output Formats

- `tiles.zip` – folder structure with PNG tiles by zoom/x/y  
- `tiles.mbtiles` – SQLite-based format (flat database file)  

---

## 🛠 Dependencies

- `Flask`
- `requests`

See [`requirements.txt`](./requirements.txt)

---

## 📝 License

MIT License  
(c) 2025 Eric Nam / ThatProject

---

## 🌐 Attribution

Map tiles provided by:
- [OpenStreetMap](https://www.openstreetmap.org)
- [Esri Satellite Imagery](https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9)

---

## 🙌 Credits & Reference

This project was inspired by [AliFlux/MapTilesDownloader](https://github.com/AliFlux/MapTilesDownloader)  
Special thanks to their work on simplifying tile downloading logic.

Created by [@ThatProject](https://github.com/0015)
