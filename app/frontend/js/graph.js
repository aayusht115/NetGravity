/**
 * NetGravity — Leaflet.js Interactive Map (India Digital Twin)
 * ============================================================
 * Real geographic coordinates for all 18 nodes on India map.
 * Animated flow arcs, custom markers, hover tooltips, legend.
 */

/* global L */

// ── Real India Geo-coordinates ─────────────────────────────
const GEO = {
    S1:  [19.08,  72.88],  // Mumbai Plant
    S2:  [23.03,  72.57],  // Ahmedabad Plant
    S3:  [17.38,  78.49],  // Hyderabad Plant
    T1:  [18.52,  73.86],  // Pune DC
    T2:  [21.14,  79.08],  // Nagpur DC
    T3:  [22.72,  75.86],  // Indore DC
    T4:  [23.26,  77.41],  // Bhopal DC
    T5:  [21.25,  81.63],  // Raipur DC
    D1:  [28.61,  77.21],  // New Delhi
    D2:  [22.57,  88.36],  // Kolkata
    D3:  [13.08,  80.27],  // Chennai
    D4:  [12.97,  77.59],  // Bangalore
    D5:  [26.91,  75.79],  // Jaipur
    D6:  [26.85,  80.95],  // Lucknow
    D7:  [25.59,  85.13],  // Patna
    D8:  [20.30,  85.82],  // Bhubaneswar
    D9:  [11.01,  76.97],  // Coimbatore
    D10: [21.17,  72.83],  // Surat
};

const NODE_STYLE = {
    source:      { color: "#6366f1", icon: "🏭", radius: 20, label: "Supply Plant"  },
    dc:          { color: "#f59e0b", icon: "🏪", radius: 16, label: "DC"            },
    destination: { color: "#22c55e", icon: "📦", radius: 12, label: "Demand Zone"  },
};

const UTIL_COLOR = {
    high:   "#ef4444",
    medium: "#f59e0b",
    low:    "#22c55e",
};

// ── State ───────────────────────────────────────────────────
const mapInstances = {};      // containerId → L.Map
const markerGroups = {};      // containerId → L.LayerGroup
const flowGroups   = {};      // containerId → L.LayerGroup
const cogGroups    = {};      // containerId → L.LayerGroup
let   _graphData   = null;
let   _lastFlows   = [];
let   _lastOpenDCs = null;

// ── Public API ──────────────────────────────────────────────

/**
 * Initialize a Leaflet map in a given container.
 * Safe to call multiple times — re-uses existing instance.
 */
export function initGraph(containerId) {
    if (mapInstances[containerId]) {
        mapInstances[containerId].invalidateSize();
        return;
    }

    const el = document.getElementById(containerId);
    if (!el) return;

    // Ensure container has height
    if (!el.style.height) el.style.height = "520px";

    const map = L.map(containerId, {
        center: [22.5, 80.0],
        zoom: 5,
        zoomControl: false,
        attributionControl: false,
        minZoom: 4,
        maxZoom: 14,
    });

    // CartoDB Dark Matter tiles — no API key required
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
        attribution: "© OpenStreetMap contributors © CartoDB",
        subdomains: "abcd",
        maxZoom: 19,
    }).addTo(map);

    // Attribution (small, bottom-right)
    L.control.attribution({ prefix: false, position: "bottomright" })
        .addAttribution('© <a href="https://www.openstreetmap.org/">OSM</a> © <a href="https://carto.com/">CARTO</a>')
        .addTo(map);

    // Zoom control top-right
    L.control.zoom({ position: "topright" }).addTo(map);

    // Layer groups (z-order: flows below nodes)
    cogGroups[containerId]    = L.layerGroup().addTo(map);
    flowGroups[containerId]   = L.layerGroup().addTo(map);
    markerGroups[containerId] = L.layerGroup().addTo(map);

    // Legend
    _addLegend(map);

    // Reset view button
    _addResetViewControl(map, containerId);

    mapInstances[containerId] = map;
}

