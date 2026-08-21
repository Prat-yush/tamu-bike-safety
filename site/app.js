// Tailwind 600/700-tier hues -- modern and friendly but dark enough for
// reliable contrast under the white badge/marker text drawn on top of them.
const GRADE_COLOR = {
  "A+": "#15803d", "A": "#16a34a", "B": "#65a30d",
  "C": "#b45309", "D": "#c2410c", "F": "#b91c1c",
};
const WARNING_GRADES = new Set(["C", "D", "F"]);
const NO_DATA_COLOR = "#64748b"; // fallback only -- every zone gets a real (possibly estimated) grade now
const NO_DATA_LABEL = "No Data";

function badgeColor(zone) {
  return GRADE_COLOR[zone.grade] || NO_DATA_COLOR;
}

// "~" flags an estimated grade at a glance, everywhere a grade is shown --
// paired with reduced-opacity pins on the map (see bikePinIcon) and plain
// language in the card/popup text.
function badgeLabel(zone) {
  if (!zone.grade) return NO_DATA_LABEL;
  return zone.estimated ? `~${zone.grade}` : zone.grade;
}

// Map-pin outline with a small bike glyph inside, tinted by grade color.
// Built as inline SVG (no icon library / external asset) so it stays
// self-contained. Estimated zones render at reduced opacity so a "~F"
// doesn't read the same as a real, reported F at a glance. Cached per
// color+estimated pair (14 possible combos across 300+ markers).
const _pinIconCache = new Map();
function bikePinIcon(color, estimated) {
  const cacheKey = `${color}|${estimated}`;
  if (_pinIconCache.has(cacheKey)) return _pinIconCache.get(cacheKey);
  const opacity = estimated ? 0.55 : 1;
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="26" height="34" viewBox="0 0 24 32" opacity="${opacity}">
      <path d="M12 0C6.48 0 2 4.48 2 10c0 7.5 10 22 10 22s10-14.5 10-22C22 4.48 17.52 0 12 0z"
            fill="${color}" stroke="#ffffff" stroke-width="1.3"/>
      <g transform="translate(12,10.5)" fill="none" stroke="#ffffff"
         stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="-4" cy="2.3" r="2.5"/>
        <circle cx="4" cy="2.3" r="2.5"/>
        <path d="M-4 2.3 L-0.8 -2.3 L2 -2.3 M-0.8 -2.3 L1 2.3 M2 -2.3 L4 2.3 M2 -2.3 L3.3 -3.8"/>
      </g>
    </svg>`;
  const icon = L.divIcon({
    className: "bike-pin-icon",
    html: svg,
    iconSize: [26, 34],
    iconAnchor: [13, 33],
    popupAnchor: [0, -30],
  });
  _pinIconCache.set(cacheKey, icon);
  return icon;
}

// Small dot marker (Google-Maps-blue-dot style) for the visitor's own live
// position. No heading/rotation -- browser-supplied heading only comes from
// GPS motion, not a compass, so an arrow just sat still-and-wrong most of
// the time. A plain dot doesn't make that claim.
let _userLocationIcon = null;
function userLocationIcon() {
  if (_userLocationIcon) return _userLocationIcon;
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">
      <circle cx="10" cy="10" r="9" fill="#1a73e8" opacity="0.18"/>
      <circle cx="10" cy="10" r="5.5" fill="#1a73e8" stroke="#ffffff" stroke-width="2"/>
    </svg>`;
  _userLocationIcon = L.divIcon({
    className: "user-location-icon",
    html: svg,
    iconSize: [20, 20],
    iconAnchor: [10, 10],
  });
  return _userLocationIcon;
}

function haversineM(lat1, lon1, lat2, lon2) {
  const r = 6371000;
  const p1 = lat1 * Math.PI / 180, p2 = lat2 * Math.PI / 180;
  const dphi = (lat2 - lat1) * Math.PI / 180;
  const dlmb = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dphi / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dlmb / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(a));
}

