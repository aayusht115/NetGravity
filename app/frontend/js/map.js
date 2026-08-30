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
} from './data.js';

// ─── State ──────────────────────────────────────────────────
const maps = {}; // containerId → L.Map
const layerGroups = {}; // containerId → { nodes, flows }
let currentState = 'actual';

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

  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd',
    maxZoom: 18,
  }).addTo(map);

  maps[containerId] = map;
  layerGroups[containerId] = {
    nodes: L.layerGroup().addTo(map),
    flows: L.layerGroup().addTo(map),
  };

  if (options.initialScenario) {
    renderScenarioDigitalTwin(containerId, options.initialScenario, options.mode || 'scenario');
  } else {
    renderNetwork(containerId, 'actual');
  }

  // Add legend
  addLegend(map, options.isCompact);

  return map;
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
  const scn = SCENARIOS.find((s) => s.id === scenarioId) || SCENARIOS[1];
  const scnData = getScenarioNetworkData(isBaseline ? 'SCN_ACTUAL' : scn.id);

  // 1. Draw Flow Polylines
  scnData.flows.forEach((flow) => {
    const from = getCoords(flow.from);
    const to = getCoords(flow.to);
    if (!from || !to) return;

    const thickness = Math.max(1.5, Math.min(8, flow.flow / 1400));
    let color = flow.changed ? '#6B2FA0' : '#94a3b8';
    let opacity = flow.changed ? 0.9 : 0.4;
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
        <strong>${flow.fromName || flow.from} → ${flow.toName || flow.to}</strong><br>
        Flow: <strong>${formatNumber(flow.flow)}</strong> units/day<br>
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

  MARKETS.forEach((m) => {
    const marker = createNodeMarker(m, 'market', containerId, null);
    lg.nodes.addLayer(marker);
  });
}

// ─── Scenario Network Data Resolver ─────────────────────────
function getScenarioNetworkData(scenarioId) {
  const facilityStats = {};
  let flows = [];

  if (scenarioId === 'SCN_ACTUAL' || scenarioId === 'actual') {
    // Current Baseline / Actual State
    facilityStats['DC_DELHI'] = { utilPct: 94.0, throughput: 9400, note: 'Critical Capacity Risk (Dec Peak 108%)' };
    facilityStats['DC_MUMBAI'] = { utilPct: 75.6, throughput: 6800 };
    facilityStats['DC_BENGALURU'] = { utilPct: 74.7, throughput: 5600 };
    facilityStats['DC_KOLKATA'] = { utilPct: 53.3, throughput: 3200 };
    facilityStats['DC_GUWAHATI'] = { utilPct: 52.5, throughput: 2100 };

    flows = LANES.map((l) => ({
      ...l,
      fromName: getFacilityName(l.from),
      toName: getFacilityName(l.to),
      changed: false,
    }));
  } else if (scenarioId === 'SCN_REBALANCE' || scenarioId === 'recommended') {
    // Scenario 1: Rebalance Baddi to Delhi NCR & Kolkata (Recommended)
    facilityStats['DC_DELHI'] = { utilPct: 91.0, throughput: 8200, note: 'Bottleneck Relieved (-12% volume)' };
    facilityStats['DC_MUMBAI'] = { utilPct: 74.2, throughput: 6680 };
    facilityStats['DC_BENGALURU'] = { utilPct: 74.7, throughput: 5600 };
    facilityStats['DC_KOLKATA'] = { utilPct: 64.0, throughput: 3840, note: 'Spare Capacity Absorbed (+12% volume)' };
    facilityStats['DC_GUWAHATI'] = { utilPct: 52.5, throughput: 2100 };

    flows = LANES.map((l) => {
      const modified = { ...l, fromName: getFacilityName(l.from), toName: getFacilityName(l.to), changed: false };
      if (l.from === 'PLT_BADDI' && l.to === 'DC_DELHI') {
        modified.flow = 7000;
        modified.changed = true;
        modified.deltaText = '↓ 1,200 u/d (Rebalanced to Relieve Bottleneck)';
      }
      if (l.from === 'PLT_BADDI' && l.to === 'DC_KOLKATA') {
        modified.flow = 2000;
        modified.changed = true;
        modified.deltaText = '↑ 800 u/d (Absorbing Spare East Capacity)';
      }
      if (l.from === 'PLT_PUNE' && l.to === 'DC_MUMBAI') {
        modified.flow = 5400;
        modified.changed = true;
        modified.deltaText = '↑ 400 u/d (Direct Regional Routing)';
      }
      return modified;
    });
  } else if (scenarioId === 'SCN_USER_1') {
    // Scenario 2: User Created 1 (Western Corridor Expansion)
    facilityStats['DC_DELHI'] = { utilPct: 94.0, throughput: 9400, note: 'Delhi remains near capacity' };
    facilityStats['DC_MUMBAI'] = { utilPct: 82.0, throughput: 7380, note: 'Western Corridor expansion' };
    facilityStats['DC_BENGALURU'] = { utilPct: 75.0, throughput: 5625 };
    facilityStats['DC_KOLKATA'] = { utilPct: 53.3, throughput: 3200 };
    facilityStats['DC_GUWAHATI'] = { utilPct: 52.5, throughput: 2100 };

    flows = LANES.map((l) => {
      const modified = { ...l, fromName: getFacilityName(l.from), toName: getFacilityName(l.to), changed: false };
      if (l.from === 'PLT_PUNE' && l.to === 'DC_MUMBAI') {
        modified.flow = 5800;
        modified.changed = true;
        modified.deltaText = '↑ 800 u/d (Western Lane Optimization)';
      }
      if (l.from === 'PLT_BADDI' && l.to === 'DC_MUMBAI') {
        modified.flow = 2300;
        modified.changed = true;
        modified.deltaText = '↑ 500 u/d (Freight Route Shift)';
      }
      return modified;
    });
  } else if (scenarioId === 'SCN_USER_2') {
    // Scenario 3: User Created 2 (Eastern Expansion + Automated Dispatch)
    facilityStats['DC_DELHI'] = { utilPct: 92.0, throughput: 9200 };
    facilityStats['DC_MUMBAI'] = { utilPct: 75.6, throughput: 6800 };
    facilityStats['DC_BENGALURU'] = { utilPct: 74.7, throughput: 5600 };
    facilityStats['DC_KOLKATA'] = { utilPct: 68.0, throughput: 4080, note: 'Kolkata DC Cross-dock Active' };
    facilityStats['DC_GUWAHATI'] = { utilPct: 58.0, throughput: 2320, note: 'Northeast replenishment enhanced' };

    flows = LANES.map((l) => {
      const modified = { ...l, fromName: getFacilityName(l.from), toName: getFacilityName(l.to), changed: false };
      if (l.from === 'PLT_BADDI' && l.to === 'DC_KOLKATA') {
        modified.flow = 2200;
        modified.changed = true;
        modified.deltaText = '↑ 1,000 u/d (Eastern Cross-Docking)';
      }
      if (l.from === 'PLT_KOLKATA' && l.to === 'DC_GUWAHATI') {
        modified.flow = 2100;
        modified.changed = true;
        modified.deltaText = '↑ 600 u/d (Automated 10-min Dispatch)';
      }
      return modified;
    });
  } else if (scenarioId === 'SCN_AI_REC_4') {
    // Scenario 4: AI Recommended 4 (Intermodal Rail Corridors)
    facilityStats['DC_DELHI'] = { utilPct: 89.0, throughput: 8900, note: 'Rail Corridor relieves road traffic' };
    facilityStats['DC_MUMBAI'] = { utilPct: 71.0, throughput: 6390 };
    facilityStats['DC_BENGALURU'] = { utilPct: 74.0, throughput: 5550 };
    facilityStats['DC_KOLKATA'] = { utilPct: 60.0, throughput: 3600 };
    facilityStats['DC_GUWAHATI'] = { utilPct: 52.5, throughput: 2100 };

    flows = LANES.map((l) => {
      const modified = { ...l, fromName: getFacilityName(l.from), toName: getFacilityName(l.to), changed: false };
      if (l.from === 'PLT_BADDI' && l.to === 'DC_DELHI') {
        modified.flow = 6800;
        modified.changed = true;
        modified.deltaText = 'Intermodal Rail Transit (↓1,400 u/d)';
      }
      if (l.from === 'PLT_HYDERABAD' && l.to === 'DC_KOLKATA') {
        modified.flow = 1800;
        modified.changed = true;
        modified.deltaText = 'Rail Freight Corridor (↑500 u/d)';
      }
      return modified;
    });
  } else {
    // Custom Generated Scenario
    const scn = SCENARIOS.find((s) => s.id === scenarioId) || {};
    const dUtil = scn.delhiUtil || 88.0;
    facilityStats['DC_DELHI'] = { utilPct: dUtil, throughput: Math.round(10000 * (dUtil / 100)), note: 'Custom Solver Allocation' };
    facilityStats['DC_MUMBAI'] = { utilPct: 75.0, throughput: 6750 };
    facilityStats['DC_BENGALURU'] = { utilPct: 74.0, throughput: 5550 };
    facilityStats['DC_KOLKATA'] = { utilPct: 62.0, throughput: 3720 };
    facilityStats['DC_GUWAHATI'] = { utilPct: 52.5, throughput: 2100 };

    flows = LANES.map((l) => {
      const modified = { ...l, fromName: getFacilityName(l.from), toName: getFacilityName(l.to), changed: false };
      if (l.from === 'PLT_BADDI' && l.to === 'DC_DELHI') {
        modified.flow = Math.round(l.flow * (dUtil / 94.0));
        modified.changed = true;
        modified.deltaText = 'Custom Parameterized Rebalancing';
      }
      return modified;
    });
  }

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
        Flow: ${formatNumber(flow.flow)} units/day<br>
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
    let override = null;
    if (state === 'recommended' && d.id === 'DC_DELHI') {
      override = { utilPct: 91.0, throughput: 8200 };
    }
    if (state === 'recommended' && d.id === 'DC_KOLKATA') {
      override = { utilPct: 64.0, throughput: 3840 };
    }
    const marker = createNodeMarker(d, 'dc', containerId, override);
    lg.nodes.addLayer(marker);
  });

  MARKETS.forEach((m) => {
    const marker = createNodeMarker(m, 'market', containerId);
    lg.nodes.addLayer(marker);
  });
}

