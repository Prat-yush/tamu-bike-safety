#!/usr/bin/env python3
"""
Match incidents.csv's free-text location_raw to a zone in zones.json.

    python zonejoin.py

Two strategies, tried in order:
  1. The consolidated table format usually embeds a real street address in
     parens, e.g. "Krueger Residence Hall Bike Rack (722 Lubbock St,
     College Station)". Forward-geocode that address (Nominatim, cached in
     geocode_cache.json so repeat addresses across runs are free) and pick
     the nearest zone spatially. This is ground truth, not a guess.
  2. Otherwise (older narrative rows with no address), fuzzy-match the free
     text against each zone's name + aliases.

Writes incidents_zoned.csv (adds zone_id, zone_name, zone_match_method,
zone_match_score) and needs_review.csv (rows that didn't confidently match
-- grow zones.json's alias lists based on what shows up here).
"""

import csv
import difflib
import json
import math
import re
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
CACHE_PATH = HERE / "geocode_cache.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "tamu-bike-safety-project (student project; geocoding incident addresses)"

ADDR_RE = re.compile(r"\(([^)]*College Station[^)]*)\)", re.I)
SUITE_RE = re.compile(r",?\s*Ste\.?\s*\S+", re.I)
ABBR_MAP = [
    (re.compile(r"\bBl\b\.?", re.I), "Blvd"),
    (re.compile(r"\bDr\b\.?", re.I), "Drive"),
    (re.compile(r"\bPw\b\.?", re.I), "Pkwy"),
    (re.compile(r"\bLn\b\.?", re.I), "Lane"),
]


def clean_address(addr: str) -> str:
    addr = SUITE_RE.sub("", addr)
    for pat, repl in ABBR_MAP:
        addr = pat.sub(repl, addr)
    return addr.strip()
STOPWORDS = {"the", "a", "an", "at", "near", "in", "on", "of", "building",
             "hall", "bike", "racks", "rack", "apartments", "apartment"}
GEOCODE_MAX_DIST_M = 400
FUZZY_THRESHOLD = 0.6


def normalize(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    words = [w for w in s.split() if w not in STOPWORDS]
    return " ".join(words)


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_zones():
    zones = json.loads((HERE / "zones.json").read_text())["zones"]
    for z in zones:
        candidates = [z["name"]] + z.get("aliases", [])
        z["_norm_candidates"] = [normalize(c) for c in candidates if c]
    return zones


def nearest_zone(lat, lon, zones):
    best, best_d = None, float("inf")
    for z in zones:
        d = haversine_m(lat, lon, z["lat"], z["lon"])
        if d < best_d:
            best, best_d = z, d
    return best, best_d


def fuzzy_score(loc_norm, zone):
    if not loc_norm:
        return 0.0
    best = 0.0
    for cand in zone["_norm_candidates"]:
        if not cand:
            continue
        if cand in loc_norm or loc_norm in cand:
            best = max(best, 0.9)
        best = max(best, difflib.SequenceMatcher(None, cand, loc_norm).ratio())
    return best


def fuzzy_best(location_raw, zones):
    loc_norm = normalize(location_raw)
    scored = [(fuzzy_score(loc_norm, z), z) for z in zones]
    scored.sort(key=lambda t: -t[0])
    return scored[0] if scored else (0.0, None)


class Geocoder:
    def __init__(self):
        self.cache = {}
        if CACHE_PATH.exists():
            self.cache = json.loads(CACHE_PATH.read_text())
        self.calls = 0

    def geocode(self, address):
        if address in self.cache:
            return self.cache[address]
        self.calls += 1
        if self.calls > 1:
            time.sleep(1.05)  # Nominatim usage policy: max 1 req/sec
        try:
            r = requests.get(NOMINATIM, params={
                "q": address, "format": "jsonv2", "limit": 1,
                "countrycodes": "us",
            }, headers={"User-Agent": UA}, timeout=15)
            r.raise_for_status()
            results = r.json()
            latlon = (float(results[0]["lat"]), float(results[0]["lon"])) if results else None
        except Exception:
            latlon = None
        self.cache[address] = latlon
        return latlon

    def save(self):
        CACHE_PATH.write_text(json.dumps(self.cache, indent=2))


def main():
    zones = load_zones()
    rows = list(csv.DictReader(open(HERE / "incidents.csv", encoding="utf-8")))
    geocoder = Geocoder()

    zoned, review = [], []
    n_geocode_match = n_fuzzy_match = 0
    for r in rows:
        loc = r.get("location_raw", "")
        zone, method, score = None, "", 0.0

        m = ADDR_RE.search(loc) if loc else None
        if m:
            latlon = geocoder.geocode(clean_address(m.group(1)) + ", TX")
            if latlon:
                zone, dist_m = nearest_zone(latlon[0], latlon[1], zones)
                if dist_m <= GEOCODE_MAX_DIST_M:
                    method, score = "geocode", round(dist_m, 1)
                else:
                    zone = None

        if zone is None and loc:
            fscore, fzone = fuzzy_best(loc, zones)
            if fzone is not None and fscore >= FUZZY_THRESHOLD:
                zone, method, score = fzone, "fuzzy", round(fscore, 3)

        matched = zone is not None
        r["zone_id"] = zone["id"] if matched else ""
        r["zone_name"] = zone["name"] if matched else ""
        r["zone_match_method"] = method
        r["zone_match_score"] = score
        zoned.append(r)
        if matched:
            n_geocode_match += method == "geocode"
            n_fuzzy_match += method == "fuzzy"
        else:
            review.append(r)

    geocoder.save()

    fieldnames = list(zoned[0].keys()) if zoned else []
    with open(HERE / "incidents_zoned.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(zoned)
    with open(HERE / "needs_review.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(review)

    print(f"{len(zoned)} incidents -> incidents_zoned.csv")
    print(f"  geocode-matched: {n_geocode_match}   fuzzy-matched: {n_fuzzy_match}"
          f"   needs_review: {len(review)}")
    print(f"  ({geocoder.calls} live geocode calls this run)")


if __name__ == "__main__":
    main()