function nearestZone(lat, lon, zones) {
  let best = null, bestD = Infinity;
  for (const z of zones) {
    const d = haversineM(lat, lon, z.lat, z.lon);
    if (d < bestD) { best = z; bestD = d; }
  }
  return { zone: best, distM: bestD };
}

// Minimal inline icons (no icon font/library) for the fact list -- one per
// line type, sized to sit inline with 0.9rem text.
const ICONS = {
  alert: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  info: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
  pin: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 5.5-8 12-8 12s-8-6.5-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>',
};

// Counts an element's text up from 0 to target over ~900ms. Purely a
// hero-stat flourish -- if it's interrupted (e.g. the tab was backgrounded
// mid-count) the worst case is it just lands on the final number a frame
// late, no real state to get wrong.
function animateCount(el, target, duration = 900) {
  if (!el || !target) { if (el) el.textContent = String(target || 0); return; }
  const start = performance.now();
  function tick(now) {
    const p = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - p, 3); // ease-out cubic
    el.textContent = Math.round(eased * target).toLocaleString();
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function distanceText(distM) {
  return distM < 1000 ? `${Math.round(distM)} m away` : `${(distM / 1000).toFixed(1)} km away`;
}

function coveragePhrase(months) {
  if (!months && months !== 0) return "the data collected so far";
  return months < 1 ? "the past month" : `the past ${months} months`;
}

function estimateBasisPhrase(z) {
  const b = z.estimate_basis || {};
  if (b.method === "nearby_zones") {
    return `Estimated from ${b.neighbor_count} nearby zone${b.neighbor_count === 1 ? "" : "s"} ` +
      `(closest ${distanceText(b.nearest_neighbor_m)})`;
  }
  return "Estimated from the campus-wide average -- no nearby zones have data either";
}

// Short fact lines for the nearest-zone card. Deliberately terse -- one
// clause per line, no sentences -- with the "not a guarantee" caveat
// handled once, outside this list, rather than repeated per zone. Each
// line carries an icon key naming which small icon (see ICONS) precedes it.
function zoneFacts(z, coverageMonths) {
  const theftLine = {
    icon: "alert",
    text: `${z.incident_count} reported theft${z.incident_count === 1 ? "" : "s"} ` +
      `in ${coveragePhrase(coverageMonths)}`,
  };
  if (z.status === "rated") {
    return [theftLine, { icon: "info", text: "Grade is based on thefts reported at racks in this zone" }];
  }
  return [
    theftLine,
    { icon: "info", text: "No reports on file for this zone. The grade is estimated based on nearby confirmed reports." },
    { icon: "pin", text: estimateBasisPhrase(z) },
  ];
}

async function loadData() {
  const [zonesRes, racksRes] = await Promise.all([
    fetch("data/zones.json"),
    fetch("data/racks.geojson"),
  ]);
  const zonesPayload = await zonesRes.json();
  return {
    zones: zonesPayload.zones,
    generatedAt: zonesPayload.generated_at,
    coverageMonths: zonesPayload.methodology && zonesPayload.methodology.data_coverage_months,
    racks: await racksRes.json(),
  };
}

function initMap(zones, racks) {
  const center = zones.length
    ? [zones.reduce((s, z) => s + z.lat, 0) / zones.length,
       zones.reduce((s, z) => s + z.lon, 0) / zones.length]
    : [30.6188, -96.3365];

  const map = L.map("map").setView(center, 15);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }).addTo(map);

  const zoneById = Object.fromEntries(zones.map(z => [z.id, z]));
  const markersByZone = new Map();

  for (const f of racks.features) {
    const [lon, lat] = f.geometry.coordinates;
    const z = zoneById[f.properties.zone_id];
    const color = z ? badgeColor(z) : "#888";
    // z.status is computed fresh by score.py from that run's incident data,
    // not hardcoded -- a zone flips from "no_reports" to "rated" on its own
    // the moment a real incident is scraped and matched to it, no code
    // change involved.
    const marker = L.marker([lat, lon], { icon: bikePinIcon(color, z && z.estimated) })
      .bindPopup(z
        ? `<strong>${z.name}</strong><br>` +
          (z.status === "rated"
            ? `Zone grade: ${z.grade}<br>${z.incident_count} report(s) on file`
            : `Zone grade: ~${z.grade} (estimated)<br>No reports on file for this zone`)
        : "Rack (unzoned)")
      .addTo(map);

    if (z) {
      marker._zone = z; // read by main()'s click handler to update the card
      if (!markersByZone.has(z.id)) markersByZone.set(z.id, []);
      markersByZone.get(z.id).push(marker);
    }
  }

  return { map, markersByZone };
}

