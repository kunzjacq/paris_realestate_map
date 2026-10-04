"use strict";

// Valeurs de référence (µg/m³) : lignes directrices OMS 2021 et valeurs limites UE à partir de 2030
const AIR = [
  { key: "no2", label: "NO₂", oms: 10, ue2030: 20 },
  { key: "pm25", label: "PM2.5", oms: 5, ue2030: 10 },
  { key: "pm10", label: "PM10", oms: 15, ue2030: 20 },
];
const BP_NOISE_LABELS = { 0: "n.d.", 1: "préservé", 2: "altéré", 3: "très dégradé" };
const COLORS = {
  zone: [26, 127, 90],
  zoneEdge: [12, 80, 55],
  walk: { 5: "#08519c", 10: "#4292c6", 15: "#9ecae1", 20: "#deebf7" },
  bp_noise: { 1: "#a6d96a", 2: "#fdd049", 3: "#d7301f" },
  // couleurs de la légende Bruitparif (40 = moins de 45 dB)
  lden_route: { 40: "#4bc700", 45: "#53fd00", 50: "#b7fd72", 55: "#fcfd00", 60: "#fda900", 65: "#fd0000", 70: "#d300fc", 75: "#950064" },
  lden_fer: { 40: "#4bc700", 45: "#53fd00", 50: "#b7fd72", 55: "#fcfd00", 60: "#fda900", 65: "#fd0000", 70: "#d300fc", 75: "#950064" },
  ramp: ["#fcfdbf", "#fec287", "#fb8861", "#e65164", "#b73779", "#822681", "#51127c"],
};
const EMPTY_PNG = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
const STORAGE_KEY = "immo_map.state.v3";
const NETWORK_COLORS = { rer: "#c2185b", transilien: "#1565c0", metro: "#e0a100" };
const NO_STATION = 65535;  // indice de gare d'une cellule sans gare atteignable
// couleur d'une gare : RER, sinon Transilien, sinon métro
const stationColor = (nets) => NETWORK_COLORS[["rer", "transilien", "metro"].find((n) => nets.includes(n)) || "rer"];
const NEAR_STATIONS = 3;  // gares affichées : les plus proches de la souris
const DATA_FORMAT = 10;  // doit suivre DATA_FORMAT de scripts/pipeline.py
const MODE_LABELS = { walk: "À pied", bike: "À vélo" };
const UNREACHED = 65535;  // temps (s) d'une cellule hors d'atteinte
// classe Lden (borne basse ; 40 = moins de 45 dB ; 0 = non renseigné) -> libellé
const ldenTxt = (x) => !x ? "n.d." : x === 40 ? "< 45 dB" : x === 75 ? "≥ 75 dB" : `${x}–${x + 5} dB`;
// seuil en secondes cohérent avec l'affichage arrondi à la minute : 10 min => jusqu'à 10 min 29 s
const limitSec = (min) => min * 60 + 29;
const fmtMin = (sec) => sec === UNREACHED ? "plus d'1 h" : `${Math.max(1, Math.round(sec / 60))} min`;
const ICON_TARGET = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">' +
  '<circle cx="8" cy="8" r="5"/><path d="M8 1v3M8 12v3M1 8h3M12 8h3"/></svg>';

const state = {
  inactive: [],        // codes des communes décochées
  walk: 10,
  walkFilter: true,    // false : le temps de trajet n'est pas un critère
  travelMode: "walk",  // "walk" ou "bike"
  networks: { rer: true, transilien: true, metro: true },  // réseaux pris en compte pour le temps de trajet
  air: {},             // seuils en µg/m³ ; absent = pas de filtre
  bpNoise: 3,
  ldenRoute: 999,
  ldenFer: 999,
  context: "none",
  showZone: true,
  showIso: true,
};

let index = null;            // data/index.json
const communes = new Map();  // code -> { meta, L, zone, context, outline, built }
let map, isoLayer, stationsLayer, accesLayer, nearLayer;
let accesByZdc = new Map();  // zdc -> marqueurs d'accès
let airRange = {};
let serverMode = false, lastVersion = null, firstFit = true;
const canvas = document.createElement("canvas");

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1) => v.toLocaleString("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d });
const getJSON = async (url, opts) => {
  const r = await fetch(url, { cache: "no-cache", ...opts });
  if (!r.ok) throw new Error(`${url} : HTTP ${r.status}`);
  return r.json();
};
const isActive = (code) => !state.inactive.includes(code);

function saveState() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (e) { /* stockage indisponible */ }
}
function loadState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    Object.assign(state, saved, { networks: { ...state.networks, ...(saved.networks || {}) } });
  } catch (e) { /* idem */ }
}

// ------------------------------------------------------------------ données
//
// Au démarrage, seuls le résumé (meta.json) et le contour de chaque commune sont chargés ;
// les couches détaillées (temps, air, bruit) le sont à la demande, quand la commune devient visible.

async function loadCommuneMeta(code, built) {
  const base = `data/communes/${code}/`;
  const [meta, outlineGeo] = await Promise.all([getJSON(base + "meta.json"), getJSON(base + "commune.geojson")]);
  const old = communes.get(code);
  if (old) removeCommuneLayers(old);
  communes.set(code, {
    code, meta, built, loaded: false, loading: null,
    outlineRings: outlineGeo.features.flatMap((f) => geoRings(f.geometry)),
    outline: L.geoJSON(outlineGeo, { style: { color: "#1f2328", weight: 2, fill: false }, interactive: false }).addTo(map),
    label: communeLabel(meta.nom, outlineGeo).addTo(map),
  });
}

async function loadCommuneData(c) {
  const base = `data/communes/${c.code}/`;
  const layers = {};
  await Promise.all(Object.entries(c.meta.layers).map(async ([name, info]) => {
    const buf = await (await fetch(`${base}${name}.bin`, { cache: "no-cache" })).arrayBuffer();
    layers[name] = info.dtype === "uint16" ? new Uint16Array(buf) : new Uint8Array(buf);
  }));
  if (communes.get(c.code) !== c) return;  // commune retirée ou reconstruite entre-temps
  Object.assign(c, {
    L: layers, loaded: true,
    context: L.imageOverlay(EMPTY_PNG, c.meta.bounds, { opacity: 0.7, interactive: false }).addTo(map),
    zone: L.layerGroup().addTo(map),      // zone retenue et voile hors zone, en contours lissés
    iso: L.layerGroup().addTo(isoLayer),  // contour de la zone atteignable
  });
  indexCells(c);
  combineWalk(c);
}

// charge les communes visibles (avec une marge) pas encore chargées, puis les dessine
const LOAD_MARGIN = 0.15;  // marge autour de la vue (fraction) pour anticiper les petits déplacements
function ensureVisibleLoaded() {
  const view = map.getBounds().pad(LOAD_MARGIN);
  const todo = [...communes.values()].filter((c) => !c.loaded && !c.loading && view.intersects(boundsOf(c)));
  if (!todo.length) return;
  showLoading(todo.length);
  todo.forEach((c) => {
    c.loading = loadCommuneData(c).catch((e) => console.error(e)).finally(() => {
      c.loading = null;
      if (![...communes.values()].some((x) => x.loading)) showLoading(0);
      if (!c.loaded) return;
      c.lastResult = computeCommune(c, thresholds());
      c.zoneDirty = true;
      drawVisibleZones();
      drawContext(c);
      drawIso();
    });
  });
}

function showLoading(n) {
  const el = $("map-loading");
  if (el) { el.hidden = !n; el.textContent = n ? `Chargement de ${n} commune(s)…` : ""; }
}

// ------------------------------------------------------------------ noms des communes
// Les noms du fond de carte sont sous le voile et les contours de zone : on dessine les nôtres au-dessus.

const LABEL_MIN_ZOOM = 12;  // en dessous, noms masqués (vue d'ensemble trop chargée)

function communeLabel(nom, geo) {
  return L.marker(labelPoint(geo), {
    pane: "labels", interactive: false, keyboard: false,
    icon: L.divIcon({ className: "commune-label", html: `<span>${nom}</span>`, iconSize: null }),
  });
}