/**
 * Render all nodes and flow arcs on every initialized map.
 * @param {Object} graphData  — { nodes, edges } from /api/data
 * @param {Array}  flows      — [{from, to, flow, total_cost, arc_utilization_pct}]
 * @param {Array}  openDCs    — active DC IDs, or null = all open
 */
export function renderGraph(graphData, flows = [], openDCs = null) {
    _graphData   = graphData;
    _lastFlows   = flows;
    _lastOpenDCs = openDCs;
    Object.keys(mapInstances).forEach(id => _renderOnMap(id, graphData, flows, openDCs));
}

/**
 * Render a CoG optimal location marker.
 */
export function renderCOGMarker(containerId, lat, lon, label = "Optimal DC") {
    const group = cogGroups[containerId];
    if (!group) return;
    group.clearLayers();

    const marker = L.circleMarker([lat, lon], {
        radius: 14,
        fillColor: "#a855f7",
        color: "#fff",
        weight: 2,
        fillOpacity: 0.9,
        className: "cog-pulse",
    }).addTo(group);

    marker.bindTooltip(`
        <div class='ng-tip'>
            <strong style="color:#a855f7">⭐ ${label}</strong><br>
            Lat: ${lat.toFixed(4)}, Lon: ${lon.toFixed(4)}<br>
            <em>Weiszfeld optimal location</em>
        </div>`, { permanent: false, sticky: true });
}

// ── Internal Render ─────────────────────────────────────────

function _renderOnMap(containerId, graphData, flows, openDCs) {
    const map        = mapInstances[containerId];
    const flowGroup  = flowGroups[containerId];
    const nodeGroup  = markerGroups[containerId];
    if (!map) return;

    flowGroup.clearLayers();
    nodeGroup.clearLayers();

    const nodes = graphData?.nodes || [];

    // Build a quick lookup: nodeId → node metadata
    const nodeMap = {};
    nodes.forEach(n => { nodeMap[n.id] = n; });

    // Max flow for arc width scaling
    const maxFlow = flows.length ? Math.max(...flows.map(f => f.flow || 0), 1) : 1;

    // ── 1. Draw Flow Arcs ────────────────────────────────────
    flows.forEach(f => {
        const srcCoord = GEO[f.from];
        const dstCoord = GEO[f.to];
        if (!srcCoord || !dstCoord) return;

        const util  = f.arc_utilization_pct ?? 0;
        const level = util > 75 ? "high" : util > 40 ? "medium" : "low";
        const color = UTIL_COLOR[level];
        const weight = 2 + (f.flow / maxFlow) * 7;

        // Compute curved arc using intermediate control point
        const curvePts = _bezierLatLngs(srcCoord, dstCoord, 0.25);

        const line = L.polyline(curvePts, {
            color,
            weight,
            opacity: 0.75,
            dashArray: "10 5",
            className: `flow-arc flow-arc-${level}`,
            lineCap: "round",
        }).addTo(flowGroup);

        // Arrow at destination
        const arrowPts = curvePts.slice(-3);
        L.polyline(arrowPts, {
            color,
            weight: weight + 1,
            opacity: 0.9,
        }).addTo(flowGroup);

        // Hover tooltip
        const srcNode = nodeMap[f.from];
        const dstNode = nodeMap[f.to];
        const costStr = (f.total_cost || 0).toLocaleString("en-IN", { maximumFractionDigits: 0 });
        line.bindTooltip(`
            <div class="ng-tip">
                <div class="ng-tip-title" style="color:${color}">
                    ${srcNode?.name || f.from} → ${dstNode?.name || f.to}
                </div>
                <div>Flow: <strong>${(f.flow || 0).toLocaleString()} units/day</strong></div>
                <div>Cost/unit: ₹ ${f.cost || 0}</div>
                <div>Arc cost: <strong>₹ ${costStr}/day</strong></div>
                ${util > 0 ? `<div>Utilization: <strong style="color:${color}">${util}%</strong></div>` : ""}
            </div>`, { sticky: true, className: "ng-leaflet-tooltip" });

        line.on("mouseover", function () { this.setStyle({ opacity: 1, weight: weight + 2 }); });
        line.on("mouseout",  function () { this.setStyle({ opacity: 0.75, weight }); });
    });

    // ── 2. Draw Node Markers ─────────────────────────────────
    nodes.forEach(node => {
        const coords = GEO[node.id];
        if (!coords) return;

        const type   = node.type;
        const style  = NODE_STYLE[type] || NODE_STYLE.destination;
        const isClosedDC = type === "dc" && openDCs && !openDCs.includes(node.id);

        const divIcon = L.divIcon({
            className: "",
            html: `
                <div class="ng-node ng-node-${type}${isClosedDC ? " ng-node-closed" : ""}"
                     style="--nc:${isClosedDC ? "#374151" : style.color};
                            width:${style.radius * 2}px;height:${style.radius * 2}px">
                    <span class="ng-node-icon">${isClosedDC ? "🔒" : style.icon}</span>
                    ${type === "dc" ? _utilizationRing(node.id, _lastFlows) : ""}
                </div>
                <div class="ng-node-label">${node.city}</div>`,
            iconSize:   [style.radius * 2, style.radius * 2],
            iconAnchor: [style.radius,     style.radius],
        });

        const marker = L.marker(coords, { icon: divIcon, zIndexOffset: type === "source" ? 300 : type === "dc" ? 200 : 100 })
            .addTo(nodeGroup);

        // Build tooltip content
        const tipContent = _buildNodeTooltip(node, type, isClosedDC, style.color, _lastFlows);
        marker.bindTooltip(tipContent, { sticky: true, className: "ng-leaflet-tooltip", offset: [0, -8] });

        // Click: zoom in on node
        marker.on("click", () => map.setView(coords, Math.max(map.getZoom(), 7), { animate: true }));
    });

    // ── 3. Fit India bounds ──────────────────────────────────
    if (nodes.length) {
        const bounds = L.latLngBounds(nodes.map(n => GEO[n.id]).filter(Boolean));
        map.fitBounds(bounds.pad(0.1), { maxZoom: 7 });
    }
}