// Temporarily pulses the pins belonging to one zone so it's obvious which
// racks on the map a "nearest zone" result is talking about.
let _glowTimer = null;
let _glowingMarkers = [];
function highlightZoneRacks(zoneId, markersByZone, durationMs = 4000) {
  for (const m of _glowingMarkers) {
    const el = m.getElement();
    if (el) el.classList.remove("pin-glow");
  }
  if (_glowTimer) clearTimeout(_glowTimer);

  const markers = markersByZone.get(zoneId) || [];
  for (const m of markers) {
    const el = m.getElement();
    if (el) el.classList.add("pin-glow");
  }
  _glowingMarkers = markers;
  _glowTimer = setTimeout(() => {
    for (const m of markers) {
      const el = m.getElement();
      if (el) el.classList.remove("pin-glow");
    }
    _glowingMarkers = [];
  }, durationMs);
}


function renderLegend() {
  const el = document.getElementById("legend");
  const gradeItems = Object.entries(GRADE_COLOR)
    .map(([g, c]) => `<span class="legend-item"><i style="background:${c}"></i>${g}</span>`)
    .join("");
  // Every zone gets a real or estimated grade now -- there's no color left
  // that means "no data," so the key just needs to explain the one visual
  // distinction that still exists: faded pin = estimated, not reported.
  const estimatedItem = `<span class="legend-item">` +
    `<i style="background:${NO_DATA_COLOR};opacity:0.55"></i>~ Estimated</span>`;
  el.innerHTML = gradeItems + estimatedItem;
}

