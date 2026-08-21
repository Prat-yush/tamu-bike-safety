#!/usr/bin/env python3
"""
Turn incidents_zoned.csv + racks.geojson into a per-zone letter grade.

    python score.py

Writes site/data/zones.json.

Methodology (documented here because it's a judgment call, not a fact):

  Zones are split into two tracks, not one continuous scale. "No reports"
  and "reports show low risk" are different kinds of information, and a
  single A-to-F ladder always ends up implying the first is a weaker
  version of the second -- which isn't true.

  1. Zones with >=1 qualifying incident ("rated") get a real letter grade:

     a. Recency weighting. Each qualifying incident (matched to a zone,
        within_window == True -- theft discovered within 7 days of
        last-seen, so a vague "sometime last month" report doesn't pin
        blame on whichever zone happened to text-match) is weighted by
        0.5 ** (days_since_incident / RECENCY_HALFLIFE_DAYS). A theft from
        over a year ago counts for much less than one from last month.

     b. Empirical-Bayes shrinkage. Raw incidents/capacity is dominated by
        noise at this sample size, so each rated zone's rate is blended
        toward the campus-wide average rate (computed across ALL zones,
        rated and unrated, so it reflects true campus-wide incidence),
        weighted by how much capacity (evidence) that zone has:

            smoothed_rate = (weighted_incidents + K * global_rate)
                             / (capacity_units + K)

        A zone with a little capacity gets pulled toward the campus
        average; a zone with a lot of capacity and a real track record is
        barely adjusted. K (PRIOR_STRENGTH) is in units of "40-capacity
        blocks" of prior evidence.

     c. Grades are percentile bands of smoothed_rate computed only among
        rated zones -- comparing "zones with a track record" against each
        other, not diluted by zones that have no track record at all.

  2. Zones with zero qualifying incidents ("no_reports") get an ESTIMATED
     letter grade instead of no grade at all: inverse-distance-weighted
     average of the smoothed_rate of the nearest rated zones within
     NEIGHBOR_MAX_M, mapped through the same grade bands as real zones (by
     interpolating where that rate would fall in the rated distribution --
     never by averaging letter grades themselves, which is lossy). A zone
     with no rated neighbor within range falls back to the flat
     campus-wide rate. Either way the zone keeps status "no_reports" and
     estimated: true, and its own confidence tier reflects how good the
     *estimate* is (how many/how close the real neighbors were) -- not
     reused from the capacity-based tier, since there's no "own" evidence
     behind an estimate at all. The frontend must render these distinctly
     (lower opacity, a "~" prefix) and say plainly that they're inferred,
     not measured -- an estimated grade is not a report.

  Small absolute sample size (dozens of incidents total across ~90 zones)
  means rated grades are still directional, not statistically precise --
  the UI must say so, and a low grade should never be read as "verified
  unsafe" any more than "no reports" means "verified safe."
"""

import bisect
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
SITE_DATA = HERE.parent / "site" / "data"

CAPACITY_UNIT = 40           # "one unit" of rack capacity, for readable rates
RECENCY_HALFLIFE_DAYS = 365  # a 1-year-old incident counts for half as much
PRIOR_STRENGTH = 3.0         # shrinkage strength, in capacity units

NEIGHBOR_K = 3         # consider up to this many nearest rated zones
NEIGHBOR_MAX_M = 600   # ...but only within this radius; beyond it, use the campus average
NEIGHBOR_SOFTEN_M = 25  # avoids a near-zero distance producing a runaway weight

GRADE_BANDS = [
    (0.15, "A+"), (0.30, "A"), (0.50, "B"), (0.70, "C"), (0.85, "D"), (1.01, "F"),
]


def grade_for_percentile(p):
    for cutoff, grade in GRADE_BANDS:
        if p <= cutoff:
            return grade
    return "F"


def grade_for_rate(rate, sorted_rates):
    """Where would this rate fall among the real rated zones? Reuses the
    exact same bands, so an estimated "B" means the same thing a real "B"
    does -- just measured on borrowed evidence."""
    if not sorted_rates:
        return "C"  # degenerate: no rated zones exist campus-wide yet
    idx = bisect.bisect_left(sorted_rates, rate)
    percentile = (idx + 1) / len(sorted_rates)
    return grade_for_percentile(percentile)


def confidence_tier(units):
    """How much evidence (capacity) backs a RATED zone's own number,
    relative to the shrinkage prior -- not used for estimated zones, which
    have their own neighbor-based tiering (see estimate_confidence)."""
    if units < PRIOR_STRENGTH:
        return "low"
    if units < 3 * PRIOR_STRENGTH:
        return "medium"
    return "high"


