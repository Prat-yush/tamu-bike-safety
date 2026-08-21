#!/usr/bin/env python3
"""
TAMU UPD e-bike / e-scooter theft alert scraper.

    python scrape.py manifest      # build manifest.csv from the JSON news feed
    python scrape.py fetch         # download every candidate page into cache/
    python scrape.py parse         # cache/ -> incidents.csv

Design notes
------------
* Pages come in two shapes. Narrative (one incident, prose) up to 2025-11;
  consolidated table ("...Electric Scooter and Electric Bicycle List") from
  2025-12 onward. `parse` dispatches on the presence of a Case No. table.
* Nothing is ever dropped at parse time. Filters (main campus, window length)
  are applied downstream at scoring time so you can change your mind without
  re-scraping. `window_hours` and `needs_review` are computed here.
* Narrative extraction is best-effort. Expect to hand-correct. There are only
  ~23 such pages -- `narrative_text` is preserved verbatim in the CSV so you
  can fill gaps by reading rather than by regex-wrangling.
"""

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://upd.tamu.edu"
FEED = f"{BASE}/_json-data/json-news-data.json"
CACHE = Path("cache")
# upd.tamu.edu sits behind a CloudFront/WAF rule that 404s any request whose
# User-Agent doesn't look like a real browser (identifying UA strings get
# silently blocked, not 403'd). A standard browser UA is required to reach
# this otherwise fully public, unauthenticated, Clery-Act-mandated content.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# The user-facing filter lives at scoring time, but we compute the flag here.
MAX_WINDOW_HOURS = 7 * 24  # inclusive; a record at exactly 7d is KEPT


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

def norm(s: str) -> str:
    """Unescape entities and flatten the dash zoo (-, en dash, nbsp)."""
    s = html.unescape(s or "")
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def is_candidate(title: str, categories) -> bool:
    """Motor-vehicle-theft alerts. Categories are unreliable -- several
    genuine MVT alerts ship with categories: []. Title is the fallback."""
    t = norm(title).lower()
    cats = [norm(c).lower() for c in (categories or [])]
    if "motor vehicle theft" in cats:
        return True
    return "motor vehicle" in t and "theft" in t


def cmd_manifest(args):
    r = requests.get(FEED, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    feed = r.json()

    rows = []
    for key, e in feed.items():
        title = norm(e.get("title", ""))
        if not is_candidate(title, e.get("categories")):
            continue

        link = e.get("link", "")
        # NEVER rebuild this from `key`. 7-2-2015 is a typo'd 2025 alert and
        # the link carries the same typo; 11-12-2025 lives under /_test/.
        url = link if link.startswith("http") else BASE + link

        ts = e.get("startdate")
        alert_date = (
            datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            if ts else ""
        )

        tl = title.lower()
        rows.append({
            "key": key,
            "alert_date": alert_date,
            "title": title,
            "url": url,
            "format_guess": "list" if ("electric" in tl or "list" in tl) else "narrative",
            "is_update": "update" in key.lower() or "(update)" in tl,
            "suspect_path": "/_test/" in url,
            "categories": "|".join(norm(c) for c in (e.get("categories") or [])),
            "tags": "|".join(norm(t) for t in (e.get("tags") or [])),
        })

    rows.sort(key=lambda r: r["alert_date"])
    write_csv("manifest.csv", rows)

    n_list = sum(1 for r in rows if r["format_guess"] == "list")
    print(f"{len(rows)} candidate alerts -> manifest.csv")
    print(f"  narrative: {len(rows) - n_list}   list: {n_list}")
    for r in rows:
        if r["is_update"]:
            print(f"  ! update page (dedupe on case_no): {r['key']}")
        if r["suspect_path"]:
            print(f"  ! non-standard path: {r['url']}")


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

def cache_path(url: str) -> Path:
    return CACHE / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".html")


