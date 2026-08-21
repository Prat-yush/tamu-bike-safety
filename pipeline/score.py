#!/usr/bin/env python3
"""
Turn incidents_zoned.csv + racks.geojson into a per-zone letter grade.

    python score.py

Writes site/data/zones.json.

Methodology (documented here because it's a judgment call, not a fact):

  1. Recency weighting. Each qualifying incident (matched to a zone,
     within_window == True -- theft discovered within 7 days of last-seen,
     so a vague "sometime last month" report doesn't pin blame on whichever
     zone happened to text-match) is weighted by
     0.5 ** (days_since_incident / RECENCY_HALFLIFE_DAYS). A theft from
     over a year ago counts for much less than one from last month -- the
     grade should reflect current risk, not permanently brand a zone for
     one bad week over a year ago.

  2. Empirical-Bayes shrinkage instead of a raw ratio. Raw
     incidents/capacity is dominated by noise: with a median zone capacity
     of ~75 racks (~1.9 "40-capacity units"), a single incident swings a
     zone from A+ to F territory on its own, and a zone with zero incidents
     gets treated as equally "safe" whether it has 5 racks or 800. Instead
     each zone's rate is blended toward the campus-wide average rate,
     weighted by how much capacity (evidence) that zone has:

         smoothed_rate = (weighted_incidents + K * global_rate)
                          / (capacity_units + K)

     A zone with little capacity (little evidence) gets pulled toward the
     campus average; a zone with a lot of capacity and a real track record
     is barely adjusted. K is in units of "40-capacity blocks" of prior
     evidence -- see PRIOR_STRENGTH below.

  3. Grades are assigned by percentile of smoothed_rate across ALL scored
     zones (no more special-casing zero-incident zones -- shrinkage already
     handles them correctly relative to their capacity).

  Small absolute sample size (dozens of incidents total across ~90 zones)
  means this is still directional, not statistically precise -- the UI
  must say so, and a low grade should never be read as "verified unsafe"
  any more than a high one means "verified safe."
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
SITE_DATA = HERE.parent / "site" / "data"

CAPACITY_UNIT = 40          # "one unit" of rack capacity, for readable rates
RECENCY_HALFLIFE_DAYS = 365  # a 1-year-old incident counts for half as much
PRIOR_STRENGTH = 3.0         # shrinkage strength, in capacity units

GRADE_BANDS = [
    (0.15, "A+"), (0.30, "A"), (0.50, "B"), (0.70, "C"), (0.85, "D"), (1.01, "F"),
]


def grade_for_percentile(p):
    for cutoff, grade in GRADE_BANDS:
        if p <= cutoff:
            return grade
    return "F"


def incident_date(row):
    for field in ("discovered_missing", "last_seen"):
        v = row.get(field)
        if v:
            try:
                return datetime.fromisoformat(v)
            except ValueError:
                pass
    if row.get("alert_date"):
        try:
            return datetime.strptime(row["alert_date"], "%Y-%m-%d")
        except ValueError:
            pass
    return None


def recency_weight(dt, now):
    if dt is None:
        return 0.5  # unknown date: don't let it vanish, don't let it dominate
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = max((now - dt).total_seconds() / 86400, 0)
    return 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS)


def main():
    now = datetime.now(timezone.utc)
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
    raw_count_by_zone, weighted_by_zone = {}, {}
    for r in incidents:
        zid = r.get("zone_id")
        if not zid or r.get("within_window") != "True":
            continue
        raw_count_by_zone[zid] = raw_count_by_zone.get(zid, 0) + 1
        w = recency_weight(incident_date(r), now)
        weighted_by_zone[zid] = weighted_by_zone.get(zid, 0.0) + w

    scored_zones = [zid for zid in zone_defs if zid in capacity_by_zone]

    # Campus-wide average rate, used as the shrinkage target.
    total_weighted = sum(weighted_by_zone.get(zid, 0.0) for zid in scored_zones)
    total_units = sum(capacity_by_zone[zid] / CAPACITY_UNIT for zid in scored_zones)
    global_rate = total_weighted / total_units if total_units else 0.0

    rated = []
    for zid in scored_zones:
        capacity = capacity_by_zone[zid]
        units = capacity / CAPACITY_UNIT
        weighted = weighted_by_zone.get(zid, 0.0)
        smoothed_rate = (weighted + PRIOR_STRENGTH * global_rate) / (units + PRIOR_STRENGTH)
        rated.append({
            "zid": zid,
            "incidents_n": raw_count_by_zone.get(zid, 0),
            "weighted": weighted,
            "capacity": capacity,
            "units": units,
            "smoothed_rate": smoothed_rate,
        })

    rated.sort(key=lambda t: t["smoothed_rate"])
    n = len(rated)
    for i, t in enumerate(rated):
        percentile = (i + 1) / n if n else 0
        t["grade"] = grade_for_percentile(percentile)

    out = []
    for t in rated:
        z = zone_defs[t["zid"]]
        # "Confidence" is just how much evidence (capacity) backs this
        # zone's rate relative to the shrinkage prior -- exposed so the
        # frontend can flag thin-data zones rather than imply precision
        # the data doesn't have.
        confidence = "low" if t["units"] < PRIOR_STRENGTH else (
            "medium" if t["units"] < 3 * PRIOR_STRENGTH else "high")
        out.append({
            "id": t["zid"],
            "name": z["name"],
            "lat": z["lat"],
            "lon": z["lon"],
            "grade": t["grade"],
            "incident_count": t["incidents_n"],
            "weighted_incident_count": round(t["weighted"], 2),
            "rack_capacity": t["capacity"],
            "rack_count": rack_count_by_zone.get(t["zid"], 0),
            "smoothed_incidents_per_40_capacity": round(t["smoothed_rate"], 3),
            "confidence": confidence,
            "has_reports": t["incidents_n"] > 0,
        })

    unscored = sorted(set(zone_defs) - set(scored_zones))
    if unscored:
        print(f"  {len(unscored)} zones have no capacity data, left unscored: "
              f"{unscored[:5]}{'...' if len(unscored) > 5 else ''}")

    SITE_DATA.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now.isoformat(),
        "methodology": {
            "recency_halflife_days": RECENCY_HALFLIFE_DAYS,
            "prior_strength_capacity_units": PRIOR_STRENGTH,
            "capacity_unit": CAPACITY_UNIT,
            "campus_wide_rate_per_40_capacity": round(global_rate, 4),
        },
        "zones": out,
    }
    (SITE_DATA / "zones.json").write_text(json.dumps(payload, indent=2))

    dist = {}
    for z in out:
        dist[z["grade"]] = dist.get(z["grade"], 0) + 1
    print(f"{len(out)} zones scored -> {SITE_DATA / 'zones.json'}")
    print(f"  grade distribution: {dist}")
    print(f"  campus-wide rate: {global_rate:.3f} incidents / 40 capacity")
    print(f"  raw incidents matched to a scored zone: "
          f"{sum(z['incident_count'] for z in out)}")


if __name__ == "__main__":
    main()