// ─── Flow Data per State (Digital Twin Tab) ─────────────────
function getFlowsForState(state) {
  if (state === 'actual') return LANES;

  if (state === 'optimised') {
    return LANES.map((l) => {
      const modified = { ...l };
      if (l.from === 'PLT_BADDI' && l.to === 'DC_DELHI') modified.flow = Math.round(l.flow * 0.88);
      if (l.from === 'PLT_BADDI' && l.to === 'DC_KOLKATA') modified.flow = Math.round(l.flow * 1.25);
      if (l.from === 'PLT_KOLKATA' && l.to === 'DC_KOLKATA') modified.flow = Math.round(l.flow * 1.15);
      return modified;
    });
  }

  if (state === 'recommended') {
    return LANES.map((l) => {
      const modified = { ...l };
      if (l.from === 'PLT_BADDI' && l.to === 'DC_DELHI') modified.flow = Math.round(l.flow * 0.82);
      if (l.from === 'PLT_BADDI' && l.to === 'DC_KOLKATA') modified.flow = Math.round(l.flow * 1.45);
      if (l.from === 'PLT_KOLKATA' && l.to === 'DC_KOLKATA') modified.flow = Math.round(l.flow * 1.3);
      if (l.from === 'DC_KOLKATA' && l.to === 'MKT_KOLKATA') modified.flow = Math.round(l.flow * 1.2);
      return modified;
    });
  }

  return LANES;
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

  // Only DCs carry a colored ring — it encodes utilisation risk, which is
  // meaningful only for them. Plants and markets are plain filled icons so
  // the ring isn't misread as carrying the same risk signal it doesn't.
  let adjustedSize = size;
  const isDc = type === 'dc';
  const border = isDc ? `3px solid ${getUtilColor(utilPct)}` : 'none';
  if (isDc) {
    adjustedSize = Math.max(12, Math.min(22, 10 + (utilPct / 100) * 14));
  }

  const icon = L.divIcon({
    className: 'custom-marker',
    html: `<div style="
      width:${adjustedSize * 2}px;height:${adjustedSize * 2}px;
      border-radius:50%;
      background:${color}22;
      border:${border};
      display:flex;align-items:center;justify-content:center;
      font-size:${Math.max(11, adjustedSize - 3)}px;
      cursor:pointer;
      box-shadow: 0 1px 4px rgba(0,0,0,0.15);
      transition: transform .2s;
    " onmouseover="this.style.transform='scale(1.2)'" onmouseout="this.style.transform='scale(1)'">${iconMap[type]}</div>`,
    iconSize: [adjustedSize * 2, adjustedSize * 2],
    iconAnchor: [adjustedSize, adjustedSize],
  });

  const marker = L.marker([node.lat, node.lng], { icon });

  let tooltipContent = `<div style="font-size:12px;line-height:1.4"><strong>${node.name}</strong><br>`;
  if (type === 'plant') {
    tooltipContent += `Capacity: ${formatNumber(node.capacity)} u/d<br>Throughput: ${formatNumber(throughput)} u/d`;
  } else if (type === 'dc') {
    tooltipContent += `Utilisation: <strong style="color:${getUtilColor(utilPct)}">${utilPct}%</strong><br>`;
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
