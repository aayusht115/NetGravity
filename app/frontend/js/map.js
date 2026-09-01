/**
 * NetGravity — Leaflet India Map (Digital Twin Engine)
 * =====================================================
 * Central interactive 2D Digital Twin renderer supporting:
 *   - Digital Twin Page (Actual | Optimised Base | Recommended)
 *   - Scenario Planning Visual Context (Visualizing Digital Twin per Scenario)
 * Clicking a facility opens the facility details.
 */

/* global L */

import {
  PLANTS,
  DCS,
  MARKETS,
  LANES,
  SCENARIOS,
  getUtilColor,
  formatNumber,
  perPeriodLabel,
} from './data.js';
import { INDIA_BASEMAP_DATA_URI } from './basemap-data.js';
import { CONFIG } from './integration/config.js';

// ─── State ──────────────────────────────────────────────────
const maps = {}; // containerId → L.Map
const layerGroups = {}; // containerId → { nodes, flows }
let currentState = 'actual';

// ─── Basemap ────────────────────────────────────────────────
/**
 * The ground the network is drawn on.
 *
 * This used to be an unconditional tile request to
 * `https://{s}.basemaps.cartocdn.com/light_all/...` — a third-party service on
 * an anonymous quota. When that quota or a corporate network refuses, the
 * service does not return an error: it returns a perfectly valid PNG with
 * "API key required" printed across it. HTTP 200, decodes cleanly, no
 * `tileerror` for Leaflet to catch. The client's own facilities were then
 * plotted on top of a watermark announcing that their software was
 * misconfigured, and nothing in the application could tell.
 *
 * `INDIA_BASEMAP_DATA_URI` is an India basemap embedded in the application —
 * the same image the 3D twin already stands on, so the two views agree. It
 * needs no key, no quota and no internet connection, and it cannot change
 * underneath us.
 *
 * WHY AN IMAGE OVERLAY IS GEOMETRICALLY EXACT HERE, and not an approximation:
 * `L.ImageOverlay` stretches the image linearly between its two corners in the
 * map's own PROJECTED space, which for Leaflet's default CRS is Web Mercator.
 * The embedded image is itself a Web Mercator crop taken between exactly these
 * corners (see `twin3d.js`, which reprojects with the same constants). A
 * linear stretch between the projected corners of a Mercator crop reproduces
 * the crop, so every facility lands on the pixel its real coordinates belong
 * to. Using an equirectangular image here would visibly bow the coastline.
 */
const BASEMAP_BOUNDS = [[4.0, 65.0], [39.0, 100.0]];  // [[latMin,lngMin],[latMax,lngMax]]

//: Beyond this the embedded raster is being upscaled, so panning stops being
//: informative. Live tiles (see CONFIG.MAP_TILE_URL) lift the cap.
const BASEMAP_MAX_ZOOM = 8;

function addBaseLayer(map) {
  if (CONFIG.MAP_TILE_URL) {
    L.tileLayer(CONFIG.MAP_TILE_URL, {
      maxZoom: 18,
      attribution: CONFIG.MAP_TILE_ATTRIBUTION || '',
    }).addTo(map);
    return 'tiles';
  }
  L.imageOverlay(INDIA_BASEMAP_DATA_URI, BASEMAP_BOUNDS, {
    opacity: 1,
    interactive: false,
    // Behind every node and corridor.
    zIndex: 1,
  }).addTo(map);
  map.setMaxZoom(BASEMAP_MAX_ZOOM);
  return 'embedded';
}

// ─── Styles & Color Tokens ──────────────────────────────────
// Facility-type colors are kept visually distinct from the red/amber/green
// utilisation-risk palette (used for the DC ring border) so a color never
// carries two different meanings on the same map.
const COLORS = {
  plant: '#6B2FA0',
  dc: '#2563eb',
  market: '#0891b2',
  flow: {
    actual: '#94a3b8',
    optimised: '#6B2FA0',
    recommended: '#16a34a',
    scenario: '#6B2FA0',
    changed: '#16a34a',
  },
};

