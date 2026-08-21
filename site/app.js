const GRADE_COLOR = {
  "A+": "#1a7f37", "A": "#3fb950", "B": "#9ecb3c",
  "C": "#d4a72c", "D": "#e8590c", "F": "#cf222e",
};
const NO_DATA_COLOR = "#6e7781"; // neutral gray -- deliberately off the red/green scale
const NO_DATA_LABEL = "No Data";

function badgeColor(zone) {
  return zone.status === "rated" ? (GRADE_COLOR[zone.grade] || "#888") : NO_DATA_COLOR;
}

function badgeLabel(zone) {
  return zone.status === "rated" ? zone.grade : NO_DATA_LABEL;
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

function coverageText(months) {
  if (!months && months !== 0) return "unknown";
  return months < 1 ? "<1 month" : `${months} month${months === 1 ? "" : "s"}`;
}

function capitalize(s) {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

// Short fact lines for the nearest-zone card. Deliberately terse -- one
// clause per line, no sentences -- with the "not a guarantee" caveat
// handled once, outside this list, rather than repeated per zone.
function zoneFacts(z, coverageMonths) {
  const theftLine = `${z.incident_count} reported theft${z.incident_count === 1 ? "" : "s"}`;
  const basisLine = z.status === "rated"
    ? "Grade is based on thefts reported at racks in this area"
    : "Not enough reports to grade this area";
  return [
    theftLine,
    `Data coverage: ${coverageText(coverageMonths)}`,
    basisLine,
    `Confidence: ${capitalize(z.confidence)}`,
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

  for (const f of racks.features) {
    const [lon, lat] = f.geometry.coordinates;
    const z = zoneById[f.properties.zone_id];
    const color = z ? badgeColor(z) : "#888";
    L.circleMarker([lat, lon], {
      radius: 4, color, fillColor: color, fillOpacity: 0.8, weight: 1,
    })
      .bindPopup(z
        ? `<strong>${z.name}</strong><br>` +
          (z.status === "rated"
            ? `Area grade: ${z.grade}<br>${z.incident_count} report(s) on file`
            : `Area grade: ${NO_DATA_LABEL}<br>No reports on file`)
        : "Rack (unzoned)")
      .addTo(map);
  }

  return map;
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
  const map = initMap(zones, racks);

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

  btn.addEventListener("click", () => {
    if (!("geolocation" in navigator)) {
      status.textContent = "Your browser doesn't support geolocation.";
      return;
    }
    status.textContent = "Locating…";
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const { latitude, longitude } = pos.coords;
        const { zone, distM } = nearestZone(latitude, longitude, zones);
        if (!zone) {
          status.textContent = "No bike rack areas found.";
          return;
        }
        status.textContent = "";
        resultEl.hidden = false;
        const label = badgeLabel(zone);
        gradeEl.textContent = label;
        gradeEl.style.background = badgeColor(zone);
        gradeEl.style.fontSize = label.length > 2 ? "0.75rem" : "1.5rem";
        nameEl.textContent = zone.name;
        distanceEl.textContent = distanceText(distM);
        factsEl.innerHTML = zoneFacts(zone, coverageMonths)
          .map(f => `<li>${f}</li>`).join("");
        map.setView([zone.lat, zone.lon], 17);
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