// point « le plus intérieur » de la commune (loin de ses limites), recherché sur une grille :
// le centre de gravité peut tomber hors d'une commune de forme irrégulière
function labelPoint(geo) {
  const polys = geo.features.flatMap((f) => f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates);
  const outer = polys.reduce((a, b) => (b[0].length > a[0].length ? b : a));  // partie principale
  const k = Math.cos((outer[0][0][1] * Math.PI) / 180);  // degrés de longitude -> distance
  const xs = outer[0].map((p) => p[0]), ys = outer[0].map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const inside = (x, y) => outer.reduce((inn, ring, r) => {
    let c = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i], [xj, yj] = ring[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
    }
    return r === 0 ? c : inn && !c;  // dans l'enveloppe et hors des trous
  }, false);
  const distToEdge = (x, y) => {
    let d = Infinity;
    for (const ring of outer) for (let i = 1; i < ring.length; i++) {
      const [ax, ay] = ring[i - 1], [bx, by] = ring[i];
      const dx = (bx - ax) * k, dy = by - ay, px = (x - ax) * k, py = y - ay;
      const t = Math.max(0, Math.min(1, (px * dx + py * dy) / (dx * dx + dy * dy || 1)));
      d = Math.min(d, Math.hypot(px - t * dx, py - t * dy));
    }
    return d;
  };
  let best = [(y0 + y1) / 2, (x0 + x1) / 2], bestD = -1;
  const N = 24;
  for (let i = 1; i < N; i++) for (let j = 1; j < N; j++) {
    const x = x0 + ((x1 - x0) * i) / N, y = y0 + ((y1 - y0) * j) / N;
    if (!inside(x, y)) continue;
    const d = distToEdge(x, y);
    if (d > bestD) { bestD = d; best = [y, x]; }
  }
  return best;
}

const boundsOf = (c) => L.latLngBounds(c.meta.bounds);
const loadedCommunes = () => [...communes.values()].filter((c) => c.loaded);

const selectedNetworks = () => Object.keys(state.networks).filter((n) => state.networks[n]);

// Temps de trajet en secondes (mode choisi) et gare la plus proche, tous réseaux choisis confondus
function combineWalk(c) {
  const v = c.L, n = v.commune.length, mode = state.travelMode;
  const walk = new Uint16Array(n).fill(UNREACHED), station = new Uint16Array(n).fill(NO_STATION);
  for (const net of selectedNetworks()) {
    const w = v[`${mode}_${net}`], s = v[`station_${mode}_${net}`];
    // format antérieur (en attente de reconstruction) : tranches de minutes en uint8, ignorées
    if (!w || !s || !(w instanceof Uint16Array)) continue;
    for (let i = 0; i < n; i++) {
      if (w[i] < walk[i]) { walk[i] = w[i]; station[i] = s[i]; }
    }
  }
  c.walkSel = walk; c.stationSel = station;
  c.isoKey = null;
}

// indices et surfaces (m²) des cellules de la commune, calculés une fois
function indexCells(c) {
  const { width: W, row_cell_area_m2: rowArea } = c.meta, mask = c.L.commune;
  let n = 0;
  for (let i = 0; i < mask.length; i++) if (mask[i]) n++;
  c.cellIdx = new Int32Array(n); c.cellArea = new Float32Array(n);
  for (let i = 0, k = 0; i < mask.length; i++) {
    if (mask[i]) { c.cellIdx[k] = i; c.cellArea[k++] = rowArea[Math.floor(i / W)]; }
  }
}

function removeCommuneLayers(c) {
  for (const k of ["context", "zone", "outline", "label"]) if (c[k]) map.removeLayer(c[k]);
  if (c.iso) isoLayer.removeLayer(c.iso);
}

async function loadGlobalLayers() {
  const [stations, acces] = await Promise.all(
    ["stations", "acces"].map((n) => getJSON(`data/global/${n}.geojson`).catch(() => null)));
  accesLayer.clearLayers(); stationsLayer.clearLayers();
  if (acces) accesLayer.addData(acces);
  if (stations) stationsLayer.addData(stations);
  stationMarkers = new Map();
  stationsLayer.eachLayer((l) => stationMarkers.set(l.feature.properties.zdc, l));
  accesByZdc = new Map();
  accesLayer.eachLayer((l) => {
    const z = l.feature.properties.zdc;
    if (!accesByZdc.has(z)) accesByZdc.set(z, []);
    accesByZdc.get(z).push(l);
  });
  hoverStation = null;
  showNearStations(null);
}

async function syncIndex() {
  index = await getJSON("data/index.json").catch(() => ({ communes: [], sources: {} }));
  const wanted = new Map(index.communes.map((c) => [c.code, c]));
  for (const [code, c] of communes) {
    if (!wanted.has(code)) { removeCommuneLayers(c); communes.delete(code); }
  }
  const toLoad = index.communes.filter((c) => !communes.has(c.code) || communes.get(c.code).built !== c.built);
  await Promise.all(toLoad.map((c) => loadCommuneMeta(c.code, c.built)));
  await loadGlobalLayers();
  state.inactive = state.inactive.filter((code) => wanted.has(code));
  if (firstFit) {
    firstFit = false;
    // dernière vue utilisée ; à défaut, toutes les communes
    if (!restoreView() && communes.size) fitTo([...communes.keys()]);
  }
  renderCommuneList();
  buildAirSliders();
  renderSources();
  update({ context: true });
  ensureVisibleLoaded();
}

const VIEW_KEY = "immo_map.view";
function saveView() {
  const c = map.getCenter();
  try { localStorage.setItem(VIEW_KEY, JSON.stringify({ lat: c.lat, lng: c.lng, zoom: map.getZoom() })); } catch (e) { /* idem */ }
}
function restoreView() {
  try {
    const v = JSON.parse(localStorage.getItem(VIEW_KEY) || "null");
    if (v) { map.setView([v.lat, v.lng], v.zoom); return true; }
  } catch (e) { /* idem */ }
  return false;
}

// n'affiche que les gares des réseaux choisis
// seules les gares les plus proches de la souris sont affichées (avec nom et accès) : sur Paris,
// des centaines de gares et des milliers d'accès surchargeaient la carte. La gare retenue dans la bulle
// de survol (la plus rapide à atteindre) en fait toujours partie.
let nearKey = "";
function showNearStations(latlng, preferred = null) {
  const nets = selectedNetworks();
  let chosen = [];
  if (latlng) {
    const k = Math.cos((latlng.lat * Math.PI) / 180);
    const cand = [];
    stationsLayer.eachLayer((l) => {
      const n = (l.feature.properties.networks || "rer").split(",");
      if (!n.some((x) => nets.includes(x))) return;
      const p = l.getLatLng(), dx = (p.lng - latlng.lng) * k, dy = p.lat - latlng.lat;
      cand.push([dx * dx + dy * dy, l]);
    });
    cand.sort((a, b) => a[0] - b[0]);
    chosen = cand.slice(0, NEAR_STATIONS).map((x) => x[1]);
    const pref = preferred && stationMarkers.get(preferred);
    if (pref && !chosen.includes(pref)) {
      if (chosen.length < NEAR_STATIONS) chosen.push(pref); else chosen[NEAR_STATIONS - 1] = pref;
    }
  }
  const key = chosen.map((l) => l.feature.properties.zdc).join("|");
  if (key === nearKey) return;
  nearKey = key;
  nearLayer.clearLayers();
  for (const l of chosen) {
    for (const a of accesByZdc.get(l.feature.properties.zdc) || []) nearLayer.addLayer(a);
    // étiquette du côté opposé à la souris, pour limiter les chevauchements entre gares proches
    const tt = l.getTooltip(), west = l.getLatLng().lng < latlng.lng;
    tt.options.direction = west ? "left" : "right";
    tt.options.offset = west ? [-8, 0] : [8, 0];
    nearLayer.addLayer(l);  // étiquette permanente ouverte à l'ajout
  }
}

function fitTo(codes) {
  const g = L.featureGroup(codes.map((c) => communes.get(c)?.outline).filter(Boolean));
  if (g.getLayers().length) map.fitBounds(g.getBounds(), { padding: [20, 20] });
}