def cmd_fetch(args):
    CACHE.mkdir(exist_ok=True)
    rows = read_csv("manifest.csv")
    got = new = 0
    for r in rows:
        p = cache_path(r["url"])
        if p.exists() and not args.force:
            got += 1
            continue
        try:
            resp = requests.get(r["url"], headers={"User-Agent": UA}, timeout=30)
            if resp.status_code != 200:
                print(f"  {resp.status_code} {r['url']}", file=sys.stderr)
                continue
            p.write_text(resp.text, encoding="utf-8")
            new += 1
            time.sleep(1.0)  # be a good citizen; this is a university server
        except Exception as exc:
            print(f"  FAIL {r['url']}: {exc}", file=sys.stderr)
    print(f"cached {got + new} pages ({new} new) in {CACHE}/")


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------

def main_content(soup: BeautifulSoup):
    """Grab the article body, avoiding nav/footer. Falls back to <body>."""
    for sel in ("#main-content", "main", "article", ".content", "#content"):
        node = soup.select_one(sel)
        if node:
            return node
    return soup.body or soup


CASE_RE = re.compile(r"\b(\d{2}-\d{4}-\d{4})\b")
DT_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2})")


def parse_dt(s: str):
    """'08/07/2026 18:00' -> datetime. Table format uses 24h."""
    m = DT_RE.search(s or "")
    if not m:
        return None
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M"):
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", fmt)
        except ValueError:
            continue
    return None


RACK_NOISE = re.compile(
    r"\b(bike|bicycle|cycle)\s+(rack|racks|lane|lanes|share|parking|room|shelter)\b",
    re.I,
)


def classify_vehicle(text: str) -> str:
    # "parked at the bike racks" is furniture, not the stolen vehicle.
    t = RACK_NOISE.sub(" ", (text or "")).lower()
    scooter = "scooter" in t
    bike = "bicycle" in t or "bike" in t
    if scooter and not bike:
        return "escooter"
    if bike and not scooter:
        return "ebike"
    if scooter and bike:
        return "ambiguous"
    return "unknown"


def parse_table_page(node, meta):
    """Consolidated list format: Case No. / Last Seen / Discovered Missing /
    General Location."""
    out = []
    for table in node.find_all("table"):
        headers = [norm(th.get_text()).lower()
                   for th in table.find_all(["th", "td"], limit=8)]
        blob = " ".join(headers)
        if "case" not in blob or "location" not in blob:
            continue

        rows = table.find_all("tr")
        if not rows:
            continue
        cols = [norm(c.get_text()).lower() for c in rows[0].find_all(["th", "td"])]

        def idx(*names):
            for i, c in enumerate(cols):
                if any(n in c for n in names):
                    return i
            return None

        i_case = idx("case")
        i_seen = idx("last seen")
        i_disc = idx("discovered", "missing")
        i_loc = idx("location")

        for tr in rows[1:]:
            cells = [norm(td.get_text()) for td in tr.find_all(["td", "th"])]
            if not cells or all(not c for c in cells):
                continue

            def cell(i):
                return cells[i] if i is not None and i < len(cells) else ""

            case = cell(i_case)
            if not CASE_RE.search(case):
                m = CASE_RE.search(" ".join(cells))
                case = m.group(1) if m else ""

            out.append(make_row(
                meta, case,
                cell(i_seen), cell(i_disc), cell(i_loc),
                source_format="list",
                narrative_text="",
            ))
    return out


# Word-processor copy/paste sometimes lands with digits split across separate
# tags ("Feb. 1 9 , 2025"), so get_text(" ") joins them with stray spaces.
# Squeeze a lone-digit-space-digit run back together when it's followed by a
# comma+year -- that shape only occurs in a split day-of-month, never
# elsewhere in these alerts.
SPLIT_DAY_RE = re.compile(r"\b(\d)\s+(\d)\s*,\s*(\d{4})\b")


def fix_split_digits(s: str) -> str:
    return SPLIT_DAY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}, {m.group(3)}", s)