// ── Tooltip HTML Builder ─────────────────────────────────────

function _buildNodeTooltip(node, type, isClosedDC, color, flows) {
    const tag = `<span style="background:${color}22;color:${color};
                 border:1px solid ${color};border-radius:4px;
                 padding:1px 6px;font-size:10px;font-weight:700">${node.id}</span>`;

    let body = "";
    if (type === "source") {
        body = `<div>Supply: <strong>${(node.supply || 0).toLocaleString()} u/day</strong></div>
                <div>Region: ${node.region}</div>`;
    } else if (type === "dc") {
        const util = _getDCUtil(node.id, flows);
        const uCol = util > 75 ? "#ef4444" : util > 40 ? "#f59e0b" : "#22c55e";
        body = `
            <div>Capacity: <strong>${(node.capacity || 0).toLocaleString()} u/day</strong></div>
            <div>Fixed cost: ₹${node.fixed_cost}L/yr</div>
            <div>Handling: ₹${node.handling_cost}/unit</div>
            ${util > 0 ? `<div>Utilization: <strong style="color:${uCol}">${util}%</strong></div>` : ""}
            <div>Status: <strong>${isClosedDC ? "🔒 CLOSED" : "✅ OPEN"}</strong></div>`;
    } else {
        body = `<div>Demand: <strong>${(node.demand || 0).toLocaleString()} u/day</strong></div>
                <div>Priority: ${node.priority}</div>
                <div>SLA: ${node.sla_days} days</div>`;
    }

    return `<div class="ng-tip">
                <div class="ng-tip-title">${tag} ${node.name}</div>
                <div style="color:#94a3b8;font-size:11px;margin:3px 0 6px">${node.city}, ${node.region}</div>
                ${body}
            </div>`;
}

