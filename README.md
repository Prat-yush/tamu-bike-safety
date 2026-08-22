# TAMU Bike Rack Safety

Shows TAMU College Station students which bike rack areas have reported
electric bike/scooter theft history, based on public UPD Crime Alert
bulletins. Static site, no backend. The data is regenerated on a schedule and
committed as static JSON.

## How it works

1. **`pipeline/scrape.py`** : pulls TAMU UPD's Crime Alert index (a public
   JSON feed), fetches every bike/e-scooter theft alert page, and parses
   incidents (case number, dates, free-text location) into `incidents.csv`.
2. **`pipeline/zonejoin.py`** : matches each incident's location text to a
   campus "zone" (see below), preferring geocoding a real street address
   when the alert includes one, falling back to fuzzy name matching.
   Unmatched rows land in `needs_review.csv`.
3. **`pipeline/racks.py`** : pulls the public TAMU Transportation Services
   bike rack inventory (ArcGIS REST API) and assigns each rack to its
   nearest zone.
4. **`pipeline/score.py`** : grades each zone A+ through F. Incidents are
   weighted by recency (1-year half-life, so old reports fade), then each
   zone's rate is shrunk toward the campus-wide average via empirical-Bayes
   smoothing, weighted by how much rack capacity (evidence) that zone has —
   a zone with only a handful of racks doesn't get graded on its own thin
   history alone. Grades are percentile bands over the smoothed rate across
   all zones. Full rationale is in the docstring at the top of the file.
   Writes `site/data/zones.json` + `site/data/racks.geojson`.
5. **`site/`** — a static page. Gets the visitor's location client-side,
   finds the nearest zone by haversine distance against the ~90 zone
   centroids, and shows its grade plus an interactive map.

A GitHub Action (`.github/workflows/update.yml`) re-runs steps 1–4 on a
schedule and redeploys the site whenever the data changes.

## Zones

TAMU's bike rack GIS layers have no building/zone name field, so zones are
generated once by clustering rack points by proximity and reverse-geocoding
each cluster's centroid (`pipeline/gen_zones.py`) into `pipeline/zones.json`.
Labels are a starting point, not gospel, hand-edit `zones.json` for any
zone whose name is wrong or too generic, and grow each zone's `aliases`
list based on what `needs_review.csv` reveals over time. Re-run
`gen_zones.py` only if the physical rack layout changes significantly (it
will regenerate zone IDs, which breaks continuity with existing grades).

## Local development

```bash
cd pipeline
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python scrape.py manifest
./.venv/bin/python scrape.py fetch
./.venv/bin/python scrape.py parse
./.venv/bin/python zonejoin.py
./.venv/bin/python racks.py
./.venv/bin/python score.py

cd ../site
python3 -m http.server 8000
# open http://localhost:8000
```

## Honesty about the data

- Sample sizes per zone are small (dozens of incidents total across ~90
  zones). Grades are directional, not statistically rigorous.
- A grade of A+ with zero reports means **no theft has been reported
  there in the data collected**, not "verified safe." Under-reporting is
  real; lock your bike properly regardless of area grade.
- This feed covers electric bikes/scooters specifically (TAMU UPD's own
  Clery Act classification). Regular pedal-bike theft may be filed
  separately and isn't currently included.