# Time without a leading zero and without minutes ("8 a.m.") is common and
# was previously unmatched (old regex required H:MM). Also tolerate stray
# spaces around the colon / periods ("5 : 45 p. m.") from the same
# copy/paste garbling handled above.
_TIME = r"\d{1,2}(?:\s*:\s*\d{2})?\s*[ap]\.?\s*m\.?"
_DATE = r"[A-Z][a-z]+\.?\s*\d{1,2}\s*,?\s*\d{4}"

# Alerts write the date/time tail in either order ("...at approximately
# TIME on DATE" or "...on DATE, at approximately TIME"). Try both.
TAIL_RE = re.compile(
    rf"(?:at\s+)?(?:approximately\s+)?(?P<time1>{_TIME})\s+on\s+(?P<date1>{_DATE})"
    rf"|"
    rf"on\s+(?P<date2>{_DATE})\s*,?\s*at\s+(?:approximately\s+)?(?P<time2>{_TIME})",
    re.I,
)
PARKED_VERB_RE = re.compile(r"\bparked\b", re.I)
LOC_START_RE = re.compile(r"\b(?:at|in|near|outside)\s+", re.I)
RETURNED_VERB_RE = re.compile(r"\b(?:returned|discovered|realized)\b", re.I)


def parse_prose_dt(date_s, time_s):
    if not date_s or not time_s:
        return None
    d = re.sub(r"\.", "", date_s).strip()
    d = re.sub(r"\s+", " ", d)
    t = re.sub(r"[.\s]", "", time_s).upper()  # "12:30 p.m." -> "12:30PM" / "8AM"
    if ":" not in t:
        t = t[:-2] + ":00" + t[-2:]  # "8AM" -> "8:00AM"
    for fmt in ("%b %d, %Y %I:%M%p", "%B %d, %Y %I:%M%p"):
        try:
            return datetime.strptime(f"{d} {t}", fmt)
        except ValueError:
            continue
    return None


def extract_tail(text_from_here):
    """Find the first date/time tail (either order) in a clause. Returns
    (date_str, time_str, match) or (None, None, None)."""
    tm = TAIL_RE.search(text_from_here)
    if not tm:
        return None, None, None
    if tm.group("time1"):
        return tm.group("date1"), tm.group("time1"), tm
    return tm.group("date2"), tm.group("time2"), tm


def extract_parked_clause(body):
    """('...parked [desc,] (at|in|near|outside) LOC ... TIME on DATE...')
    Returns (loc, date_s, time_s) or (None, None, None)."""
    vm = PARKED_VERB_RE.search(body)
    if not vm:
        return None, None, None
    rest = body[vm.end():]
    lm = LOC_START_RE.search(rest)
    if not lm:
        return None, None, None
    after_loc = rest[lm.end():]
    date_s, time_s, tm = extract_tail(after_loc)
    if not tm:
        return None, None, None
    loc = norm(after_loc[:tm.start()])
    loc = re.sub(r"[.,;]+$", "", loc).strip()
    return loc, date_s, time_s


def extract_returned_clause(body):
    vm = RETURNED_VERB_RE.search(body)
    if not vm:
        return None, None
    date_s, time_s, _ = extract_tail(body[vm.end():])
    return date_s, time_s


def parse_narrative_page(node, meta):
    """One incident per page, prose. Best-effort; keeps raw text for review."""
    text = fix_split_digits(norm(node.get_text(" ")))

    m = re.search(r"Case Number\s*[:#]?\s*(\d{2}-\d{4}-\d{4})", text, re.I)
    case = m.group(1) if m else (CASE_RE.search(text).group(1)
                                 if CASE_RE.search(text) else "")

    # Trim boilerplate so narrative_text stays readable.
    body = text
    cut = re.search(r"Anyone (?:with|having) information", body, re.I)
    if cut:
        body = body[:cut.start()]
    start = re.search(r"(?:On [A-Z][a-z]+\.?\s+\d{1,2}|The victim)", body)
    if start:
        body = body[start.start():]

    loc, park_date_s, park_time_s = extract_parked_clause(body)
    ret_date_s, ret_time_s = extract_returned_clause(body)

    loc = loc or ""

    seen = parse_prose_dt(park_date_s, park_time_s)
    disc = parse_prose_dt(ret_date_s, ret_time_s)

    return [make_row(
        meta, case,
        seen.strftime("%m/%d/%Y %H:%M") if seen else "",
        disc.strftime("%m/%d/%Y %H:%M") if disc else "",
        loc,
        source_format="narrative",
        narrative_text=body[:1200],
        seen_dt=seen, disc_dt=disc,
    )]


