// Tailwind 600/700-tier hues -- modern and friendly but dark enough for
// reliable contrast under the white badge/marker text drawn on top of them.
const GRADE_COLOR = {
  "A+": "#15803d", "A": "#16a34a", "B": "#65a30d",
  "C": "#b45309", "D": "#c2410c", "F": "#b91c1c",
};
const WARNING_GRADES = new Set(["C", "D", "F"]);
const NO_DATA_COLOR = "#64748b"; // neutral slate -- deliberately off the red/green scale
const NO_DATA_LABEL = "No Data";

function badgeColor(zone) {
  return zone.status === "rated" ? (GRADE_COLOR[zone.grade] || "#888") : NO_DATA_COLOR;
}

function badgeLabel(zone) {
  return zone.status === "rated" ? zone.grade : NO_DATA_LABEL;
}

// Map-pin outline with a small bike glyph inside, tinted by grade color.
// Built as inline SVG (no icon library / external asset) so it stays
// self-contained. Cached per-color since there are only 7 possible colors
// across 300+ markers.
const _pinIconCache = new Map();
function bikePinIcon(color) {
  if (_pinIconCache.has(color)) return _pinIconCache.get(color);
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="26" height="34" viewBox="0 0 24 32">
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
  _pinIconCache.set(color, icon);
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

function distanceText(distM) {
  return distM < 1000 ? `${Math.round(distM)} m away` : `${(distM / 1000).toFixed(1)} km away`;
}

function coveragePhrase(months) {
  if (!months && months !== 0) return "the data collected so far";
  return months < 1 ? "the past month" : `the past ${months} months`;
}

// Short fact lines for the nearest-zone card. Deliberately terse -- one
// clause per line, no sentences -- with the "not a guarantee" caveat
// handled once, outside this list, rather than repeated per zone.
function zoneFacts(z, coverageMonths) {
  const theftLine = `${z.incident_count} reported theft${z.incident_count === 1 ? "" : "s"} ` +
    `in ${coveragePhrase(coverageMonths)}`;
  const basisLine = z.status === "rated"
    ? "Grade is based on thefts reported at racks in this zone"
    : "Not enough reports to grade this zone";
  return [theftLine, basisLine];
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
    const marker = L.marker([lat, lon], { icon: bikePinIcon(color) })
      .bindPopup(z
        ? `<strong>${z.name}</strong><br>` +
          (z.status === "rated"
            ? `Zone grade: ${z.grade}<br>${z.incident_count} report(s) on file`
            : `No theft history in this zone`)
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
  const noDataItem = `<span class="legend-item"><i style="background:${NO_DATA_COLOR}"></i>${NO_DATA_LABEL}</span>`;
  el.innerHTML = gradeItems + noDataItem;
}

async function main() {
  const { zones, racks, generatedAt, coverageMonths } = await loadData();
  renderLegend();
  const { map, markersByZone } = initMap(zones, racks);

  const lastUpdatedEl = document.getElementById("last-updated");
  if (generatedAt) {
    lastUpdatedEl.textContent = `Data last refreshed: ${new Date(generatedAt).toLocaleString()}`;
  }

  const btn = document.getElementById("locate-btn");
  const status = document.getElementById("locate-status");
  const resultEl = document.getElementById("nearest-result");
  const gradeEl = document.getElementById("nearest-grade");
  const nameEl = document.getElementById("nearest-name");
  const distanceEl = document.getElementById("nearest-distance");
  const factsEl = document.getElementById("nearest-facts");
  const warningEl = document.getElementById("theft-warning");

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

  // distM is null when there's no reference point to measure from yet
  // (e.g. a rack was clicked directly before the visitor ever located
  // themselves). pan controls whether the map recenters -- skipped for a
  // direct marker click since the visitor already navigated there.
  function showZone(zone, distM, { pan = true } = {}) {
    resultEl.classList.add("visible");
    const label = badgeLabel(zone);
    gradeEl.textContent = label;
    gradeEl.style.background = badgeColor(zone);
    gradeEl.style.fontSize = label.length > 2 ? "0.75rem" : "1.5rem";
    nameEl.textContent = zone.name;
    distanceEl.textContent = distM != null ? distanceText(distM) : "";
    factsEl.innerHTML = zoneFacts(zone, coverageMonths)
      .map(f => `<li>${f}</li>`).join("");

    const showWarning = zone.status === "rated" && WARNING_GRADES.has(zone.grade);
    warningEl.hidden = !showWarning;

    highlightZoneRacks(zone.id, markersByZone);
    if (pan) map.setView([zone.lat, zone.lon], 17);
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