// ------------------------------------------------------------------ carte

function initMap() {
  map = L.map("map", { zoomControl: true }).setView([48.83, 2.48], 13);
  const wmts = (layer, fmtImg) =>
    `https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=${layer}&STYLE=normal` +
    `&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=${fmtImg}`;
  const ign = '© <a href="https://www.ign.fr/">IGN</a>';
  const bases = {
    "Plan IGN": L.tileLayer(wmts("GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2", "image/png"), { maxZoom: 19, attribution: ign }),
    "Photo aérienne": L.tileLayer(wmts("ORTHOIMAGERY.ORTHOPHOTOS", "image/jpeg"), { maxZoom: 19, attribution: ign }),
    "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "© OpenStreetMap" }),
  };
  bases["Plan IGN"].addTo(map);
  L.control.layers(bases, null, { position: "topright" }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);

  // panes : contours d'isochrones et gares au-dessus des surfaces raster
  map.createPane("zone").style.zIndex = 410;
  map.createPane("iso").style.zIndex = 420;
  const labels = map.createPane("labels");  // noms de communes, au-dessus des zones
  labels.style.zIndex = 630;
  labels.style.pointerEvents = "none";
  map.createPane("stations").style.zIndex = 640;
  isoLayer = L.layerGroup().addTo(map);
  // accès et gares dessinés sur canvas (des centaines à milliers de points autour de Paris)
  const pointRenderer = L.canvas({ padding: 0.3, pane: "stations" });
  accesLayer = L.geoJSON(null, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { pane: "stations", renderer: pointRenderer, radius: 2.5, color: "#7a1d4e", weight: 1, fillOpacity: 1 }),
    onEachFeature: (f, l) => l.bindTooltip(`Accès : ${f.properties.nom_acces}`),
  });  // hors carte : seuls les accès des gares proches sont affichés (nearLayer)
  stationsLayer = L.geoJSON(null, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { pane: "stations", renderer: pointRenderer, radius: 6, color: "#fff", weight: 2,
      fillColor: stationColor(f.properties.networks || "rer"), fillOpacity: 1 }),
    onEachFeature: (f, l) => l.bindTooltip(`${f.properties.nom} · ${f.properties.lignes}`,
      { permanent: true, direction: "right", offset: [8, 0], className: "station-label" }),
  });
  nearLayer = L.layerGroup().addTo(map);

  map.on("click", onMapClick);
  map.on("moveend", () => { ensureVisibleLoaded(); drawVisibleZones(); drawIso(); saveView(); });
  const labelsByZoom = () => map.getContainer().classList.toggle("labels-off", map.getZoom() < LABEL_MIN_ZOOM);
  map.on("zoomend", labelsByZoom);
  labelsByZoom();
  initHover();
}

// ------------------------------------------------------------------ filtrage

function thresholds() {
  // temps en secondes ; sans filtre, même les cellules hors d'atteinte passent
  const t = { walk: state.walkFilter ? limitSec(state.walk) : UNREACHED, bp: state.bpNoise, route: state.ldenRoute, fer: state.ldenFer };
  for (const a of AIR) t[a.key] = (state.air[a.key] ?? Infinity) * 10 + 0.5;  // dixièmes de µg/m³
  return t;
}

function cellPasses(c, i, t = thresholds()) {
  const v = c.L;
  return {
    walk: c.walkSel[i] <= t.walk,
    no2: v.no2[i] <= t.no2,
    pm25: v.pm25[i] <= t.pm25,
    pm10: v.pm10[i] <= t.pm10,
    bp: v.bp_noise[i] <= t.bp,     // 0 = non renseigné, accepté
    route: v.lden_route[i] < t.route,  // 0 = non renseigné, accepté ; 40 = moins de 45 dB
    fer: v.lden_fer[i] < t.fer,        // 0 = non renseigné, accepté ; 40 = moins de 45 dB
  };
}

function computeCommune(c, t) {
  const { width: W, height: H } = c.meta;
  const v = c.L, idx = c.cellIdx, area = c.cellArea, walk = c.walkSel;
  const { no2, pm25, pm10, bp_noise: bpn, lden_route: route, lden_fer: fer } = v;
  const tw = t.walk, tn = t.no2, t25 = t.pm25, t10 = t.pm10, tb = t.bp, tr = t.route, tf = t.fer;
  const match = new Uint8Array(W * H);
  let total = 0, ok = 0, cWalk = 0, cAir = 0, cBp = 0, cRoute = 0, cFer = 0;
  for (let k = 0; k < idx.length; k++) {   // cellules de la commune seulement
    const i = idx[k], a = area[k];
    const w = walk[i] <= tw;
    const ai = no2[i] <= tn && pm25[i] <= t25 && pm10[i] <= t10;
    const bp = bpn[i] <= tb, ro = route[i] < tr, fe = fer[i] < tf;
    total += a;
    if (w) cWalk += a;
    if (ai) cAir += a;
    if (bp) cBp += a;
    if (ro) cRoute += a;
    if (fe) cFer += a;
    if (w && ai && bp && ro && fe) { match[i] = 1; ok += a; }
  }
  return { match, total, ok, crit: { walk: cWalk, air: cAir, bp: cBp, route: cRoute, fer: cFer } };
}

// ------------------------------------------------------------------ rendu