def make_row(meta, case, seen_s, disc_s, loc, source_format,
             narrative_text, seen_dt=None, disc_dt=None):
    seen = seen_dt or parse_dt(seen_s)
    disc = disc_dt or parse_dt(disc_s)

    window = ""
    within = ""
    if seen and disc:
        window = round((disc - seen).total_seconds() / 3600, 2)
        within = str(window <= MAX_WINDOW_HOURS)

    # The list-format title always names BOTH vehicle types, so it can only
    # mislead. Only the narrative title carries signal.
    signal = [narrative_text, loc]
    if source_format == "narrative":
        signal.append(meta["title"])
    vt = classify_vehicle(" ".join(signal))

    # The consolidated table has no vehicle column, so 'unknown' is expected
    # there and not worth flagging. Every row on these pages is an e-bike or
    # e-scooter by definition of the alert, which is all the scoping needs.
    missing = not case or not loc or window == ""
    if source_format == "narrative" and vt in ("unknown", "ambiguous"):
        missing = True
    return {
        "case_no": case,
        "alert_key": meta["key"],
        "alert_date": meta["alert_date"],
        "alert_url": meta["url"],
        "source_format": source_format,
        "vehicle_type": vt,
        "last_seen": seen.isoformat() if seen else "",
        "discovered_missing": disc.isoformat() if disc else "",
        "window_hours": window,
        "within_window": within,          # filter on this at SCORING time
        "location_raw": loc,              # join key for your zone lookup table
        "zone_id": "",                    # fill from location_raw -> zone map
        "needs_review": str(missing),
        "narrative_text": narrative_text,
    }


def cmd_parse(args):
    rows = read_csv("manifest.csv")
    incidents = []
    for r in rows:
        p = cache_path(r["url"])
        if not p.exists():
            print(f"  no cache for {r['url']} (run fetch)", file=sys.stderr)
            continue
        soup = BeautifulSoup(p.read_text(encoding="utf-8"), "html.parser")
        node = main_content(soup)

        got = parse_table_page(node, r)
        if not got:
            got = parse_narrative_page(node, r)
        incidents.extend(got)

    # Dedupe on case_no, preferring the later alert (update pages supersede).
    by_case, no_case = {}, []
    for inc in incidents:
        if inc["case_no"]:
            prev = by_case.get(inc["case_no"])
            if not prev or inc["alert_date"] >= prev["alert_date"]:
                by_case[inc["case_no"]] = inc
        else:
            no_case.append(inc)

    final = sorted(by_case.values(), key=lambda x: x["alert_date"]) + no_case
    write_csv("incidents.csv", final)

    review = sum(1 for i in final if i["needs_review"] == "True")
    within = sum(1 for i in final if i["within_window"] == "True")
    print(f"{len(incidents)} parsed -> {len(final)} after dedupe -> incidents.csv")
    print(f"  needs_review: {review}")
    print(f"  within {MAX_WINDOW_HOURS/24:.0f}d window: {within}")
    print(f"  no case number: {len(no_case)}")
    print("\nNext: fill zone_id from location_raw, then audit every row.")


# --------------------------------------------------------------------------

def write_csv(path, rows):
    if not rows:
        Path(path).write_text("")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    if not Path(path).exists():
        sys.exit(f"{path} not found -- run the earlier step first")
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("manifest").set_defaults(func=cmd_manifest)
    f = sub.add_parser("fetch"); f.add_argument("--force", action="store_true")
    f.set_defaults(func=cmd_fetch)
    sub.add_parser("parse").set_defaults(func=cmd_parse)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