function _getDCUtil(dcId, flows) {
    const outFlows = flows.filter(f => f.from === dcId);
    if (!outFlows.length) return 0;
    return outFlows[0].arc_utilization_pct ?? 0;
}

function _utilizationRing(dcId, flows) {
    const util = _getDCUtil(dcId, flows);
    if (!util) return "";
    const col = util > 75 ? "#ef4444" : util > 40 ? "#f59e0b" : "#22c55e";
    return `<div class="ng-util-ring" style="--util:${util};--uc:${col}"></div>`;
}

// ── Bezier Curve Helper ──────────────────────────────────────
/**
 * Generates intermediate lat/lng points to simulate a bezier arc.
 * Bows the path sideways to avoid overlap on bidirectional routes.
 */
function _bezierLatLngs(src, dst, bow = 0.2) {
    const nPts = 20;
    const [lat1, lon1] = src;
    const [lat2, lon2] = dst;

    // Midpoint + perpendicular offset
    const midLat = (lat1 + lat2) / 2;
    const midLon = (lon1 + lon2) / 2;
    const dLat   = lat2 - lat1;
    const dLon   = lon2 - lon1;
    const cLat   = midLat + bow * (-dLon);
    const cLon   = midLon + bow * ( dLat);

    const pts = [];
    for (let t = 0; t <= 1; t += 1 / nPts) {
        // Quadratic bezier: B(t) = (1-t)²P0 + 2(1-t)tP1 + t²P2
        const mt = 1 - t;
        const pLat = mt * mt * lat1 + 2 * mt * t * cLat + t * t * lat2;
        const pLon = mt * mt * lon1 + 2 * mt * t * cLon + t * t * lon2;
        pts.push([pLat, pLon]);
    }
    return pts;
}

// ── Legend Control ────────────────────────────────────────────
function _addLegend(map) {
    const legend = L.control({ position: "bottomleft" });
    legend.onAdd = () => {
        const div = L.DomUtil.create("div", "ng-map-legend");
        div.innerHTML = `
            <div class="legend-title">NETWORK</div>
            <div class="legend-row"><span class="legend-dot" style="background:#6366f1"></span> Supply Plant</div>
            <div class="legend-row"><span class="legend-dot" style="background:#f59e0b"></span> Distribution Center</div>
            <div class="legend-row"><span class="legend-dot" style="background:#22c55e"></span> Demand Zone</div>
            <div class="legend-title" style="margin-top:8px">FLOW UTIL.</div>
            <div class="legend-row"><span class="legend-line" style="background:#22c55e"></span> Low (&lt;40%)</div>
            <div class="legend-row"><span class="legend-line" style="background:#f59e0b"></span> Medium (40–75%)</div>
            <div class="legend-row"><span class="legend-line" style="background:#ef4444"></span> High (&gt;75%)</div>`;
        return div;
    };
    legend.addTo(map);
}

// ── Reset View Control ────────────────────────────────────────
function _addResetViewControl(map, containerId) {
    const ctrl = L.control({ position: "topright" });
    ctrl.onAdd = () => {
        const div = L.DomUtil.create("div", "ng-map-btn leaflet-bar");
        div.innerHTML = `<a title="Reset view" href="#">⊡</a>`;
        div.addEventListener("click", e => {
            e.preventDefault();
            const data = _graphData;
            if (data?.nodes) {
                const bounds = L.latLngBounds(data.nodes.map(n => GEO[n.id]).filter(Boolean));
                map.fitBounds(bounds.pad(0.1), { maxZoom: 7, animate: true });
            }
        });
        return div;
    };
    ctrl.addTo(map);
}

// ── Tab resize fix: call invalidateSize when tab becomes visible ──
export function invalidateMapSize(containerId) {
    const m = mapInstances[containerId];
    if (m) setTimeout(() => m.invalidateSize(), 50);
}