function hexToRgb(h) {
  const v = parseInt(h.slice(1), 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

function rampColor(t) {
  const stops = COLORS.ramp.map(hexToRgb);
  const x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
  const k = Math.min(stops.length - 2, Math.floor(x)), f = x - k;
  return stops[k].map((v, j) => Math.round(v + (stops[k + 1][j] - v) * f));
}

function paint(c, fill) {
  const { width: W, height: H } = c.meta;
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(W, H);
  fill(img.data, W, H);
  ctx.putImageData(img, 0, 0);
  return canvas.toDataURL();
}

// ------------------------------------------------------------------ contours lissés

const zoneRenderer = L.canvas({ padding: 0.3, pane: "zone" });  // panneau créé dans initMap
const isoRenderer = L.canvas({ padding: 0.3, pane: "iso" });
const ISO_STYLE = { pane: "iso", renderer: isoRenderer, color: "#08519c", weight: 1.5, dashArray: "5 4", fill: false, interactive: false };

// masque 0/1 -> champ flouté (boîte 3×3 deux fois ≈ gaussienne), bordé d'une cellule à 0.
// Avec k > 1, le masque est d'abord regroupé par blocs k×k (part de cellules retenues dans le bloc) :
// moins de points de contour quand une cellule fait moins d'un pixel à l'écran.
function blurMask(mask, W, H, k = 1) {
  const Wd = Math.ceil(W / k), Hd = Math.ceil(H / k);
  const W2 = Wd + 2, H2 = Hd + 2;
  let a = new Float32Array(W2 * H2), b = new Float32Array(W2 * H2);
  const share = 1 / (k * k);
  for (let r = 0; r < H; r++) {
    const row = (Math.floor(r / k) + 1) * W2 + 1;
    for (let c = 0; c < W; c++) if (mask[r * W + c]) a[row + Math.floor(c / k)] += share;
  }
  for (let pass = 0; pass < 2; pass++) {
    b.fill(0);
    for (let r = 0; r < H2; r++) for (let c = 1; c < W2 - 1; c++) {
      const i = r * W2 + c; b[i] = (a[i - 1] + a[i] + a[i + 1]) / 3;
    }
    a.fill(0);
    for (let r = 1; r < H2 - 1; r++) for (let c = 1; c < W2 - 1; c++) {
      const i = r * W2 + c; a[i] = (b[i - W2] + b[i] + b[i + W2]) / 3;
    }
  }
  return { f: a, W2, H2 };
}

// courbes de niveau 0,5 (marching squares), reliées en anneaux fermés ; coordonnées en indices de cellule
function marchingSquares(f, W2, H2) {
  const nb = new Int32Array(2 * W2 * H2 * 2).fill(-1);   // deux voisins par arête traversée
  const link = (e1, e2) => {
    nb[2 * e1] < 0 ? nb[2 * e1] = e2 : nb[2 * e1 + 1] = e2;
    nb[2 * e2] < 0 ? nb[2 * e2] = e1 : nb[2 * e2 + 1] = e1;
  };
  const Hid = (r, c) => 2 * (r * W2 + c), Vid = (r, c) => 2 * (r * W2 + c) + 1;
  for (let r = 0; r < H2 - 1; r++) {
    for (let c = 0; c < W2 - 1; c++) {
      const tl = f[r * W2 + c], tr = f[r * W2 + c + 1], br = f[(r + 1) * W2 + c + 1], bl = f[(r + 1) * W2 + c];
      const k = (tl > 0.5 ? 8 : 0) | (tr > 0.5 ? 4 : 0) | (br > 0.5 ? 2 : 0) | (bl > 0.5 ? 1 : 0);
      if (k === 0 || k === 15) continue;
      const T = Hid(r, c), B = Hid(r + 1, c), Lf = Vid(r, c), R = Vid(r, c + 1);
      switch (k) {
        case 1: case 14: link(Lf, B); break;
        case 2: case 13: link(B, R); break;
        case 3: case 12: link(Lf, R); break;
        case 4: case 11: link(T, R); break;
        case 6: case 9: link(T, B); break;
        case 7: case 8: link(Lf, T); break;
        case 5: case 10: {  // point selle : tranché par la moyenne au centre
          const centerIn = (tl + tr + br + bl) / 4 > 0.5;
          if ((k === 5) === centerIn) { link(Lf, T); link(B, R); } else { link(Lf, B); link(T, R); }
        }
      }
    }
  }
  const point = (e) => {  // position interpolée du passage à 0,5 sur l'arête e
    const v = e & 1, cell = e >> 1, r = Math.floor(cell / W2), c = cell % W2;
    const a = f[cell], b = v ? f[cell + W2] : f[cell + 1];
    const t = (0.5 - a) / (b - a);
    return v ? [c, r + t] : [c + t, r];
  };
  const seen = new Uint8Array(2 * W2 * H2), rings = [];
  for (let e = 0; e < 2 * W2 * H2; e++) {
    if (nb[2 * e] < 0 || seen[e]) continue;
    const ring = [];
    let prev = -1, cur = e;
    while (cur >= 0 && !seen[cur]) {
      seen[cur] = 1; ring.push(point(cur));
      const n1 = nb[2 * cur], n2 = nb[2 * cur + 1];
      const next = n1 !== prev && !seen[n1] ? n1 : n2;
      prev = cur; cur = next;
    }
    if (ring.length >= 4) rings.push(ring);
  }
  return rings;
}

// lissage de Chaikin sur un anneau fermé (converge vers une B-spline quadratique)
function chaikin(ring, iterations = 2) {
  let pts = ring;
  for (let k = 0; k < iterations; k++) {
    const out = [];
    for (let i = 0; i < pts.length; i++) {
      const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length];
      out.push([0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1], [0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1]);
    }
    pts = out;
  }
  return pts;
}

// contours lissés d'un masque de cellules, en LatLng
// simplification de Douglas-Peucker d'un anneau fermé (tolérance dans l'unité des coordonnées)
function simplify(ring, tol) {
  if (ring.length < 8 || tol <= 0) return ring;
  const keep = new Uint8Array(ring.length), tol2 = tol * tol;
  keep[0] = keep[ring.length - 1] = 1;
  const stack = [[0, ring.length - 1]];
  while (stack.length) {
    const [i0, i1] = stack.pop();
    const [ax, ay] = ring[i0], [bx, by] = ring[i1];
    const dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy || 1;
    let worst = -1, wd = tol2;
    for (let i = i0 + 1; i < i1; i++) {
      const [px, py] = ring[i];
      const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2));
      const ex = px - ax - t * dx, ey = py - ay - t * dy, d2 = ex * ex + ey * ey;
      if (d2 > wd) { wd = d2; worst = i; }
    }
    if (worst > 0) { keep[worst] = 1; stack.push([i0, worst], [worst, i1]); }
  }
  const out = ring.filter((_, i) => keep[i]);
  return out.length >= 3 ? out : ring;
}

// contours lissés d'un masque de cellules, en LatLng, détaillés selon le zoom courant
function smoothContours(mask, meta) {
  const { left, top, cell } = meta.merc;
  const mpp = 156543.03392804097 / Math.pow(2, Math.round(map.getZoom()));  // mètres Mercator par pixel
  let k = 1;
  while (cell * k * 2 <= mpp) k *= 2;  // cellules regroupées tant qu'elles font moins d'un pixel
  const { f, W2, H2 } = blurMask(mask, meta.width, meta.height, k);
  const step = cell * k, tol = (0.35 * mpp) / step;  // ~1/3 de pixel, en cellules regroupées
  return marchingSquares(f, W2, H2).map((ring) => simplify(chaikin(ring), tol).map(([c, r]) =>
    L.CRS.EPSG3857.unproject(L.point(left + (c - 1) * step + step / 2, top - (r - 1) * step - step / 2))));
}

// anneaux (LatLng) d'une géométrie GeoJSON Polygon / MultiPolygon
function geoRings(geom) {
  const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.type === "MultiPolygon" ? geom.coordinates : [];
  return polys.flatMap((poly) => poly.map((ring) => ring.slice(0, -1).map(([lon, lat]) => L.latLng(lat, lon))));
}

// rendu « projecteur » : hors zone assombri, zone retenue claire et cernée d'un trait souligné de blanc.
// Avec une couche de contexte, la zone n'est pas teintée (ses couleurs restent lisibles) et le trait
// est noir, couleur absente des légendes (le vert se confondait avec les zones calmes du bruit).
const ZONE_STYLE = {
  plain:   { dim: 0.5, fill: "#2ecc71", fillOpacity: 0.18, line: "#0b6b45" },
  context: { dim: 0.3, fill: null, fillOpacity: 0, line: "#111" },
};

function drawZone(c, res) {
  c.zone.clearLayers();
  if (!isActive(c.code) || !state.showZone) return;
  const rings = smoothContours(res.match, c.meta);
  const st = ZONE_STYLE[state.context === "none" ? "plain" : "context"];
  const base = { pane: "zone", renderer: zoneRenderer, interactive: false, fillRule: "evenodd" };
  // voile sur la commune hors zone retenue : contour de la commune + contours de zone (règle pair-impair)
  c.zone.addLayer(L.polygon([...c.outlineRings, ...rings],
    { ...base, stroke: false, fillColor: "#1b1b20", fillOpacity: st.dim }));
  if (!rings.length) return;
  // remplissage éventuel et liseré blanc dans un même tracé, puis le trait par-dessus
  c.zone.addLayer(L.polygon(rings, { ...base, fill: !!st.fill, fillColor: st.fill || "#000",
    fillOpacity: st.fillOpacity, color: "#fff", weight: 3, opacity: 0.9 }));
  c.zone.addLayer(L.polygon(rings, { ...base, fill: false, color: st.line, weight: 1.5 }));
}

// temps (s) -> tranche de la légende (5, 10, 15, 20 min), 0 au-delà
const walkBin = (sec) => {
  const m = Math.round(sec / 60);
  return m > 20 ? 0 : Math.max(5, 5 * Math.ceil(m / 5));
};

function contextColorFn(key) {
  if (key in airRange) {
    const { min, max } = airRange[key];
    return (v) => v ? rampColor((v / 10 - min) / (max - min)) : null;
  }
  const rgb = Object.fromEntries(Object.entries(COLORS[key]).map(([k, h]) => [k, hexToRgb(h)]));
  return (v) => rgb[v] || null;
}

function drawContext(only = null) {
  const key = state.context;
  if (!only) renderLegend();
  for (const c of only ? [only] : loadedCommunes()) {
    if (key === "none") { c.context.setUrl(EMPTY_PNG); continue; }
    const colorOf = contextColorFn(key), mask = c.L.commune;
    const src = key === "walk" ? c.walkSel.map(walkBin) : c.L[key];
    c.context.setUrl(paint(c, (d, W, H) => {
      for (let i = 0; i < W * H; i++) {
        if (!mask[i]) continue;   // chaque grille ne peint que sa commune : pas de double couche aux bords
        const k = colorOf(src[i]);
        if (!k) continue;
        const p = i * 4;
        d[p] = k[0]; d[p + 1] = k[1]; d[p + 2] = k[2]; d[p + 3] = 255;
      }
    }));
  }
}