async function main() {
  const { zones, racks, generatedAt, coverageMonths } = await loadData();
  renderLegend();
  const { map, markersByZone } = initMap(zones, racks);

  const lastUpdatedEl = document.getElementById("last-updated");
  if (generatedAt) {
    lastUpdatedEl.textContent = `Data last refreshed: ${new Date(generatedAt).toLocaleString()}`;
  }

  const totalReports = zones.reduce((sum, z) => sum + (z.incident_count || 0), 0);
  animateCount(document.getElementById("stat-zones"), zones.length);
  animateCount(document.getElementById("stat-reports"), totalReports);
  animateCount(document.getElementById("stat-months"), coverageMonths || 0);

  const btn = document.getElementById("locate-btn");
  const status = document.getElementById("locate-status");
  const resultEl = document.getElementById("nearest-result");
  const gradeEl = document.getElementById("nearest-grade");
  const nameEl = document.getElementById("nearest-name");
  const distanceEl = document.getElementById("nearest-distance");
  const factsEl = document.getElementById("nearest-facts");
  const warningEl = document.getElementById("theft-warning");
  const warningTextEl = document.getElementById("theft-warning-text");

  let lastCoords = null;
  let userMarker = null;
  let watchId = null;

  function updateUserMarker(lat, lon) {
    lastCoords = { lat, lon };
    if (userMarker) {
      userMarker.setLatLng([lat, lon]);
    } else {
      userMarker = L.marker([lat, lon], {
        icon: userLocationIcon(), zIndexOffset: 1000, interactive: false,
      }).addTo(map);
    }
  }

  // Keeps the dot moving after the first fix. Started once, from the
  // locate button, since geolocation permission is already granted by
  // that point -- no separate prompt.
  function startWatchingLocation() {
    if (watchId != null || !("geolocation" in navigator)) return;
    watchId = navigator.geolocation.watchPosition(
      (pos) => updateUserMarker(pos.coords.latitude, pos.coords.longitude),
      () => { /* keep the last-known dot position on a transient watch error */ },
      { enableHighAccuracy: true, maximumAge: 5000 }
    );
  }

  function applyZoneContent(zone, distM) {
    const label = badgeLabel(zone);
    gradeEl.textContent = label;
    gradeEl.style.background = badgeColor(zone);
    gradeEl.style.fontSize = label.length > 2 ? "0.75rem" : "1.5rem";
    nameEl.textContent = zone.name;
    distanceEl.textContent = distM != null ? distanceText(distM) : "";
    factsEl.innerHTML = zoneFacts(zone, coverageMonths)
      .map(f => `<li>${ICONS[f.icon] || ""}<span>${f.text}</span></li>`).join("");

    const showWarning = WARNING_GRADES.has(zone.grade);
    warningEl.hidden = !showWarning;
    if (showWarning) {
      warningTextEl.textContent = zone.estimated
        ? `This zone's grade is estimated at ${zone.grade} based on nearby confirmed reports. ` +
          `No thefts have been reported here specifically, but be extra careful!`
        : `This zone has had a history of bike thefts. Please be extra careful!`;
    }
  }

  // distM is null when there's no reference point to measure from yet
  // (e.g. a rack was clicked directly before the visitor ever located
  // themselves). pan controls whether the map recenters -- skipped for a
  // direct marker click since the visitor already navigated there.
  function showZone(zone, distM, { pan = true } = {}) {
    const alreadyVisible = resultEl.classList.contains("visible");
    highlightZoneRacks(zone.id, markersByZone);
    if (pan) map.setView([zone.lat, zone.lon], 17);

    if (!alreadyVisible) {
      // First reveal: no old content to fade out, just fill it in and let
      // the existing grow-in (max-height/opacity) transition handle the
      // rest.
      applyZoneContent(zone, distM);
      resultEl.classList.add("visible");
      return;
    }

    // Switching to a different zone while the card is already open: fade
    // the old content out, swap it, fade back in -- reads as a deliberate
    // transition instead of a jarring instant text swap.
    resultEl.classList.add("content-fading");
    setTimeout(() => {
      applyZoneContent(zone, distM);
      resultEl.classList.remove("content-fading");
    }, 150);
  }

  for (const markers of markersByZone.values()) {
    for (const marker of markers) {
      marker.on("click", () => {
        const zone = marker._zone;
        const distM = lastCoords
          ? haversineM(lastCoords.lat, lastCoords.lon, zone.lat, zone.lon)
          : null;
        showZone(zone, distM, { pan: false });
      });
    }
  }

  function locateNearest(lat, lon) {
    const { zone, distM } = nearestZone(lat, lon, zones);
    if (!zone) {
      status.textContent = "No bike rack zones found.";
      return;
    }
    status.textContent = "";
    showZone(zone, distM);
  }

  btn.addEventListener("click", () => {
    if (!("geolocation" in navigator)) {
      status.textContent = "Your browser doesn't support geolocation.";
      return;
    }

    // Already tracking a live fix (from watchPosition, started below after
    // the first successful locate) -- reuse it instead of firing a brand
    // new getCurrentPosition. Some browsers only grant geolocation as a
    // one-time permission; a second fresh request can come back
    // PERMISSION_DENIED even while the original watch is still happily
    // delivering updates, which read as a confusing "access denied" bug
    // on the second click.
    if (lastCoords) {
      locateNearest(lastCoords.lat, lastCoords.lon);
      return;
    }

    status.textContent = "Locating…";
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const { latitude, longitude } = pos.coords;
        updateUserMarker(latitude, longitude);
        locateNearest(latitude, longitude);
        startWatchingLocation();
      },
      (err) => {
        status.textContent = `Couldn't get your location (${err.message}). ` +
          `You can still browse the map below.`;
      },
      { enableHighAccuracy: true, timeout: 10000 }
    );
  });
}

main();