def estimate_confidence(neighbors):
    """Confidence in an ESTIMATED grade: about how good the borrowed
    evidence is (count + proximity of real rated neighbors), not about the
    zone's own capacity, since there's no "own" evidence at all here."""
    if not neighbors:
        return "low"
    if len(neighbors) >= 2 and neighbors[0][0] <= 300:
        return "high"
    return "medium"


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


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

    # How far back the underlying alert data actually goes -- shown to
    # users alongside every grade so "0 reports" reads as "0 reports in
    # N months of records," not an unqualified verdict.
    all_dates = [d for d in (incident_date(r) for r in incidents) if d]
    earliest = min(all_dates) if all_dates else now
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=timezone.utc)
    data_coverage_months = round((now - earliest).days / 30.44)

    raw_count_by_zone, weighted_by_zone = {}, {}
    for r in incidents:
        zid = r.get("zone_id")
        if not zid or r.get("within_window") != "True":
            continue
        raw_count_by_zone[zid] = raw_count_by_zone.get(zid, 0) + 1
        w = recency_weight(incident_date(r), now)
        weighted_by_zone[zid] = weighted_by_zone.get(zid, 0.0) + w

    scored_zones = [zid for zid in zone_defs if zid in capacity_by_zone]

    # Campus-wide average rate (over ALL scored zones, rated or not) --
    # this is the true population rate, used as the shrinkage target for
    # rated zones and as the no-nearby-neighbor fallback for estimates.
    total_weighted = sum(weighted_by_zone.get(zid, 0.0) for zid in scored_zones)
    total_units = sum(capacity_by_zone[zid] / CAPACITY_UNIT for zid in scored_zones)
    global_rate = total_weighted / total_units if total_units else 0.0

    rated_zids = [zid for zid in scored_zones if raw_count_by_zone.get(zid, 0) > 0]
    unrated_zids = [zid for zid in scored_zones if zid not in raw_count_by_zone]

    rated = []
    for zid in rated_zids:
        capacity = capacity_by_zone[zid]
        units = capacity / CAPACITY_UNIT
        weighted = weighted_by_zone.get(zid, 0.0)
        smoothed_rate = (weighted + PRIOR_STRENGTH * global_rate) / (units + PRIOR_STRENGTH)
        rated.append({
            "zid": zid, "incidents_n": raw_count_by_zone[zid], "weighted": weighted,
            "capacity": capacity, "units": units, "smoothed_rate": smoothed_rate,
        })

    rated.sort(key=lambda t: t["smoothed_rate"])
    n = len(rated)
    for i, t in enumerate(rated):
        percentile = (i + 1) / n if n else 0
        t["grade"] = grade_for_percentile(percentile)

    rated_sorted_rates = [t["smoothed_rate"] for t in rated]  # already ascending

    out = []
    for t in rated:
        z = zone_defs[t["zid"]]
        out.append({
            "id": t["zid"],
            "name": z["name"],
            "lat": z["lat"],
            "lon": z["lon"],
            "status": "rated",
            "grade": t["grade"],
            "estimated": False,
            "incident_count": t["incidents_n"],
            "weighted_incident_count": round(t["weighted"], 2),
            "rack_capacity": t["capacity"],
            "rack_count": rack_count_by_zone.get(t["zid"], 0),
            "smoothed_incidents_per_40_capacity": round(t["smoothed_rate"], 3),
            "confidence": confidence_tier(t["units"]),
            "has_reports": True,
        })

    for zid in unrated_zids:
        z = zone_defs[zid]
        capacity = capacity_by_zone[zid]

        neighbors = []
        for t in rated:
            rz = zone_defs[t["zid"]]
            d = haversine_m(z["lat"], z["lon"], rz["lat"], rz["lon"])
            if d <= NEIGHBOR_MAX_M:
                neighbors.append((d, t["zid"], t["smoothed_rate"]))
        neighbors.sort(key=lambda t: t[0])
        neighbors = neighbors[:NEIGHBOR_K]

        if neighbors:
            weights = [1 / (d + NEIGHBOR_SOFTEN_M) for d, _, _ in neighbors]
            total_w = sum(weights)
            estimated_rate = sum(w * rate for w, (_, _, rate) in zip(weights, neighbors)) / total_w
            basis = {
                "method": "nearby_zones",
                "neighbor_zone_ids": [zid2 for _, zid2, _ in neighbors],
                "neighbor_count": len(neighbors),
                "nearest_neighbor_m": round(neighbors[0][0]),
            }
        else:
            estimated_rate = global_rate
            basis = {"method": "campus_average", "neighbor_count": 0}

        out.append({
            "id": zid,
            "name": z["name"],
            "lat": z["lat"],
            "lon": z["lon"],
            "status": "no_reports",
            "grade": grade_for_rate(estimated_rate, rated_sorted_rates),
            "estimated": True,
            "estimate_basis": basis,
            "incident_count": 0,
            "weighted_incident_count": 0.0,
            "rack_capacity": capacity,
            "rack_count": rack_count_by_zone.get(zid, 0),
            "estimated_incidents_per_40_capacity": round(estimated_rate, 3),
            "confidence": estimate_confidence(neighbors),
            "has_reports": False,
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
            "data_coverage_months": data_coverage_months,
            "neighbor_max_m": NEIGHBOR_MAX_M,
            "neighbor_k": NEIGHBOR_K,
        },
        "zones": out,
    }
    (SITE_DATA / "zones.json").write_text(json.dumps(payload, indent=2))

    dist = {}
    for z in out:
        key = f"{z['grade']}{'~' if z['estimated'] else ''}"
        dist[key] = dist.get(key, 0) + 1
    fallback_n = sum(1 for z in out if z.get("estimate_basis", {}).get("method") == "campus_average")
    print(f"{len(out)} zones scored -> {SITE_DATA / 'zones.json'}")
    print(f"  rated: {len(rated)}   estimated: {len(unrated_zids)} "
          f"({fallback_n} with no rated neighbor in range, used campus average)")
    print(f"  distribution (grade, ~ = estimated): {dist}")
    print(f"  campus-wide rate: {global_rate:.3f} incidents / 40 capacity")


if __name__ == "__main__":
    main()