function renderLegend() {
  const key = state.context, legend = $("legend");
  if (key === "none") { legend.innerHTML = ""; return; }
  if (key in airRange) {
    const { min, max } = airRange[key];
    legend.innerHTML = `<div class="ramp" style="background:linear-gradient(90deg,${COLORS.ramp.join(",")})"></div>
      <div class="ends"><span>${fmt(min)} µg/m³</span><span>${fmt(max)} µg/m³</span></div>`;
    return;
  }
  const label = key === "walk" ? (k) => `≤ ${k} min` : key === "bp_noise" ? (k) => BP_NOISE_LABELS[k]
    : (k) => +k === 40 ? "< 45 dB" : +k === 75 ? "≥ 75" : `${k}–${+k + 5}`;
  legend.innerHTML = Object.entries(COLORS[key]).map(([k, h]) =>
    `<span><i class="sw" style="background:${h}"></i>${label(k)}</span>`).join("");
}

// contour de la zone atteignable dans le temps choisi, tracé à partir des temps de trajet de chaque commune
// (même lissage que la zone retenue) ; seulement pour les communes visibles, et si le réglage a changé
function drawIso() {
  const key = state.showIso && state.walkFilter
    ? `${state.travelMode}|${selectedNetworks().join("+")}|${state.walk}|${Math.round(map.getZoom())}` : "";
  for (const c of loadedCommunes()) {
    if (c.isoKey === key || !isVisible(c)) continue;
    c.isoKey = key;
    c.iso.clearLayers();
    if (!key) continue;
    // limité à la commune : sinon le contour s'arrête net au bord de la grille (bande de 300 m autour),
    // ce qui dessine des traits droits parasites dans les communes voisines
    const limit = limitSec(state.walk), t = c.walkSel, own = c.L.commune, mask = new Uint8Array(t.length);
    for (let i = 0; i < t.length; i++) mask[i] = own[i] && t[i] <= limit ? 1 : 0;
    const rings = smoothContours(mask, c.meta);
    if (rings.length) c.iso.addLayer(L.polygon(rings, ISO_STYLE));
  }
}

function renderResult(results) {
  const km2 = (m2) => fmt(m2 / 1e6, 2);
  const pct = (a, b) => b ? Math.round(100 * a / b) : 0;
  const active = sortedCommunes().filter((c) => isActive(c.code));
  if (!active.length) { $("result").innerHTML = '<p class="note">Aucune commune sélectionnée.</p>'; return; }
  let total = 0, ok = 0;
  const crit = { walk: 0, air: 0, route: 0, fer: 0, bp: 0 };
  const missing = active.filter((c) => !results.has(c.code));
  const rows = active.filter((c) => results.has(c.code)).map((c) => {
    const r = results.get(c.code);
    total += r.total; ok += r.ok;
    for (const k in crit) crit[k] += r.crit[k];
    return `<span>${c.meta.nom}</span><span>${km2(r.ok)} km²</span><span class="muted">${pct(r.ok, r.total)} %</span>`;
  }).join("");
  const critTxt = [["Marche", crit.walk], ["Air", crit.air], ["Bruit routier", crit.route], ["Bruit ferroviaire", crit.fer], ["Indice global", crit.bp]]
    .map(([n, a]) => `${n} ${pct(a, total)} %`).join(" · ");
  $("result").innerHTML = `
    <div class="big">${km2(ok)} km² <small>soit ${pct(ok, total)} % de la surface</small></div>
    <div class="per">${rows}</div>
    <div class="limits">Part de la surface respectant chaque critère pris seul : ${critTxt}</div>
    ${missing.length ? `<div class="limits">Non comptées (pas encore chargées) : ${missing.map((c) => c.meta.nom).join(", ")}</div>` : ""}`;
}

// contours recalculés seulement pour les communes visibles ; les autres le sont au déplacement de la carte
function isVisible(c) {
  const [[s, w], [n, e]] = c.meta.bounds;
  return map.getBounds().pad(0.2).intersects(L.latLngBounds([s, w], [n, e]));
}

function drawVisibleZones() {
  const z = Math.round(map.getZoom());  // le détail des contours dépend du zoom
  for (const c of loadedCommunes()) {
    if ((c.zoneDirty || c.zoneZoom !== z) && isVisible(c)) {
      drawZone(c, c.lastResult); c.zoneDirty = false; c.zoneZoom = z;
    }
  }
}

function update({ context = false } = {}) {
  const t = thresholds();
  const results = new Map();
  for (const c of loadedCommunes()) {
    const r = computeCommune(c, t);
    results.set(c.code, r);
    c.lastResult = r;
    c.zoneDirty = true;
  }
  drawVisibleZones();
  if (context) drawContext();
  drawIso();
  if (serverMode) requestStats(); else renderResult(results);
  saveState();
}

// surfaces de toutes les communes actives, calculées par le serveur (les communes hors écran
// ne sont pas chargées dans le navigateur) ; une seule requête en vol, la plus récente gagne
let statsCtrl = null, statsTimer = 0;
function requestStats() {
  clearTimeout(statsTimer);
  statsTimer = setTimeout(async () => {
    if (statsCtrl) statsCtrl.abort();
    statsCtrl = new AbortController();
    const query = {
      codes: [...communes.keys()].filter(isActive), mode: state.travelMode, networks: selectedNetworks(),
      walk: state.walkFilter ? state.walk : null, air: state.air, bp: state.bpNoise,
      route: state.ldenRoute, fer: state.ldenFer,
    };
    try {
      const r = await fetch("api/stats", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(query), signal: statsCtrl.signal });
      const j = await r.json();
      renderResult(new Map(Object.entries(j)));
    } catch (e) {
      if (e.name !== "AbortError") console.error(e);
    }
  }, 120);
}

// pendant le glissement d'un curseur : au plus une mise à jour par image affichée
let updateFrame = 0;
function scheduleUpdate() {
  if (!updateFrame) updateFrame = requestAnimationFrame(() => { updateFrame = 0; update(); });
}

// ------------------------------------------------------------------ liste des communes

const sortedCommunes = () => [...communes.values()].sort((a, b) => a.meta.nom.localeCompare(b.meta.nom, "fr"));
let lastStatus = null;

function renderCommuneList(jobStatus = lastStatus) {
  lastStatus = jobStatus;
  const building = new Set();
  if (jobStatus) {
    if (jobStatus.current) building.add(jobStatus.current.code);
    jobStatus.pending.forEach((c) => building.add(c));
  }
  const items = sortedCommunes().map((c) => `
    <li>
      <label><input type="checkbox" data-code="${c.code}" ${isActive(c.code) ? "checked" : ""}>
        <span>${c.meta.nom}</span></label>
      ${!(c.meta.fer_coverage < 0.9) ? "" : `<span class="warn" title="Bruit ferroviaire connu sur ${Math.round(100 * c.meta.fer_coverage)} % de la commune seulement.">⚠ fer</span>`}
      ${c.meta.format === undefined || c.meta.format < DATA_FORMAT
        ? '<span class="warn" title="Données produites par une ancienne version : relancez ./run.sh pour les reconstruire.">⚠ à reconstruire</span>'
        : c.meta.route_coverage >= 0.9 ? "" : `<span class="warn" title="Bruit routier connu sur ${Math.round(100 * c.meta.route_coverage)} % de la commune seulement (hors agglomération, seuls les grands axes sont cartographiés).">⚠ route</span>`}
      ${building.has(c.code) ? '<span class="building">mise à jour…</span>' : ""}
      <button type="button" class="zoom" data-zoom="${c.code}" title="Centrer la carte">${ICON_TARGET}</button>
      ${serverMode ? `<button type="button" class="del" data-del="${c.code}" title="Retirer la commune">×</button>` : ""}
    </li>`);
  $("communes").innerHTML = items.join("") || '<li class="note">Aucune commune chargée.</li>';
  $("commune-count").textContent = communes.size ? `${communes.size - state.inactive.length} / ${communes.size} actives` : "";
}