// ─── Public API: Init Map ───────────────────────────────────
export function initMap(containerId, options = {}) {
  const container = document.getElementById(containerId);
  if (!container) return null;

  if (maps[containerId]) {
    setTimeout(() => {
      maps[containerId].invalidateSize();
    }, 50);
    return maps[containerId];
  }

  const zoom = options.zoom || (options.isCompact ? 4.2 : 5);
  const center = options.center || [22.5, 79.5];
  const scrollWheelZoom = options.scrollWheelZoom !== undefined ? options.scrollWheelZoom : !options.isCompact;

  const map = L.map(containerId, {
    center: center,
    zoom: zoom,
    zoomControl: true,
    attributionControl: false,
    scrollWheelZoom: scrollWheelZoom,
  });

  addBaseLayer(map);

  maps[containerId] = map;
  layerGroups[containerId] = {
    nodes: L.layerGroup().addTo(map),
    flows: L.layerGroup().addTo(map),
  };

  // Draw the network immediately.
  // `initMap` used to create the map, add a legend and stop: nothing was
  // plotted until the user clicked one of the network-state toggle buttons,
  // which is the only thing wired to `setNetworkState`. Opening Digital Twin →
  // 2D therefore showed an empty basemap with a legend for a network that was
  // fully loaded.
  if (options.initialScenario) {
    renderScenarioDigitalTwin(containerId, options.initialScenario, options.mode || 'scenario');
  } else {
    renderNetwork(containerId, currentState);
    fitToNetwork(containerId);
  }

  // Add legend
  addLegend(map, options.isCompact);

  return map;
}

/**
 * Redraw every mounted map from the arrays as they stand now.
 *
 * Called when a network finishes loading. Without it a map created before the
 * upload kept the node set it was built with — and the one refresh path that
 * existed named containers ('home-map', 'twin-map') that are in no template,
 * so it silently did nothing for every network ever loaded.
 */
export function refreshAllMaps() {
  Object.keys(maps).forEach((id) => {
    if (id === 'scenario-leaflet-map') return;
    renderNetwork(id, currentState);
    fitToNetwork(id);
    try { maps[id].invalidateSize(); } catch (e) { /* not yet visible */ }
  });
}

/**
 * Frame the map on the network that is loaded, rather than on India.
 *
 * A fixed centre/zoom is right for the demo network and wrong for anyone
 * whose sites sit in one region. No-op when nothing has coordinates.
 */
export function fitToNetwork(containerId) {
  const map = maps[containerId];
  if (!map) return;
  const pts = [...PLANTS, ...DCS, ...MARKETS]
    .filter((n) => Number.isFinite(n.lat) && Number.isFinite(n.lng))
    .map((n) => [n.lat, n.lng]);
  if (pts.length < 2) return;
  try {
    map.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 7 });
  } catch (e) {
    /* Degenerate bounds — keep the default view. */
  }
}

// ─── Invalidate Map Size ────────────────────────────────────
export function invalidateMapSize(containerId) {
  if (containerId && maps[containerId]) {
    setTimeout(() => {
      try {
        maps[containerId].invalidateSize();
      } catch (e) {
        console.warn('Map resize error:', e);
      }
    }, 60);
  } else {
    Object.keys(maps).forEach((id) => {
      setTimeout(() => {
        try {
          maps[id].invalidateSize();
        } catch (e) {
          console.warn('Map resize error:', e);
        }
      }, 60);
    });
  }
}

// ─── Public API: Set Network State (Digital Twin Tab) ───────
export function setNetworkState(state) {
  currentState = state;
  Object.keys(maps).forEach((id) => {
    if (id !== 'scenario-leaflet-map') {
      renderNetwork(id, state);
    }
  });
}

