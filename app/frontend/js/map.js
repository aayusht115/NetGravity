/**
 * NetGravity — Leaflet India Map (Digital Twin)
 * ===============================================
 * Interactive map with three network states:
 *   Actual | Optimised Base | Recommended
 * Clicking a facility opens the detail panel.
 */

/* global L */

import { PLANTS, DCS, MARKETS, LANES, SCENARIOS,
         getUtilColor, formatNumber } from './data.js';

// ─── State ──────────────────────────────────────────────────
const maps = {};           // containerId → L.Map
const layerGroups = {};    // containerId → { nodes, flows }
let currentState = 'actual';

// ─── Styles ─────────────────────────────────────────────────
const COLORS = {
  plant:  '#6B2FA0',
  dc:     '#f59e0b',
  market: '#22c55e',
  flow:   { actual: '#94a3b8', optimised: '#6B2FA0', recommended: '#16a34a' },
};

// ─── Public API ─────────────────────────────────────────────
export function initMap(containerId) {
  if (maps[containerId]) {
    maps[containerId].invalidateSize();
    return;
  }

  const map = L.map(containerId, {
    center: [22.5, 80.0],
    zoom: 5,
    zoomControl: true,
    scrollWheelZoom: true,
  });

  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://carto.com/">CARTO</a>',
    maxZoom: 18,
  }).addTo(map);

  maps[containerId] = map;
  layerGroups[containerId] = {
    nodes: L.layerGroup().addTo(map),
    flows: L.layerGroup().addTo(map),
  };

  renderNetwork(containerId, 'actual');

  // Add legend
  addLegend(map);
}

export function setNetworkState(state) {
  currentState = state;
  Object.keys(maps).forEach(id => renderNetwork(id, state));
}

// ─── Render Network ─────────────────────────────────────────
function renderNetwork(containerId, state) {
  const lg = layerGroups[containerId];
  if (!lg) return;

  lg.nodes.clearLayers();
  lg.flows.clearLayers();

  // Get flow data for this state
  const flowData = getFlowsForState(state);

  // Draw flows first (below nodes)
  flowData.forEach(flow => {
    const from = getCoords(flow.from);
    const to = getCoords(flow.to);
    if (!from || !to) return;

    const thickness = Math.max(1, Math.min(8, flow.flow / 1500));
    const color = COLORS.flow[state] || COLORS.flow.actual;
    const opacity = state === 'actual' ? 0.3 : 0.5;

    const line = L.polyline([from, to], {
      color: color,
      weight: thickness,
      opacity: opacity,
      dashArray: state === 'recommended' ? '8 4' : null,
    });

    line.bindTooltip(`
      <strong>${flow.from} → ${flow.to}</strong><br>
      Flow: ${formatNumber(flow.flow)} units/day<br>
      Cost: ₹${flow.cost}/unit · ${flow.distance} km
    `, { sticky: true });

    lg.flows.addLayer(line);
  });

  // Draw nodes
  PLANTS.forEach(p => {
    const marker = createNodeMarker(p, 'plant', containerId);
    lg.nodes.addLayer(marker);
  });

  DCS.forEach(d => {
    const marker = createNodeMarker(d, 'dc', containerId);
    lg.nodes.addLayer(marker);
  });

  MARKETS.forEach(m => {
    const marker = createNodeMarker(m, 'market', containerId);
    lg.nodes.addLayer(marker);
  });
}

// ─── Flow Data per State ────────────────────────────────────
function getFlowsForState(state) {
  if (state === 'actual') return LANES;

  // Optimised base: same topology, adjusted flows
  if (state === 'optimised') {
    return LANES.map(l => {
      const modified = { ...l };
      // Simulate optimised rebalancing
      if (l.from === 'PLT_BADDI' && l.to === 'DC_DELHI') modified.flow = Math.round(l.flow * 0.88);
      if (l.from === 'PLT_BADDI' && l.to === 'DC_KOLKATA') modified.flow = Math.round(l.flow * 1.25);
      if (l.from === 'PLT_KOLKATA' && l.to === 'DC_KOLKATA') modified.flow = Math.round(l.flow * 1.15);
      return modified;
    });
  }

  // Recommended: flow rebalancing scenario
  if (state === 'recommended') {
    return LANES.map(l => {
      const modified = { ...l };
      if (l.from === 'PLT_BADDI' && l.to === 'DC_DELHI') modified.flow = Math.round(l.flow * 0.82);
      if (l.from === 'PLT_BADDI' && l.to === 'DC_KOLKATA') modified.flow = Math.round(l.flow * 1.45);
      if (l.from === 'PLT_KOLKATA' && l.to === 'DC_KOLKATA') modified.flow = Math.round(l.flow * 1.30);
      if (l.from === 'DC_KOLKATA' && l.to === 'MKT_KOLKATA') modified.flow = Math.round(l.flow * 1.20);
      return modified;
    });
  }

  return LANES;
}