function initCommuneList() {
  const ul = $("communes");
  // liste repliable, état mémorisé
  const toggle = $("toggle-communes");
  const setFolded = (folded) => {
    ul.hidden = folded;
    toggle.setAttribute("aria-expanded", String(!folded));
    toggle.title = folded ? "Déplier la liste des communes" : "Replier la liste des communes";
    try { localStorage.setItem("immo_map.communesFolded", folded ? "1" : ""); } catch (e) { /* stockage indisponible */ }
  };
  let folded = false;
  try { folded = localStorage.getItem("immo_map.communesFolded") === "1"; } catch (e) { /* idem */ }
  setFolded(folded);
  toggle.addEventListener("click", () => setFolded(!ul.hidden));
  ul.addEventListener("change", (e) => {
    const code = e.target.dataset.code; if (!code) return;
    state.inactive = e.target.checked ? state.inactive.filter((c) => c !== code) : [...state.inactive, code];
    renderCommuneList(); buildAirSliders(); update({ context: true });
  });
  ul.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const z = btn.dataset.zoom, d = btn.dataset.del;
    if (z) fitTo([z]);
    if (d) {
      btn.disabled = true;
      await fetch(`api/commune/${d}`, { method: "DELETE" });
      pollStatus();
    }
  });
}

// ------------------------------------------------------------------ ajout de communes (mode serveur)

async function requestBuild(codes) {
  const r = await fetch("api/build", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ codes }),
  });
  const j = await r.json();
  if (!r.ok) showJobMessage(j.error || "erreur", true);
  pollStatus();
}

function showJobMessage(msg, error = false) {
  const box = $("jobs");
  box.hidden = false;
  box.innerHTML = `<span class="${error ? "err" : ""}">${msg}</span>`;
}

function initSearch() {
  const input = $("search"), list = $("suggestions");
  let timer, results = [], sel = -1;
  const show = () => {
    list.hidden = false;
    list.innerHTML = results.length ? results.map((c, k) =>
      `<li data-code="${c.code}" class="${k === sel ? "sel" : ""}">${c.nom} <small>(${c.dep})${communes.has(c.code) ? " · déjà chargée" : ""}</small></li>`).join("")
      : '<li class="empty">Aucune commune d\'Île-de-France trouvée</li>';
  };
  const pick = (code) => {
    list.hidden = true; input.value = "";
    if (communes.has(code)) fitTo([code]); else requestBuild([code]);
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { list.hidden = true; return; }
    timer = setTimeout(async () => {
      results = await getJSON(`api/search?q=${encodeURIComponent(q)}`).catch(() => []);
      sel = results.length ? 0 : -1; show();
    }, 200);
  });
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { sel = Math.min(results.length - 1, sel + 1); show(); e.preventDefault(); }
    if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); show(); e.preventDefault(); }
    if (e.key === "Enter" && sel >= 0) { pick(results[sel].code); e.preventDefault(); }
    if (e.key === "Escape") list.hidden = true;
  });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-code]"); if (li) pick(li.dataset.code);
  });
  input.addEventListener("blur", () => setTimeout(() => { list.hidden = true; }, 150));

  $("add-visible").addEventListener("click", async () => {
    const b = map.getBounds();
    const j = await getJSON(`api/bbox?w=${b.getWest()}&s=${b.getSouth()}&e=${b.getEast()}&n=${b.getNorth()}`);
    if (j.total > j.max) {
      showJobMessage(`${j.total} communes visibles : zoomez davantage (${j.max} au maximum à la fois).`, true);
      return;
    }
    const todo = j.communes.filter((c) => !communes.has(c.code)).map((c) => c.code);
    if (!todo.length) { showJobMessage("Toutes les communes visibles sont déjà chargées."); return; }
    requestBuild(todo);
  });
}

// ------------------------------------------------------------------ âge des données et mise à jour

const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" }) : "?";
let refreshRunning = false;

async function loadFreshness() {
  if (!serverMode) return;
  let f;
  try { f = await getJSON("api/freshness"); } catch (e) { return; }
  $("freshness-box").hidden = false;
  const btn = $("refresh-btn"), note = $("refresh-note");
  const stale = f.sources.filter((x) => x.stale);
  if (refreshRunning) {
    btn.disabled = true;
    note.textContent = "Mise à jour en cours (suivi dans la section Communes).";
  } else if (f.to_update) {
    btn.disabled = false;
    note.textContent = `${stale.length} source(s) et ${f.communes.length} commune(s) concernées ; ` +
      `données les plus anciennes : ${fmtDate(f.oldest)}. Les données actuelles restent utilisées jusqu'à leur remplacement.`;
  } else {
    btn.disabled = true;
    note.textContent = `Rien à mettre à jour : toutes les données ont moins de 6 mois (les plus anciennes datent du ${fmtDate(f.oldest)}).`;
  }
  $("refresh-details").hidden = !f.sources.length;
  $("refresh-sources").innerHTML = f.sources.map((x) =>
    `<li class="${x.stale ? "stale" : ""}">${x.label} : ${x.files} fichier(s), du ${fmtDate(x.oldest)}` +
    `${x.stale ? ` — ${x.stale} de plus de 6 mois` : ""}</li>`).join("");
}

function initRefresh() {
  $("refresh-btn").addEventListener("click", async () => {
    $("refresh-btn").disabled = true;
    await fetch("api/refresh", { method: "POST" });
    refreshRunning = true;
    loadFreshness();
    pollStatus();
  });
}

let pollTimer = null;
async function pollStatus() {
  clearTimeout(pollTimer);
  if (!serverMode) return;
  let s;
  try { s = await getJSON("api/status"); } catch (e) { pollTimer = setTimeout(pollStatus, 5000); return; }
  const busy = s.current || s.pending.length;
  const box = $("jobs");
  const errs = Object.entries(s.errors).map(([code, m]) => `<div class="err">Échec ${code} : ${m}</div>`).join("");
  if (busy) {
    box.hidden = false;
    box.innerHTML = (s.current ? `<div><span class="spin"></span>${s.current.step}</div>` : "") +
      (s.pending.length ? `<div>${s.pending.length} commune(s) en attente</div>` : "") + errs;
  } else if (errs) {
    box.hidden = false; box.innerHTML = errs;
  } else if (box.querySelector(".spin")) {
    box.hidden = true;
  }
  const running = (s.current && s.current.code === "__refresh__") || s.pending.includes("__refresh__");
  if (lastVersion !== null && s.version !== lastVersion) { await syncIndex(); loadFreshness(); }
  if (running !== refreshRunning) { refreshRunning = running; loadFreshness(); }
  lastVersion = s.version;
  renderCommuneList(s);
  pollTimer = setTimeout(pollStatus, busy ? 1500 : 5000);
}

// ------------------------------------------------------------------ popup

function cellAt(latlng) {
  const p = L.CRS.EPSG3857.project(latlng);
  for (const c of loadedCommunes()) {
    const m = c.meta;
    const col = Math.floor((p.x - m.merc.left) / m.merc.cell);
    const row = Math.floor((m.merc.top - p.y) / m.merc.cell);
    if (col < 0 || row < 0 || col >= m.width || row >= m.height) continue;
    const i = row * m.width + col;
    if (c.L.commune[i]) return { c, i };
  }
  return null;
}

// ------------------------------------------------------------------ survol : gare la plus proche

let hoverBox, hoverEvt = null, hoverFrame = 0, hoverStation = null;
let stationMarkers = new Map();  // zdc -> marqueur