// ─── Public API: Render Scenario Digital Twin ───────────────
export function renderScenarioDigitalTwin(containerId, scenarioId, mode = 'scenario') {
  if (!maps[containerId]) {
    initMap(containerId, { isCompact: containerId === 'scenario-leaflet-map' });
  }

  const lg = layerGroups[containerId];
  if (!lg) return;

  lg.nodes.clearLayers();
  lg.flows.clearLayers();

  const isBaseline = mode === 'baseline' || scenarioId === 'SCN_ACTUAL';
  const scn = SCENARIOS.find((s) => s.id === scenarioId);
  // With no solved scenarios, `scn` is undefined and reading `scn.id` threw.
  // Falling back to the baseline draws the network as it actually is, which is
  // the right thing to show when there is no scenario to overlay.
  const scnData = getScenarioNetworkData((isBaseline || !scn) ? 'SCN_ACTUAL' : scn.id);

  // Sites this scenario introduces. They are in no uploaded network, so they
  // are in neither PLANTS nor DCS — without this a scenario that opens a new
  // distribution centre drew every lane into it and never drew the centre.
  const newSites = (!isBaseline && scn && scn.newSites) ? scn.newSites : [];
  const extraCoords = new Map(
    newSites.filter((s) => Number.isFinite(s.lat) && Number.isFinite(s.lng))
      .map((s) => [s.id, [s.lat, s.lng]]));
  const extraNames = new Map(newSites.map((s) => [s.id, s.name]));

  const coordsOf = (id) => extraCoords.get(id) || getCoords(id);
  const nameOf = (id) => extraNames.get(id) || getFacilityName(id);

  // 1. Draw Flow Polylines
  scnData.flows.forEach((flow) => {
    const from = coordsOf(flow.from);
    const to = coordsOf(flow.to);
    if (!from || !to) return;
    // A lane the plan does not use is not drawn on a scenario map. Drawing
    // every lane at its baseline weight is how a scenario that empties a
    // corridor looks identical to one that does not.
    if (!isBaseline && !(flow.flow > 0)) return;

    const thickness = Math.max(1.5, Math.min(8, flow.flow / 1400));
    let color = flow.changed ? '#6B2FA0' : '#94a3b8';
    let opacity = flow.changed ? 0.9 : 0.35;
    let dashArray = flow.changed ? '6 4' : null;

    if (isBaseline) {
      color = '#94a3b8';
      opacity = 0.45;
      dashArray = null;
    }

    const line = L.polyline([from, to], {
      color: color,
      weight: thickness,
      opacity: opacity,
      dashArray: dashArray,
    });

    const deltaText = flow.deltaText ? `<br><strong style="color:var(--primary)">${flow.deltaText}</strong>` : '';

    line.bindTooltip(
      `
      <div style="font-size:12px;line-height:1.4">
        <strong>${nameOf(flow.from)} → ${nameOf(flow.to)}</strong><br>
        Flow: <strong>${formatNumber(flow.flow)}</strong> ${perPeriodLabel()}<br>
        Cost: ₹${flow.cost}/unit · ${flow.distance} km
        ${deltaText}
      </div>
    `,
      { sticky: true }
    );

    lg.flows.addLayer(line);
  });

  // 2. Draw Nodes (Plants, DCs, Markets)
  PLANTS.forEach((p) => {
    const marker = createNodeMarker(p, 'plant', containerId, scnData.facilityStats[p.id]);
    lg.nodes.addLayer(marker);
  });

  DCS.forEach((d) => {
    const override = scnData.facilityStats[d.id];
    const marker = createNodeMarker(d, 'dc', containerId, override);
    lg.nodes.addLayer(marker);
  });

  // Greenfield sites, drawn as the DCs (or plants) they are, marked as new.
  newSites.forEach((site) => {
    if (!Number.isFinite(site.lat) || !Number.isFinite(site.lng)) return;
    const state = scnData.facilityStats[site.id] || {};
    const node = {
      id: site.id, name: site.name, lat: site.lat, lng: site.lng,
      capacity: site.capacity, utilPct: state.utilPct ?? 0,
      throughput: state.throughput ?? 0,
    };
    const marker = createNodeMarker(
      node, site.role === 'PLANT' ? 'plant' : 'dc', containerId,
      { ...state, isNew: true },
    );
    lg.nodes.addLayer(marker);
  });

  MARKETS.forEach((m) => {
    const marker = createNodeMarker(m, 'market', containerId, null);
    lg.nodes.addLayer(marker);
  });
}

