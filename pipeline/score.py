#!/usr/bin/env python3
"""
Turn incidents_zoned.csv + racks.geojson into a per-zone letter grade.

    python score.py

Writes site/data/zones.json.

Methodology (documented here because it's a judgment call, not a fact):
  - Count qualifying incidents per zone: matched to a zone, and
    within_window == True (theft discovered within 7 days of last-seen, so
    a vague "sometime in the last month" report doesn't pin blame on
    whichever zone happened to get text-matched).
  - Normalize by rack capacity in that zone (incidents per 40 capacity
    slots -- a round unit) so a zone with many more racks doesn't look
    worse just for hosting more bikes.
  - Rank all zones that have at least one rack by that normalized rate and
    bucket into letter grades by quantile (worst ~15% -> F, next ~15% -> D,
    etc). Small sample size (dozens of incidents total across ~90 zones)
    means this is directional, not precise -- the UI must say so.
  - Zones with zero matched incidents get the best grade band, but the
    frontend must label that "no reports on file", never "verified safe" --
    it may just mean under-reported or rarely-checked, not risk-free.
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
SITE_DATA = HERE.parent / "site" / "data"
CAPACITY_UNIT = 40

GRADE_BANDS = [
    (0.15, "A+"), (0.30, "A"), (0.50, "B"), (0.70, "C"), (0.85, "D"), (1.01, "F"),
]


def grade_for_percentile(p):
    for cutoff, grade in GRADE_BANDS:
        if p <= cutoff:
            return grade
    return "F"


def main():
    zone_defs = {z["id"]: z for z in json.loads((HERE / "zones.json").read_text())["zones"]}

    racks = json.loads((SITE_DATA / "racks.geojson").read_text())["features"]
    capacity_by_zone, rack_count_by_zone = {}, {}
    for f in racks:
        p = f["properties"]
        zid = p.get("zone_id")
        if not zid:
            continue
        capacity_by_zone[zid] = capacity_by_zone.get(zid, 0) + (p.get("capacity") or 0)
        rack_count_by_zone[zid] = rack_count_by_zone.get(zid, 0) + 1

    incidents = list(csv.DictReader(open(HERE / "incidents_zoned.csv", encoding="utf-8")))
    incident_count_by_zone = {}
    for r in incidents:
        zid = r.get("zone_id")
        if zid and r.get("within_window") == "True":
            incident_count_by_zone[zid] = incident_count_by_zone.get(zid, 0) + 1

    scored_zones = [zid for zid in zone_defs if zid in capacity_by_zone]

    rated = []
    for zid in scored_zones:
        capacity = capacity_by_zone[zid]
        incidents_n = incident_count_by_zone.get(zid, 0)
        rate = incidents_n / max(capacity / CAPACITY_UNIT, 0.1)
        rated.append((zid, incidents_n, capacity, rate))

    # Rank grades only among zones that actually have incidents -- lumping
    # zero-incident zones into the same percentile pool pushes every
    # incident zone into the bottom bands regardless of how it compares to
    # other incident zones.
    zero = [t for t in rated if t[1] == 0]
    nonzero = sorted((t for t in rated if t[1] > 0), key=lambda t: t[3])
    n = len(nonzero)

    grade_by_zid = {zid: "A+" for zid, *_ in zero}
    for i, (zid, *_rest) in enumerate(nonzero):
        percentile = (i + 1) / n if n else 0
        grade_by_zid[zid] = grade_for_percentile(percentile)

    out = []
    for zid, incidents_n, capacity, rate in rated:
        grade = grade_by_zid[zid]
        z = zone_defs[zid]
        out.append({
            "id": zid,
            "name": z["name"],
            "lat": z["lat"],
            "lon": z["lon"],
            "grade": grade,
            "incident_count": incidents_n,
            "rack_capacity": capacity,
            "rack_count": rack_count_by_zone.get(zid, 0),
            "incidents_per_40_capacity": round(rate, 3),
            "has_reports": incidents_n > 0,
        })

    unscored = sorted(set(zone_defs) - set(scored_zones))
    if unscored:
        print(f"  {len(unscored)} zones have no capacity data, left unscored: "
              f"{unscored[:5]}{'...' if len(unscored) > 5 else ''}")

    SITE_DATA.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "zones": out,
    }
    (SITE_DATA / "zones.json").write_text(json.dumps(payload, indent=2))

    dist = {}
    for z in out:
        dist[z["grade"]] = dist.get(z["grade"], 0) + 1
    print(f"{len(out)} zones scored -> {SITE_DATA / 'zones.json'}")
    print(f"  grade distribution: {dist}")
    print(f"  total qualifying incidents matched to a scored zone: "
          f"{sum(z['incident_count'] for z in out)}")


if __name__ == "__main__":
    main()