function initHover() {
  // encadré d'information fixe, dans le coin supérieur droit de la carte (sous le choix du fond de carte)
  hoverBox = L.DomUtil.create("div", "hover-box", map.getContainer());
  hoverBox.hidden = true;
  map.on("mousemove", (e) => {
    hoverEvt = e;
    if (!hoverFrame) hoverFrame = requestAnimationFrame(renderHover);
  });
  // sortie de la carte (vers le menu…) : événement du navigateur, plus fiable que « mouseout » de Leaflet
  map.getContainer().addEventListener("mouseleave", () => { hoverEvt = null; hideHover(); showNearStations(null); });
  map.on("movestart", hideHover);  // la bulle ne correspondrait plus au point sous la souris
}

function highlightStation(zdc) {
  if (hoverStation === zdc) return;
  const prev = stationMarkers.get(hoverStation);
  if (prev) prev.setStyle({ radius: 6, color: "#fff", weight: 2 });
  hoverStation = zdc;
  const cur = stationMarkers.get(zdc);
  if (cur) { cur.setStyle({ radius: 10, color: "#1f2328", weight: 3 }); cur.bringToFront(); }
}

function hideHover() {
  hoverBox.hidden = true;
  highlightStation(null);
}

// gare la plus rapide à atteindre depuis la cellule i pour un mode donné, réseaux cochés
function bestStation(c, i, mode) {
  let t = UNREACHED, si = NO_STATION;
  for (const net of selectedNetworks()) {
    const w = c.L[`${mode}_${net}`], s = c.L[`station_${mode}_${net}`];
    if (w instanceof Uint16Array && s && w[i] < t) { t = w[i]; si = s[i]; }
  }
  return { t, st: si < c.meta.stations.length ? c.meta.stations[si] : null };
}

// raisons d'exclusion d'un point : critère, valeur locale et seuil demandé
const BP_LIMIT_LABELS = { 1: "préservé seulement", 2: "« très dégradé » exclu" };
function exclusionHtml(c, i, ok) {
  if (!isActive(c.code)) return '<div class="verdict muted">Commune décochée</div>';
  const v = c.L, reasons = [];
  if (!ok.walk) {
    const t = c.walkSel[i];
    reasons.push(`Trajet ${t === UNREACHED ? "plus d'1 h" : fmtMin(t)} ${state.travelMode === "bike" ? "à vélo" : "à pied"}
      <span class="muted">(seuil ${state.walk} min)</span>`);
  }
  if (!ok.route) reasons.push(`Bruit routier ${ldenTxt(v.lden_route[i])} <span class="muted">(seuil &lt; ${state.ldenRoute} dB)</span>`);
  if (!ok.fer) reasons.push(`Bruit ferroviaire ${ldenTxt(v.lden_fer[i])} <span class="muted">(seuil &lt; ${state.ldenFer} dB)</span>`);
  if (!ok.bp) reasons.push(`Indice global ${BP_NOISE_LABELS[v.bp_noise[i]]} <span class="muted">(${BP_LIMIT_LABELS[state.bpNoise]})</span>`);
  for (const a of AIR) {
    if (!ok[a.key]) reasons.push(`${a.label} ${fmt(v[a.key][i] / 10)} µg/m³ <span class="muted">(seuil ${fmt(state.air[a.key])})</span>`);
  }
  if (!reasons.length) return '<div class="verdict ok">✓ Dans la zone retenue</div>';
  return `<div class="verdict"><span class="ko">✗ Exclu :</span><ul>${reasons.map((r) => `<li>${r}</li>`).join("")}</ul></div>`;
}

function renderHover() {
  hoverFrame = 0;
  const e = hoverEvt;
  const hit = e && cellAt(e.latlng);
  if (!hit) { hideHover(); if (e) showNearStations(e.latlng); return; }
  const { c, i } = hit;
  const limit = state.walk, mode = state.travelMode;
  const res = { walk: bestStation(c, i, "walk"), bike: bestStation(c, i, "bike") };
  const sel = res[mode];
  const reached = sel.st && (!state.walkFilter || sel.t <= limitSec(limit));
  const how = { walk: "à pied", bike: "à vélo" };
  // en-tête : la gare retenue pour le mode sélectionné, ou l'absence de gare dans le seuil
  let html = reached
    ? `<strong>${sel.st.nom}</strong><br><span class="muted">${sel.st.lignes.join(" + ")}</span>`
    : `<span class="ko">Aucune gare à ${limit} min ${how[mode]} ou moins</span>`;
  // les deux modes, toujours
  const rows = ["walk", "bike"].map((m) => {
    const r = res[m];
    const time = r.st ? fmtMin(r.t) : "plus d'1 h";
    const other = r.st && (!reached || r.st.zdc !== sel.st.zdc) ? ` · ${r.st.nom}` : "";
    return `<tr class="${m === mode ? "cur" : ""}"><td>${MODE_LABELS[m]}</td><td>${time}${other}</td></tr>`;
  }).join("");
  html += `<table>${rows}</table>`;
  // niveaux de bruit au point survolé, pastille aux couleurs de la légende
  const v = c.L;
  const sw = (key, x) => COLORS[key][x] ? `<i class="sw" style="background:${COLORS[key][x]}"></i>` : '<i class="sw"></i>';
  const ok = cellPasses(c, i);
  const bad = (k) => ok[k] ? "" : ' class="fail"';
  html += `<table class="noise">
    <tr${bad("route")}><td>${sw("lden_route", v.lden_route[i])}Bruit routier</td><td>${ldenTxt(v.lden_route[i])}</td></tr>
    <tr${bad("fer")}><td>${sw("lden_fer", v.lden_fer[i])}Bruit ferroviaire</td><td>${ldenTxt(v.lden_fer[i])}</td></tr>
    <tr${bad("bp")}><td>${sw("bp_noise", v.bp_noise[i])}Indice global</td><td>${BP_NOISE_LABELS[v.bp_noise[i]]}</td></tr>
  </table>`;
  // pollution de l'air, pastille selon les repères : vert sous la recommandation OMS,
  // jaune jusqu'à la valeur limite UE 2030, rouge au-delà
  const airSw = (a, x) => `<i class="sw" style="background:${x <= a.oms ? "#4bc700" : x <= a.ue2030 ? "#fdd049" : "#d7301f"}"
    title="OMS ${a.oms} · UE 2030 ${a.ue2030} µg/m³"></i>`;
  html += `<table class="noise">${AIR.map((a) => {
    const x = v[a.key][i] / 10;
    return `<tr${bad(a.key)}><td>${airSw(a, x)}${a.label} ${c.meta.air_year}</td><td>${fmt(x)} µg/m³</td></tr>`;
  }).join("")}</table>`;
  html += exclusionHtml(c, i, ok);
  hoverBox.innerHTML = html;
  hoverBox.hidden = false;
  showNearStations(e.latlng, sel.st && sel.st.zdc);
  highlightStation(reached ? sel.st.zdc : null);
}

// clic hors des communes chargées : proposer d'ajouter la commune (les informations d'un point
// d'une commune chargée sont dans la bulle de survol)
async function onMapClick(e) {
  if (!serverMode || cellAt(e.latlng)) return;
  const found = await getJSON(`api/at?lon=${e.latlng.lng}&lat=${e.latlng.lat}`).catch(() => []);
  if (!found.length || communes.has(found[0].code)) return;
  const f = found[0];
  const pop = L.popup().setLatLng(e.latlng).setContent(
    `<strong>${f.nom}</strong><br><span class="note">Commune non chargée.</span>
     <button type="button" class="btn primary" id="add-here">Ajouter cette commune</button>`).openOn(map);
  $("add-here").addEventListener("click", () => { map.closePopup(pop); requestBuild([f.code]); });
}

// ------------------------------------------------------------------ contrôles