// ─── Scenario Network Data Resolver ─────────────────────────
/**
 * The facility states and lane flows to draw for one scenario.
 *
 * Reads the solved state the backend returned with the scenario
 * (`scenario_facilities` / `scenario_flows`), and marks a lane "changed" by
 * comparing its scenario volume against the same lane in the baseline.
 *
 * This function used to be a switch over five prototype scenario ids
 * (`SCN_REBALANCE`, `SCN_USER_1`, …), each branch assigning literal
 * utilisation and throughput to `DC_DELHI`, `DC_MUMBAI`, `DC_BENGALURU`,
 * `DC_KOLKATA` and `DC_GUWAHATI`, with hand-written flow overrides on
 * `PLT_BADDI → DC_DELHI` and a `deltaText` describing a rebalancing no solver
 * had performed. A user-created scenario matched none of those branches and
 * fell into an `else` that invented `DC_DELHI` at `delhiUtil || 88.0` and four
 * more facilities beside it — none of which exist in an uploaded network, so
 * the override map addressed nothing and every scenario rendered the baseline.
 *
 * Returns empty collections when the scenario carries no solved state, so the
 * map draws the network without overrides rather than inventing them.
 */
function getScenarioNetworkData(scenarioId) {
  const facilityStats = {};

  const isBaseline = scenarioId === 'SCN_ACTUAL' || scenarioId === 'actual';
  const scn = isBaseline ? null : SCENARIOS.find((s) => s.id === scenarioId);

  const solved = scn ? (scn.scenarioFacilities || {}) : {};
  const baseFacilities = scn ? (scn.baselineFacilities || {}) : {};
  const solvedFlows = scn ? (scn.scenarioFlows || []) : [];
  const baseFlows = scn ? (scn.baselineFlows || []) : [];

  Object.entries(solved).forEach(([facilityId, state]) => {
    if (!state) return;
    const before = baseFacilities[facilityId];
    // A note only where the solver actually moved something, and worded from
    // the two numbers rather than from a stored narrative.
    let note = '';
    if (state.isOpen === false) {
      note = 'Closed in this scenario';
    } else if (before && typeof before.utilPct === 'number'
               && typeof state.utilPct === 'number') {
      const shift = state.utilPct - before.utilPct;
      if (Math.abs(shift) >= 0.05) {
        note = `${shift > 0 ? '↑' : '↓'} ${Math.abs(shift).toFixed(1)} pp vs baseline`;
      }
    }
    facilityStats[facilityId] = {
      utilPct: state.utilPct,
      throughput: state.throughput,
      capacity: state.capacity,
      isOpen: state.isOpen,
      wasOpen: before ? before.isOpen : null,
      note,
    };
  });

  // Lane volumes, keyed the way the solver reports them.
  const laneKey = (from, to) => `${from}->${to}`;
  const baseByLane = new Map(
    baseFlows.map((f) => [laneKey(f.origin_id, f.destination_id), f]));
  const scnByLane = new Map(
    solvedFlows.map((f) => [laneKey(f.origin_id, f.destination_id), f]));
  const laneByKey = new Map(LANES.map((l) => [laneKey(l.from, l.to), l]));

  const rowFor = (from, to, lane) => {
    const key = laneKey(from, to);
    const after = scnByLane.get(key);
    const before = baseByLane.get(key);
    const row = lane
      ? { ...lane, fromName: getFacilityName(from), toName: getFacilityName(to), changed: false }
      : {
        // A corridor the scenario created — there is no uploaded lane behind
        // it, so its cost and distance come from the solved flow itself.
        from, to, cost: after ? +(after.transport_cost / Math.max(after.flow_units, 1)).toFixed(2) : 0,
        distance: after ? Math.round(after.distance_km) : 0,
        flow: 0, changed: false,
      };
    if (isBaseline || (!after && !before)) return row;

    const afterUnits = after ? after.flow_units : 0;
    const beforeUnits = before ? before.flow_units : 0;
    row.flow = afterUnits;
    const shift = afterUnits - beforeUnits;
    if (Math.abs(shift) >= 1) {
      row.changed = true;
      row.deltaText = beforeUnits === 0
        ? `New corridor: ${formatNumber(Math.round(afterUnits))} units`
        : `${shift > 0 ? '↑' : '↓'} ${formatNumber(Math.abs(Math.round(shift)))} units vs baseline`;
    }
    return row;
  };

  const flows = LANES.map((lane) => rowFor(lane.from, lane.to, lane));

  // Corridors the scenario opened that are in no uploaded lane list — every
  // route into and out of a greenfield site is one of these. Without them a
  // scenario that opens a new DC drew the site with nothing reaching it.
  scnByLane.forEach((flow, key) => {
    if (laneByKey.has(key)) return;
    if (!(flow.flow_units > 0)) return;
    flows.push(rowFor(flow.origin_id, flow.destination_id, null));
  });

  return { facilityStats, flows };
}