// ─── Create Node Marker ─────────────────────────────────────
function createNodeMarker(node, type, containerId) {
  const iconMap = { plant: '🏭', dc: '🏪', market: '📦' };
  const colorMap = { plant: COLORS.plant, dc: COLORS.dc, market: COLORS.market };
  const sizeMap = { plant: 18, dc: 15, market: 10 };
  const color = colorMap[type];
  const size = sizeMap[type];

  // For DCs, adjust size by utilisation
  let adjustedSize = size;
  let borderColor = color;
  if (type === 'dc') {
    adjustedSize = Math.max(12, Math.min(22, 10 + (node.utilPct / 100) * 14));
    borderColor = getUtilColor(node.utilPct);
  }

  const icon = L.divIcon({
    className: 'custom-marker',
    html: `<div style="
      width:${adjustedSize * 2}px;height:${adjustedSize * 2}px;
      border-radius:50%;
      background:${color}22;
      border:3px solid ${borderColor};
      display:flex;align-items:center;justify-content:center;
      font-size:${Math.max(12, adjustedSize - 2)}px;
      cursor:pointer;
      transition: transform .2s;
    " onmouseover="this.style.transform='scale(1.2)'" onmouseout="this.style.transform='scale(1)'">${iconMap[type]}</div>`,
    iconSize: [adjustedSize * 2, adjustedSize * 2],
    iconAnchor: [adjustedSize, adjustedSize],
  });

  const marker = L.marker([node.lat, node.lng], { icon });

  // Tooltip
  let tooltipContent = `<strong>${node.name}</strong><br>`;
  if (type === 'plant') {
    tooltipContent += `Capacity: ${formatNumber(node.capacity)} u/d<br>Throughput: ${formatNumber(node.throughput)} u/d`;
  } else if (type === 'dc') {
    tooltipContent += `Utilisation: <strong style="color:${getUtilColor(node.utilPct)}">${node.utilPct}%</strong><br>`;
    tooltipContent += `Capacity: ${formatNumber(node.capacity)} u/d<br>Throughput: ${formatNumber(node.throughput)} u/d`;
  } else {
    tooltipContent += `Demand: ${formatNumber(node.demand)} u/d<br>SLA: ${node.slaDays}d · ${node.priority}`;
  }
  marker.bindTooltip(tooltipContent);

  // Click to open facility panel
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
  const node = all.find(n => n.id === id);
  return node ? [node.lat, node.lng] : null;
}

// ─── Legend ──────────────────────────────────────────────────
function addLegend(map) {
  const legend = L.control({ position: 'bottomright' });
  legend.onAdd = function() {
    const div = L.DomUtil.create('div');
    div.style.cssText = 'background:white;padding:10px 14px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.12);font-size:11px;line-height:1.8;font-family:Inter,sans-serif';
    div.innerHTML = `
      <div style="font-weight:700;margin-bottom:4px">Network Legend</div>
      <div><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:${COLORS.plant};vertical-align:middle;margin-right:6px"></span>Plant</div>
      <div><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:${COLORS.dc};vertical-align:middle;margin-right:6px"></span>Distribution Centre</div>
      <div><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:${COLORS.market};vertical-align:middle;margin-right:6px"></span>Demand Market</div>
      <div style="margin-top:4px;border-top:1px solid #eee;padding-top:4px">
        <div><span style="display:inline-block;width:20px;height:2px;background:#dc2626;vertical-align:middle;margin-right:6px"></span>Critical (&gt;90%)</div>
        <div><span style="display:inline-block;width:20px;height:2px;background:#f59e0b;vertical-align:middle;margin-right:6px"></span>Moderate (75-90%)</div>
        <div><span style="display:inline-block;width:20px;height:2px;background:#22c55e;vertical-align:middle;margin-right:6px"></span>Healthy (&lt;75%)</div>
      </div>
    `;
    return div;
  };
  legend.addTo(map);
}
