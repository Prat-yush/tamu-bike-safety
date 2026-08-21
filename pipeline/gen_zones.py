#!/usr/bin/env python3
"""
One-time (re-run only if the rack layout changes a lot) zone generator:
cluster all bike rack points by proximity, then reverse-geocode each
cluster's centroid via OSM Nominatim to get a human-readable label.

    python gen_zones.py

Writes zones.json. Labels are a starting point, not gospel -- hand-edit
zones.json afterward for any zone whose auto-label is wrong or too generic
(Nominatim doesn't know every campus building). The alias list per zone is
intentionally left thin; grow it as zonejoin.py's needs_review.csv reveals
real incident phrasing that doesn't match yet.
"""

import json
import math
import time
from pathlib import Path

import requests

ARCGIS = "https://arc.ts.tamu.edu/arcgis/rest/services/Hosted"
LAYERS = [("Rack_Locations_view", 0), ("Rack_Locations_view", 1), ("Rack_Locations_view", 2)]
PAGE_SIZE = 1000
CLUSTER_RADIUS_M = 150
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
UA = "tamu-bike-safety-project (student project, one-time reverse-geocode pass)"

OUT_PATH = Path(__file__).parent / "zones.json"


def fetch_points():
    pts = []
    for layer, sublayer in LAYERS:
        url = f"{ARCGIS}/{layer}/FeatureServer/{sublayer}/query"
        offset = 0
        while True:
            r = requests.get(url, params={
                "where": "1=1", "outFields": "objectid", "outSR": 4326,
                "f": "json", "resultRecordCount": PAGE_SIZE, "resultOffset": offset,
            }, timeout=30)
            r.raise_for_status()
            batch = r.json().get("features", [])
            for f in batch:
                g = f.get("geometry") or {}
                if g.get("x") is not None:
                    pts.append((g["y"], g["x"]))
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
    return pts


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cluster(points, radius_m):
    """Greedy single-pass clustering: each point joins the nearest existing
    cluster within radius, else starts a new one. Good enough for a bike
    rack layout that's mostly tight, isolated clumps."""
    clusters = []  # list of {"lat":, "lon":, "pts":[...]}
    for lat, lon in points:
        best, best_d = None, radius_m
        for c in clusters:
            d = haversine_m(lat, lon, c["lat"], c["lon"])
            if d < best_d:
                best, best_d = c, d
        if best:
            best["pts"].append((lat, lon))
            best["lat"] = sum(p[0] for p in best["pts"]) / len(best["pts"])
            best["lon"] = sum(p[1] for p in best["pts"]) / len(best["pts"])
        else:
            clusters.append({"lat": lat, "lon": lon, "pts": [(lat, lon)]})
    return clusters


def reverse_geocode(lat, lon):
    try:
        r = requests.get(NOMINATIM, params={
            "lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18,
        }, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        d = r.json()
        addr = d.get("address", {})
        name = (d.get("name") or addr.get("building") or addr.get("amenity")
                or addr.get("leisure") or addr.get("road") or "")
        return name or None
    except Exception:
        return None


def main():
    points = fetch_points()
    print(f"{len(points)} rack points fetched")

    clusters = cluster(points, CLUSTER_RADIUS_M)
    print(f"{len(clusters)} zones from {CLUSTER_RADIUS_M}m clustering")

    zones = []
    seen_names = {}
    for i, c in enumerate(clusters):
        name = reverse_geocode(c["lat"], c["lon"]) or f"Zone {i+1}"
        time.sleep(1.05)  # Nominatim usage policy: max 1 req/sec
        base = name
        n = seen_names.get(base, 0)
        seen_names[base] = n + 1
        if n:
            name = f"{base} ({n+1})"
        zones.append({
            "id": f"zone-{i+1:03d}",
            "name": name,
            "lat": round(c["lat"], 6),
            "lon": round(c["lon"], 6),
            "rack_point_count": len(c["pts"]),
            "aliases": [],
        })
        print(f"  {i+1}/{len(clusters)}: {name} ({len(c['pts'])} racks)")

    OUT_PATH.write_text(json.dumps({"zones": zones}, indent=2))
    print(f"-> {OUT_PATH}")


if __name__ == "__main__":
    main()