// ─── Render Network Standard (Digital Twin Tab) ─────────────
function renderNetwork(containerId, state) {
  const lg = layerGroups[containerId];
  if (!lg) return;

  lg.nodes.clearLayers();
  lg.flows.clearLayers();

  const flowData = getFlowsForState(state);

  // Draw flows
  flowData.forEach((flow) => {
    const from = getCoords(flow.from);
    const to = getCoords(flow.to);
    if (!from || !to) return;

    const thickness = Math.max(1, Math.min(8, flow.flow / 1500));
    const color = COLORS.flow[state] || COLORS.flow.actual;
    const opacity = state === 'actual' ? 0.35 : 0.6;

    const line = L.polyline([from, to], {
      color: color,
      weight: thickness,
      opacity: opacity,
      dashArray: state === 'recommended' ? '8 4' : null,
    });

    line.bindTooltip(
      `
      <div style="font-size:12px">
        <strong>${getFacilityName(flow.from)} → ${getFacilityName(flow.to)}</strong><br>
        Flow: ${formatNumber(flow.flow)} ${perPeriodLabel()}<br>
        Cost: ₹${flow.cost}/unit · ${flow.distance} km
      </div>
    `,
      { sticky: true }
    );

    lg.flows.addLayer(line);
  });

  // Draw nodes
  PLANTS.forEach((p) => {
    const marker = createNodeMarker(p, 'plant', containerId);
    lg.nodes.addLayer(marker);
  });

  DCS.forEach((d) => {
    // No per-id overrides. This branch reassigned utilisation and throughput
    // for two of the prototype's own DCs whenever the "recommended" state was
    // selected — figures no engine produced, shown on top of whatever network
    // was loaded. A recommended state now draws the same solved values as the
    // rest of the map until a scenario supplies its own.
    const marker = createNodeMarker(d, 'dc', containerId, null);
    lg.nodes.addLayer(marker);
  });

  MARKETS.forEach((m) => {
    const marker = createNodeMarker(m, 'market', containerId);
    lg.nodes.addLayer(marker);
  });
}

// ─── Flow Data per State (Digital Twin Tab) ─────────────────
function getFlowsForState(state) {
  // Every state draws the solved flows on `LANES`.
  //
  // "optimised" and "recommended" used to be manufactured here by scaling
  // three named prototype lanes — `PLT_BADDI → DC_DELHI` at 0.88, then 0.82,
  // `PLT_BADDI → DC_KOLKATA` at 1.25, then 1.45 — multipliers that came from
  // nobody's optimiser. On an uploaded network none of those ids match, so all
  // three toggles already showed identical corridors while claiming to show
  // three different plans.
  //
  // A genuine optimised or recommended plan for the CURRENT network is a
  // scenario: solve one and the scenario map draws its own flows, from
  // `getScenarioNetworkData()`. Until then these states have nothing of their
  // own to show, and `renderNetwork()`'s caller says so.
  return LANES;
}