function buildAirSliders() {
  const box = $("air-sliders");
  box.innerHTML = "";
  airRange = {};
  const active = [...communes.values()].filter((c) => isActive(c.code));
  const pool = active.length ? active : [...communes.values()];
  if (!pool.length) return;
  $("air-year").textContent = `moyennes annuelles ${pool[0].meta.air_year}`;
  for (const a of AIR) {
    let min = Infinity, max = -Infinity;
    for (const c of pool) {  // résumé calculé à la construction : pas besoin des couches détaillées
      const r = c.meta.air_range?.[a.key];
      if (r) { min = Math.min(min, r[0]); max = Math.max(max, r[1]); }
    }
    if (!isFinite(min)) continue;
    min = Math.floor(min * 2) / 2; max = Math.ceil(max * 2) / 2;
    if (max <= min) max = min + 0.5;
    airRange[a.key] = { min, max };
    if (state.air[a.key] !== undefined && state.air[a.key] >= max) delete state.air[a.key];
    const value = state.air[a.key] === undefined ? max : Math.max(min, state.air[a.key]);
    const pos = (x) => `${100 * (x - min) / (max - min)}%`;
    const refs = [["OMS", a.oms], ["UE 2030", a.ue2030]]
      .filter(([, x]) => x >= min && x <= max)
      .map(([n, x]) => {
        const f = (x - min) / (max - min);
        const cls = f < 0.1 ? "l" : f > 0.9 ? "r" : "";
        return `<span class="${cls}" style="left:${pos(x)}">${n} ${x}</span>`;
      }).join("");
    const notes = [];
    if (a.oms < min) notes.push(`Recommandation OMS (${a.oms} µg/m³) dépassée partout.`);
    if (a.ue2030 < min) notes.push(`Valeur limite UE 2030 (${a.ue2030} µg/m³) dépassée partout.`);
    if (a.ue2030 > max) notes.push(`Valeur limite UE 2030 (${a.ue2030} µg/m³) respectée partout.`);
    const div = document.createElement("div");
    div.className = "air";
    div.innerHTML = `
      <div class="row"><span class="name">${a.label}</span><span class="val"></span></div>
      <input type="range" min="${min}" max="${max}" step="0.1" value="${value}">
      <div class="refs">${refs}</div>
      ${notes.length ? `<div class="note">${notes.join(" ")}</div>` : ""}`;
    box.appendChild(div);
    const s = div.querySelector("input"), out = div.querySelector(".val");
    const show = () => {
      const x = state.air[a.key];
      out.textContent = x === undefined ? "pas de filtre" : `≤ ${fmt(x)} µg/m³`;
    };
    s.addEventListener("input", () => {
      if (+s.value >= max) delete state.air[a.key]; else state.air[a.key] = +s.value;
      show(); scheduleUpdate();
    });
    show();
  }
}

function renderSources() {
  $("sources").innerHTML = "<p><strong>Sources</strong></p>" +
    Object.values(index?.sources || {}).map((s) => `<p>${s}</p>`).join("") +
    "<p>Isochrones IGN : vitesse de marche implicite ≈ 4,2 km/h, depuis les entrées des gares.</p>";
}

function initControls() {
  const walk = $("walk");
  const [wMin, wMax] = [+walk.min, +walk.max];
  walk.parentElement.querySelectorAll(".ticks span").forEach((t) => {
    t.style.left = `${100 * (parseInt(t.textContent) - wMin) / (wMax - wMin)}%`;
  });
  state.walk = Math.min(wMax, Math.max(wMin, state.walk));
  const walkFilter = $("walk-filter");
  const showWalk = () => {
    $("walk-out").textContent = state.walkFilter ? `≤ ${state.walk} min` : "pas de filtre";
    $("walk-controls").classList.toggle("off", !state.walkFilter);
    walk.disabled = !state.walkFilter;
  };
  walk.value = state.walk;
  walkFilter.checked = state.walkFilter;
  walk.addEventListener("input", () => { state.walk = +walk.value; showWalk(); scheduleUpdate(); });
  walkFilter.addEventListener("change", () => { state.walkFilter = walkFilter.checked; showWalk(); update(); });
  const modeSeg = $("travel-mode");
  const showMode = () => modeSeg.querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.mode === state.travelMode)));
  modeSeg.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b || b.dataset.mode === state.travelMode) return;
    state.travelMode = b.dataset.mode; showMode();
    for (const c of loadedCommunes()) combineWalk(c);
    update({ context: state.context === "walk" });
  });
  showMode();
  for (const net of Object.keys(state.networks)) {
    const box = $(`net-${net}`);
    box.checked = state.networks[net];
    box.addEventListener("change", () => {
      state.networks[net] = box.checked;
      for (const c of loadedCommunes()) combineWalk(c);
      showNearStations(hoverEvt && hoverEvt.latlng);
      update({ context: state.context === "walk" });
    });
  }
  showWalk();

  const bind = (id, key, ctx = false, prop = "value") => {
    const el = $(id);
    el[prop] = state[key];
    el.addEventListener("change", () => {
      state[key] = prop === "checked" ? el.checked : (isNaN(+el.value) ? el.value : +el.value);
      update({ context: ctx });
    });
  };
  bind("bp-noise", "bpNoise");
  bind("lden-route", "ldenRoute");
  bind("lden-fer", "ldenFer");
  bind("context", "context", true);
  bind("show-zone", "showZone", false, "checked");
  bind("show-iso", "showIso", false, "checked");
  initCommuneList();
}

// ------------------------------------------------------------------ menu : largeur et masquage

const PANEL_KEY = "immo_map.panel";
const PANEL_DEFAULT = 360, PANEL_MIN = 260, PANEL_MAX = 720;

function initPanel() {
  const panel = $("panel"), resizer = $("resizer"), root = document.documentElement;
  let pref = { width: PANEL_DEFAULT, hidden: false };
  try { Object.assign(pref, JSON.parse(localStorage.getItem(PANEL_KEY) || "{}")); } catch (e) { /* stockage indisponible */ }
  const save = () => { try { localStorage.setItem(PANEL_KEY, JSON.stringify(pref)); } catch (e) { /* idem */ } };
  const setWidth = (w) => {
    pref.width = Math.round(Math.min(PANEL_MAX, Math.max(PANEL_MIN, w, 0)));
    root.style.setProperty("--panel-width", `${pref.width}px`);
  };

  // bouton « Menu » sur la carte, visible quand le menu est masqué
  const ShowControl = L.Control.extend({
    options: { position: "topleft" },
    onAdd() {
      const b = L.DomUtil.create("button", "leaflet-control show-panel");
      b.type = "button"; b.textContent = "☰ Menu"; b.title = "Afficher le menu";
      L.DomEvent.disableClickPropagation(b);
      L.DomEvent.on(b, "click", () => setHidden(false));
      return b;
    },
  });
  const showControl = new ShowControl();
  const setHidden = (h) => {
    pref.hidden = h;
    document.body.classList.toggle("panel-hidden", h);
    if (h) showControl.addTo(map); else showControl.remove();
    map.invalidateSize();
    save();
  };
  $("hide-panel").addEventListener("click", () => setHidden(true));

  // poignée de redimensionnement
  resizer.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    resizer.setPointerCapture(e.pointerId);
    resizer.classList.add("dragging"); document.body.classList.add("resizing");
    const x0 = e.clientX, w0 = panel.getBoundingClientRect().width;
    let frame = 0;
    const move = (ev) => {
      setWidth(w0 + ev.clientX - x0);
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; map.invalidateSize(); });
    };
    const up = () => {
      resizer.removeEventListener("pointermove", move);
      resizer.classList.remove("dragging"); document.body.classList.remove("resizing");
      map.invalidateSize(); save();
    };
    resizer.addEventListener("pointermove", move);
    resizer.addEventListener("pointerup", up, { once: true });
  });
  resizer.addEventListener("dblclick", () => { setWidth(PANEL_DEFAULT); map.invalidateSize(); save(); });

  setWidth(pref.width);
  if (pref.hidden) setHidden(true);
}

async function main() {
  loadState();
  initMap();
  initPanel();
  initControls();
  try {
    lastVersion = (await getJSON("api/status")).version;
    serverMode = true;
  } catch (e) { serverMode = false; }
  $("add-box").hidden = !serverMode;
  $("static-note").hidden = serverMode;
  if (serverMode) { initSearch(); initRefresh(); loadFreshness(); }
  await syncIndex();
  if (serverMode) pollStatus();
}

main().catch((err) => {
  console.error(err);
  $("result").innerHTML = `<p class="note">Erreur de chargement des données : ${err}.<br>
    Lancez <code>./run.sh</code> depuis le dossier du projet puis ouvrez http://localhost:8000/.</p>`;
});
