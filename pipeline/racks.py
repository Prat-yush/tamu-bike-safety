#!/usr/bin/env python3
"""
Pull TAMU Transportation Services bike rack layers from the public ArcGIS
REST API and assign each rack to the nearest zone in zones.json.

    python racks.py

Writes site/data/racks.geojson.
"""

import json
import math
from pathlib import Path

import requests

ARCGIS = "https://arc.ts.tamu.edu/arcgis/rest/services/Hosted"
LAYERS = [
    ("Rack_Locations_view", 0, "hub"),
    ("Rack_Locations_view", 1, "bikeshare"),
    ("Rack_Locations_view", 2, "regular"),
]
PAGE_SIZE = 1000

ZONES_PATH = Path(__file__).parent / "zones.json"
OUT_PATH = Path(__file__).parent.parent / "site" / "data" / "racks.geojson"


def fetch_layer(layer, sublayer):
    url = f"{ARCGIS}/{layer}/FeatureServer/{sublayer}/query"
    features = []
    offset = 0
    while True:
        r = requests.get(url, params={
            "where": "1=1",
            "outFields": "objectid,type,typequantity,total_capacity",
            "outSR": 4326,
            "f": "json",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
        }, timeout=30)
        r.raise_for_status()
        data = r.json()
        batch = data.get("features", [])
        features.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return features


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_zone(lat, lon, zones):
    best, best_d = None, float("inf")
    for z in zones:
        d = haversine_m(lat, lon, z["lat"], z["lon"])
        if d < best_d:
            best, best_d = z, d
    return best, best_d


def main():
    zones = json.loads(ZONES_PATH.read_text())["zones"]

    out_features = []
    counts = {}
    for layer, sublayer, kind in LAYERS:
        feats = fetch_layer(layer, sublayer)
        counts[kind] = len(feats)
        for f in feats:
            geom = f.get("geometry") or {}
            lon, lat = geom.get("x"), geom.get("y")
            if lon is None or lat is None:
                continue
            attrs = f.get("attributes", {})
            zone, dist_m = nearest_zone(lat, lon, zones)
            out_features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "id": f"{kind}-{attrs.get('objectid')}",
                    "kind": kind,
                    "rack_type": attrs.get("type"),
                    "typequantity": attrs.get("typequantity"),
                    "capacity": attrs.get("total_capacity"),
                    "zone_id": zone["id"] if zone else None,
                    "zone_dist_m": round(dist_m, 1) if zone else None,
                },
            })

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": out_features,
    }))

    print(f"{sum(counts.values())} racks fetched {counts} -> {OUT_PATH}")
    far = [f for f in out_features if (f["properties"]["zone_dist_m"] or 0) > 150]
    if far:
        print(f"  {len(far)} racks are >150m from their nearest zone centroid "
              f"-- consider adding a zone for them")


if __name__ == "__main__":
    main()