/** True when `state` has a distinct solved plan behind it; only 'actual' does. */
export function stateHasOwnPlan(state) {
  return state === 'actual';
}

// ─── Create Node Marker ─────────────────────────────────────
function createNodeMarker(node, type, containerId, overrideStats = null) {
  const iconMap = { plant: '🏭', dc: '🏪', market: '📦' };
  const colorMap = { plant: COLORS.plant, dc: COLORS.dc, market: COLORS.market };
  const sizeMap = { plant: 16, dc: 14, market: 9 };
  const color = colorMap[type];
  const size = sizeMap[type];

  let utilPct = node.utilPct;
  let throughput = node.throughput;
  let note = '';

  if (overrideStats) {
    if (overrideStats.utilPct !== undefined) utilPct = overrideStats.utilPct;
    if (overrideStats.throughput !== undefined) throughput = overrideStats.throughput;
    if (overrideStats.note) note = overrideStats.note;
  }

  // A facility the scenario shuts, and one it introduces, are the two things a
  // reader most needs to see on a scenario map — and both were invisible: a
  // closed site kept its full ring and a note buried in a hover tooltip, and a
  // new site was not drawn at all. Closed is greyed and struck; new is ringed
  // in the accent colour with a badge.
  const isClosed = !!overrideStats && overrideStats.isOpen === false;
  const isNew = !!overrideStats && overrideStats.isNew === true;

  // Only DCs carry a colored ring — it encodes utilisation risk, which is
  // meaningful only for them. Plants and markets are plain filled icons so
  // the ring isn't misread as carrying the same risk signal it doesn't.
  let adjustedSize = size;
  const isDc = type === 'dc';
  let border = isDc ? `3px solid ${getUtilColor(utilPct)}` : 'none';
  if (isDc) {
    adjustedSize = Math.max(12, Math.min(22, 10 + ((utilPct || 0) / 100) * 14));
  }
  if (isClosed) border = '3px dashed #94a3b8';
  if (isNew) border = '3px solid #6B2FA0';

  const glyph = isClosed ? '⛔' : iconMap[type];
  const badge = isNew
    ? '<span style="position:absolute;top:-6px;right:-8px;background:#6B2FA0;color:#fff;'
      + 'font-size:8px;font-weight:800;padding:1px 4px;border-radius:6px;letter-spacing:.04em">NEW</span>'
    : '';

  const icon = L.divIcon({
    className: 'custom-marker',
    html: `<div style="
      position:relative;
      width:${adjustedSize * 2}px;height:${adjustedSize * 2}px;
      border-radius:50%;
      background:${isClosed ? '#94a3b81f' : `${color}22`};
      border:${border};
      opacity:${isClosed ? 0.6 : 1};
      display:flex;align-items:center;justify-content:center;
      font-size:${Math.max(11, adjustedSize - 3)}px;
      cursor:pointer;
      box-shadow: 0 1px 4px rgba(0,0,0,0.15);
      transition: transform .2s;
    " onmouseover="this.style.transform='scale(1.2)'" onmouseout="this.style.transform='scale(1)'">${glyph}${badge}</div>`,
    iconSize: [adjustedSize * 2, adjustedSize * 2],
    iconAnchor: [adjustedSize, adjustedSize],
  });

  const marker = L.marker([node.lat, node.lng], { icon });

  let tooltipContent = `<div style="font-size:12px;line-height:1.4"><strong>${node.name}</strong>`;
  if (isNew) tooltipContent += ' <span style="color:#6B2FA0;font-weight:700">(new in this scenario)</span>';
  if (isClosed) tooltipContent += ' <span style="color:#dc2626;font-weight:700">(closed in this scenario)</span>';
  tooltipContent += '<br>';
  if (type === 'plant') {
    tooltipContent += `Capacity: ${formatNumber(node.capacity)} u/d<br>Throughput: ${formatNumber(throughput)} u/d`;
  } else if (type === 'dc') {
    tooltipContent += `Utilisation: <strong style="color:${getUtilColor(utilPct)}">${utilPct === null || utilPct === undefined ? '—' : `${utilPct}%`}</strong><br>`;
    tooltipContent += `Capacity: ${formatNumber(node.capacity)} u/d<br>Throughput: ${formatNumber(throughput)} u/d`;
    if (note) {
      tooltipContent += `<div style="margin-top:3px;font-size:11px;color:var(--primary);font-weight:600">• ${note}</div>`;
    }
  } else {
    tooltipContent += `Demand: ${formatNumber(node.demand)} u/d<br>SLA: ${node.slaDays}d · ${node.priority}`;
  }
  tooltipContent += `</div>`;
  marker.bindTooltip(tooltipContent, { sticky: true });

  if (type !== 'market') {
    marker.on('click', () => {
      if (typeof window.openFacilityPanel === 'function') {
        window.openFacilityPanel(node.id);
      }
    });
  }

  return marker;
}

// ─── Get Coordinates ────────────────────────────────────────
function getCoords(id) {
  const all = [...PLANTS, ...DCS, ...MARKETS];
  const node = all.find((n) => n.id === id);
  return node ? [node.lat, node.lng] : null;
}

function getFacilityName(id) {
  const all = [...PLANTS, ...DCS, ...MARKETS];
  const node = all.find((n) => n.id === id);
  return node ? node.name : id;
}

// ─── Legend ──────────────────────────────────────────────────
// Simple icon chips that mirror the exact marker glyphs on the map, so
// the legend reads at a glance instead of requiring a color-to-meaning
// lookup. Facility-type icons and the utilisation-risk ring colors are
// shown as two clearly separate groups since they answer different
// questions (what is this node vs. how loaded is it).
function iconChip(bg, glyph) {
  return `<span style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;background:${bg}22;font-size:10px;margin-right:6px;flex-shrink:0">${glyph}</span>`;
}

function addLegend(map, isCompact = false) {
  const legend = L.control({ position: 'bottomright' });
  legend.onAdd = function () {
    const div = L.DomUtil.create('div');
    if (isCompact) {
      div.style.cssText =
        'background:rgba(255,255,255,0.94);padding:7px 10px;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,.1);font-size:10.5px;line-height:1.6;font-family:Inter,sans-serif;border:1px solid #cbd5e1';
      div.innerHTML = `
        <div style="display:flex;align-items:center">${iconChip(COLORS.plant, '🏭')}Plant</div>
        <div style="display:flex;align-items:center">${iconChip(COLORS.dc, '🏪')}Distribution Centre</div>
        <div style="display:flex;align-items:center">${iconChip(COLORS.market, '📦')}Market</div>
      `;
    } else {
      div.style.cssText =
        'background:white;padding:10px 14px;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.12);font-size:11.5px;line-height:1.5;font-family:Inter,sans-serif';
      div.innerHTML = `
        <div style="display:flex;align-items:center;margin-bottom:5px">${iconChip(COLORS.plant, '🏭')}Plant</div>
        <div style="display:flex;align-items:center;margin-bottom:5px">${iconChip(COLORS.dc, '🏪')}Distribution Centre</div>
        <div style="display:flex;align-items:center">${iconChip(COLORS.market, '📦')}Demand Market</div>
        <div style="margin-top:7px;padding-top:7px;border-top:1px solid #eee">
          <div style="font-weight:700;font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.03em;margin-bottom:4px">DC Ring = Utilisation</div>
          <div style="display:flex;align-items:center;margin-bottom:3px"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#dc2626;margin-right:7px"></span>Critical (&gt;95%)</div>
          <div style="display:flex;align-items:center;margin-bottom:3px"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#f59e0b;margin-right:7px"></span>Stress (85–95%)</div>
          <div style="display:flex;align-items:center"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#22c55e;margin-right:7px"></span>Healthy (&lt;85%)</div>
        </div>
      `;
    }
    return div;
  };
  legend.addTo(map);
}
