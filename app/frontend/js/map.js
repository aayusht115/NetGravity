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
  formatCurrencyExact,
  NETWORK_GEOGRAPHY,
} from './data.js';
import { WORLD_COUNTRIES, countriesContaining, networkWindow,
         loadAdmin1 } from './world-basemap.js';
import { CONFIG } from './integration/config.js';
// One definition of what a corridor's thickness means, shared with the 3D
// twin and with the legend that describes it — see the module header.
import { flowBands, bandForFlow, twinLegendHtml, facilityLabel,
         facilityShortLabel, glyphSize, NODE_STYLE,
         scenarioLegendHtml } from './twin-legend.js';

// ─── State ──────────────────────────────────────────────────
const maps = {}; // containerId → L.Map
//: containerId → the base layer(s) and notice currently mounted, so the
//: basemap can be re-chosen when a network loads after the map was built.
const baseLayers = {};
const layerGroups = {}; // containerId → { nodes, flows }
const mapOptions = {};  // containerId → { isCompact, showLabels }


// ─── Basemap ────────────────────────────────────────────────
/**
 * The ground the network is drawn on.
 *
 * Two things were wrong with what this replaces.
 *
 * It began as an unconditional tile request to
 * `https://{s}.basemaps.cartocdn.com/light_all/...` — a third-party service on
 * an anonymous quota. When that quota or a corporate network refuses, the
 * service does not return an error: it returns a perfectly valid PNG with
 * "API key required" printed across it. HTTP 200, decodes cleanly, no
 * `tileerror` for Leaflet to catch. The client's own facilities were then
 * plotted on top of a watermark announcing that their software was
 * misconfigured, and nothing in the application could tell.
 *
 * The fix for that was `INDIA_BASEMAP_DATA_URI`, a raster photograph of India
 * embedded in the application. It needed no key and no internet — but it is a
 * photograph of ONE COUNTRY, cropped to 4-39N / 65-100E. A network anywhere
 * else got a bare 10-degree graticule: a US network's twelve facilities on an
 * empty grid, correct coordinates, no land.
 *
 * Now the land is vector. `WORLD_COUNTRIES` is Natural Earth 110m — 177
 * countries, public domain, bundled, ~180 KB — drawn as GeoJSON polygons by
 * Leaflet's own renderer. It needs no key, no quota and no connection; it
 * covers every network anywhere; it stays sharp at any zoom instead of
 * pixelating past zoom 8; and, because the 3D twin builds its ground plane
 * from the SAME rings, the two views cannot drift apart.
 */

/** Land, borders and water — a quiet ground that never competes with the network. */
const BASEMAP_STYLE = {
  // Internal borders are a hair lighter than national ones, so the hierarchy
  // reads without a legend: coast, then country, then state.
  subdivision: '#b9c8db',
  subdivisionActive: '#91a6c0',
  // Land lighter than water, the way a printed atlas reads. The first pass had
  // land #eef2f7 on water #f8fafc — a 4% luminance difference, so the
  // coastline was technically drawn and effectively invisible.
  water: '#cddced',
  land: '#f6f9fc',
  landActive: '#ffffff',     // countries this network actually has sites in
  border: '#a9b8cb',
  borderActive: '#7f93ad',
  graticule: '#dfe8f2',
};

/**
 * Countries the loaded network has sites in.
 *
 * Used to lift those few out of the rest of the world, so a US network reads
 * as "the United States, with context around it" rather than as an undammed
 * world map. Recomputed only when the network changes.
 */
function activeCountryNames() {
  const pts = [...PLANTS, ...DCS, ...MARKETS]
    .filter((n) => Number.isFinite(n.lat) && Number.isFinite(n.lng));
  if (!pts.length) return new Set();
  return new Set(countriesContaining(pts).map((c) => c.name));
}

/**
 * Meridians and parallels, under the land.
 *
 * Kept from the old graticule-only fallback, because it does something the
 * coastlines do not: it gives distance a scale. It is quiet enough to read as
 * a grid rather than as data.
 */
function addGraticule(map, step = 10) {
  const layer = L.layerGroup();
  const style = { color: BASEMAP_STYLE.graticule, weight: 1, opacity: 0.9,
                  interactive: false };
  for (let lat = -80; lat <= 80; lat += step) {
    L.polyline([[lat, -180], [lat, 180]], style).addTo(layer);
  }
  for (let lng = -180; lng <= 180; lng += step) {
    L.polyline([[-85, lng], [85, lng]], style).addTo(layer);
  }
  return layer;
}

/**
 * The country layer.
 *
 * `L.geoJSON` reads [lng, lat] pairs, which is GeoJSON order and the reverse
 * of Leaflet's own [lat, lng] — it does the swap itself, so the rings go in
 * exactly as `world-basemap.js` stores them and as the 3D twin reads them.
 */
function addCountries(map) {
  const active = activeCountryNames();
  return L.geoJSON(WORLD_COUNTRIES, {
    // Behind every node and corridor, and never in the way of a click.
    interactive: false,
    pane: 'tilePane',
    style: (feature) => {
      const isActive = active.has(feature.properties.name);
      return {
        fillColor: isActive ? BASEMAP_STYLE.landActive : BASEMAP_STYLE.land,
        fillOpacity: 1,
        color: isActive ? BASEMAP_STYLE.borderActive : BASEMAP_STYLE.border,
        weight: isActive ? 0.9 : 0.6,
        interactive: false,
      };
    },
  });
}

/**
 * States, provinces and prefectures, drawn inside the countries.
 *
 * Borders only — no fill. The country layer beneath already paints the land,
 * and filling each subdivision again would double the geometry for no visual
 * gain and put a seam on every shared edge.
 *
 * Added asynchronously: the module is 1.6 MB and the map is useful without
 * it. The countries are on screen from the first frame; the internal borders
 * arrive a moment later.
 */
function addSubdivisions(map, containerId) {
  loadAdmin1().then((admin1) => {
    if (!admin1) return;
    // The map may have been torn down while this was in flight.
    if (maps[containerId] !== map) return;
    const active = activeCountryNames();
    const layer = L.geoJSON(admin1.collection, {
      interactive: false,
      pane: 'tilePane',
      style: (feature) => {
        const isActive = active.has(feature.properties.admin);
        return {
          fill: false,
          color: isActive ? BASEMAP_STYLE.subdivisionActive : BASEMAP_STYLE.subdivision,
          weight: isActive ? 0.9 : 0.5,
          opacity: isActive ? 1 : 0.55,
          interactive: false,
        };
      },
    }).addTo(map);
    // Registered so a basemap rebuild removes it with everything else.
    const record = baseLayers[containerId];
    if (record) record.layers.push(layer);
    // NOT bringToBack(). The country layer is in the same pane and paints an
    // OPAQUE fill, so sending the borders behind it hid every one of them —
    // 9,402 paths in the DOM and a blank map. Added after the countries, they
    // draw on top of the fills; nodes and corridors live in `overlayPane`,
    // which Leaflet stacks above `tilePane` regardless, so they still win.
  });
}

function addBaseLayer(map, containerId) {
  // A configured tile service still wins: it is photographic, and someone who
  // has set one has decided they want it.
  if (CONFIG.MAP_TILE_URL) {
    const tiles = L.tileLayer(CONFIG.MAP_TILE_URL, {
      maxZoom: 18,
      attribution: CONFIG.MAP_TILE_ATTRIBUTION || '',
    }).addTo(map);
    return { kind: 'tiles', layers: [tiles], control: null };
  }

  map.getContainer().style.background = BASEMAP_STYLE.water;
  const grat = addGraticule(map).addTo(map);
  const land = addCountries(map).addTo(map);
  const record = { kind: 'vector', layers: [grat, land], control: null };
  baseLayers[containerId] = record;
  addSubdivisions(map, containerId);
  return record;
}

// ─── Styles & Color Tokens ──────────────────────────────────
// Facility-type colors are kept visually distinct from the red/amber/green
// utilisation-risk palette (used for the DC ring border) so a color never
// carries two different meanings on the same map.
const COLORS = {
  // From the one place the legend reads them too, so a chip and the marker it
  // keys are the same colour. They were two independent literals and had
  // drifted apart.
  plant: NODE_STYLE.plant.color,
  dc: NODE_STYLE.dc.color,
  market: NODE_STYLE.market.color,
  // One corridor colour, because there is one network on this map. The
  // `optimised`, `recommended`, `scenario` and `changed` entries were keyed by
  // a network state this map no longer has; the scenario map draws a changed
  // lane from its own literals a few hundred lines below, and left here they
  // read as a palette something still chooses from.
  flow: {
    actual: '#94a3b8',
  },
};

/**
 * "Zoom to the network", beside the +/- control.
 *
 * Both halves of a zoom, not one: the way IN, because the default frame is
 * the whole country and a network can be a speck inside it (measured on the
 * Canadian workbook: zoom 2, the sites occupying about a seventh of the card),
 * and the way BACK, because zooming into a corner of a card this size with no
 * reset is a trap. `fitBounds` is the only thing that knows where the nodes
 * are; a user has no way to ask for it otherwise.
 *
 * The Digital Twin page gets the same button — one behaviour on every map,
 * which is the point of the ask.
 */
function addFitControl(map, containerId) {
  const control = L.control({ position: 'bottomleft' });
  control.onAdd = () => {
    const bar = L.DomUtil.create('div', 'leaflet-bar map-fit-control');
    const link = L.DomUtil.create('a', '', bar);
    link.href = '#';
    link.title = 'Zoom to the network';
    link.setAttribute('role', 'button');
    link.setAttribute('aria-label', 'Zoom to the network');
    link.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" '
      + 'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" '
      + 'width="14" height="14"><path d="M7 3H3v4M13 3h4v4M7 17H3v-4M13 17h4v-4"/></svg>';
    L.DomEvent.disableClickPropagation(bar);
    L.DomEvent.on(link, 'click', (e) => {
      L.DomEvent.preventDefault(e);
      fitToNetwork(containerId, { sites: true });
    });
    return bar;
  };
  control.addTo(map);
}

/**
 * Give a compact map the wheel, but only once it has been asked for.
 *
 * The scenario planner's twin card is the bottom row of a page that scrolls.
 * A map that takes the wheel the instant the pointer crosses it swallows that
 * scroll — the page stops moving and the map zooms instead, which is why the
 * wheel was turned off for compact maps in the first place. Turning it back
 * on unconditionally would restore the original problem.
 *
 * So: click the map and it zooms like the Digital Twin's; move the pointer
 * off it and the page has the wheel back. A wheel over an unarmed map says
 * so rather than doing nothing silently — Nielsen #1, visibility of system
 * status. Every other affordance (+/-, fit, double-click, box-zoom, keyboard)
 * is live from the start, as it is on the twin page.
 */
function armCompactZoom(map, container) {
  if (!container || container.querySelector('.map-zoom-hint')) return;

  const hint = document.createElement('div');
  hint.className = 'map-zoom-hint';
  hint.setAttribute('aria-hidden', 'true');
  hint.textContent = 'Click the map to zoom';
  container.appendChild(hint);

  let armed = false;
  let hintTimer = null;

  const arm = () => {
    if (armed) return;
    armed = true;
    map.scrollWheelZoom.enable();
    container.classList.add('is-zoom-armed');
    hint.classList.remove('is-visible');
  };
  const disarm = () => {
    if (!armed) return;
    armed = false;
    map.scrollWheelZoom.disable();
    container.classList.remove('is-zoom-armed');
  };

  container.addEventListener('mousedown', arm);
  container.addEventListener('mouseleave', disarm);
  container.addEventListener('wheel', () => {
    if (armed) return;
    hint.classList.add('is-visible');
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => hint.classList.remove('is-visible'), 1500);
  }, { passive: true });
}

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
  // The Digital Twin page is the reference: its map has the wheel on from
  // the start, and every other map in this product is meant to behave the
  // same way. `isCompact` still decides the default for anything that does
  // not say otherwise, but a caller that asks for the wheel gets it — see
  // scenarios.js, which does.
  const scrollWheelZoom = options.scrollWheelZoom !== undefined
    ? options.scrollWheelZoom : !options.isCompact;

  const map = L.map(containerId, {
    center: center,
    zoom: zoom,
    // Bottom-left, not Leaflet's default top-left: the top-left corner of a
    // map card is where the network's own figures go (see
    // .twin3d-stats-overlay), and the +/- buttons were sitting on top of
    // them — which is why the 2D copy had been pushed to the right-hand side
    // and stopped matching the 3D twin.
    zoomControl: false,
    attributionControl: false,
    scrollWheelZoom: scrollWheelZoom,
  });
  L.control.zoom({ position: 'bottomleft' }).addTo(map);
  addFitControl(map, containerId);
  // Only for a map whose wheel is genuinely off. Arming a map that already
  // zooms would show "Click the map to zoom" over one that just zoomed.
  if (options.isCompact && !scrollWheelZoom) armCompactZoom(map, container);

  maps[containerId] = map;
  // What this particular map is for. Read back by `createNodeMarker`, which
  // is shared with the Scenario Planner's thumbnail: `showLabels` is what
  // keeps the facility pills on the Digital Twin's full-size map and off a
  // card a few hundred pixels wide beside a comparison table.
  mapOptions[containerId] = {
    isCompact: !!options.isCompact,
    showLabels: options.showLabels !== undefined
      ? !!options.showLabels : !options.isCompact,
  };
  // Read-only handle for diagnostics: "where is the map looking" is otherwise
  // unanswerable from outside this module.
  if (typeof window !== 'undefined') {
    // Every mounted map, keyed by container. "What zoom is the scenario
    // planner's card at" was unanswerable from outside this module, which
    // meant the only way to check that its wheel zoom works was to look at a
    // pane transform and hope. Read-only: nothing in the app writes here.
    window.__ngMaps = maps;
    if (containerId === 'map-twin') window.__ngTwinMap = map;
  }
  baseLayers[containerId] = addBaseLayer(map, containerId);
  layerGroups[containerId] = {
    nodes: L.layerGroup().addTo(map),
    flows: L.layerGroup().addTo(map),
  };

  // Draw the network immediately.
  // `initMap` used to create the map, add a legend and stop: nothing was
  // plotted until the user clicked one of the network-state toggle buttons,
  // which was the only thing that drew it. Opening Digital Twin → 2D therefore
  // showed an empty basemap with a legend for a network that was fully loaded.
  // Those toggles are gone now; this is the only thing that draws it.
  if (options.initialScenario) {
    renderScenarioDigitalTwin(containerId, options.initialScenario, options.mode || 'scenario');
  } else {
    renderNetwork(containerId);
  }
  // Framed AFTER drawing, on BOTH paths. It used to be inside the `else`, so
  // the only map built through the scenario path — Scenario Planning's
  // baseline twin — kept the literal `center: [22.5, 79.5], zoom: 4.2` it was
  // constructed with: central India, for every network ever loaded. The nodes
  // and lanes were drawn correctly the whole time, several thousand kilometres
  // off the left edge of a map nobody had ever pointed at them.
  fitToNetwork(containerId);

  // Add legend
  addLegend(map, options.isCompact);

  // Labels that would sit on top of each other are dropped until there is
  // room for them. Recomputed on view change only — a pan or a zoom is the
  // only thing that can alter which pills collide.
  if (mapOptions[containerId].showLabels) {
    map.on('moveend zoomend', () => declutterMapLabels(containerId));
    setTimeout(() => declutterMapLabels(containerId), 0);
  }

  return map;
}

/**
 * Hide the facility labels that would land on top of one another.
 *
 * On the Canadian network twenty-one distribution centres sit inside a few
 * hundred kilometres of southern Ontario and Quebec, so at the zoom that
 * frames the whole country their pills overlap into a block of text a reader
 * can pick nothing out of.
 *
 * Placed in draw order, and a label that clashes with one already placed is
 * hidden until the reader zooms in far enough to separate them — which makes
 * zooming the obvious way to read a dense corner rather than something to
 * discover (Nielsen #3). Nothing is removed from the DOM: the marker, its
 * tooltip and its click target are untouched, so a hidden label costs the
 * reader nothing but the pill.
 */
function declutterMapLabels(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const labels = [...container.querySelectorAll('.map-node-label')];
  if (!labels.length) return;

  // Shown first, so every box is measured at its natural size rather than at
  // the zero it would report while hidden.
  labels.forEach((el) => { el.style.visibility = ''; });

  const placed = [];
  labels.forEach((el) => {
    const r = el.getBoundingClientRect();
    if (!r.width) return;
    const clash = placed.some((q) =>
      r.left < q.right + 2 && r.right > q.left - 2
      && r.top < q.bottom + 2 && r.bottom > q.top - 2);
    if (clash) {
      // `visibility`, not `display`: a hidden label must keep taking part in
      // the next pass's measurement, and a `display:none` element has no box
      // to measure.
      el.style.visibility = 'hidden';
    } else {
      placed.push(r);
    }
  });
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
    // Re-choose the basemap: a map built before hydration was framed on the
    // bundled India raster, and the network that has since loaded may be
    // somewhere else entirely. Without this the first view of a US network is
    // its nodes drawn over the Deccan.
    rebuildBaseLayer(id);
    // The scenario map's NODES belong to whichever scenario is selected, and
    // scenarios.js redraws them from its own `authoritativeDataLoaded`
    // handler. Drawing the plain network over them here would replace a
    // scenario's plan with the baseline's. Its basemap and framing are still
    // this function's business — skipping the map entirely, as this used to,
    // is why it kept an India basemap and an India viewport for every network.
    if (id !== 'scenario-leaflet-map') {
      renderNetwork(id);
    }
    fitToNetwork(id);
    try { maps[id].invalidateSize(); } catch (e) { /* not yet visible */ }
  });
  renderMapLegendCounts();
}

/**
 * Swap the basemap for the one the loaded network needs.
 *
 * Cheap and idempotent: when the chosen kind has not changed, nothing is
 * touched. Only called on a network refresh, never per frame.
 */
function rebuildBaseLayer(containerId) {
  const map = maps[containerId];
  if (!map) return;
  // Always rebuilt on a network change, because the country layer highlights
  // the countries the network is IN — which is a property of the data, not of
  // the map. The old check compared basemap KINDS and so never fired for two
  // different networks that happened to want the same kind.
  const current = baseLayers[containerId];

  (current?.layers || []).forEach((layer) => {
    try { map.removeLayer(layer); } catch (e) { /* already gone */ }
  });
  if (current?.control) {
    try { map.removeControl(current.control); } catch (e) { /* already gone */ }
  }
  map.getContainer().style.background = '';
  map.setMaxZoom(18);
  baseLayers[containerId] = addBaseLayer(map, containerId);
}

/**
 * Frame the map on the network that is loaded, rather than on India.
 *
 * A fixed centre/zoom is right for the demo network and wrong for anyone
 * whose sites sit in one region. No-op when nothing has coordinates.
 */
export function fitToNetwork(containerId, { sites = false } = {}) {
  const map = maps[containerId];
  if (!map) return;
  const nodes = [...PLANTS, ...DCS, ...MARKETS];
  // `networkWindow()` — the same call the 3D twin projects its ground plane
  // onto. This used to be `fitBounds(points, {padding: [40,40]})`, which is a
  // padding in SCREEN PIXELS and therefore a different amount of geography in
  // every container size; the twin padded by a share of the span. The two
  // views were framed by two rules and agreed only by coincidence.
  //
  // `sites: true` asks the same function for the sites alone. The default
  // window is the whole COUNTRY, deliberately — a reader orients on a
  // coastline they know before they read a node — and for most networks the
  // two are close. For some they are not: Canada's outline reaches 83°N, and
  // Mercator stretches that top strip so violently that a network entirely
  // in the southern provinces frames at zoom 2, as a cluster a centimetre
  // across on a map of the world. That is the default view, unchanged and
  // still correct; this is the way in. Same window function, same padding
  // rules — only `wholeCountry` differs, so the two framings cannot drift.
  const win = sites ? networkWindow(nodes, { wholeCountry: false })
                    : networkWindow(nodes);
  if (!win) return;
  try {
    map.fitBounds(
      L.latLngBounds([[win.latMin, win.lngMin], [win.latMax, win.lngMax]]),
      { padding: [0, 0], maxZoom: 7 },
    );
  } catch (e) {
    /* Degenerate bounds — keep the default view. */
  }
}

/**
 * A map has just become visible: re-measure it, then frame it.
 *
 * `initMap('map-twin')` runs when the Digital Twin tab opens, and at that
 * moment `#twin-2d-panel` is `display: none` because 3D is the default view.
 * Leaflet therefore fitted the network into a 0x0 container, which resolves to
 * the minimum zoom — so clicking "2D Map" showed the whole world with the
 * network as a coloured speck over Georgia. `invalidateSize()` alone does not
 * fix it: it corrects the viewport and leaves the zoom where it was.
 *
 * Only for reveal. It must NOT run on every window resize, or it would throw
 * away a pan or zoom the user had just made.
 */
export function revealMap(containerId) {
  const map = maps[containerId];
  if (!map) return;
  try {
    map.invalidateSize();
  } catch (e) {
    return;   // still not laid out; the next reveal will catch it
  }
  const size = map.getSize();
  if (!size || size.x < 40 || size.y < 40) return;
  fitToNetwork(containerId);
}

/** Is this container's map mounted yet? */
export function hasMap(containerId) {
  return Boolean(maps[containerId]);
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
        Cost: ${formatCurrencyExact(flow.cost)}/unit · ${flow.distance == null ? '—' : flow.distance + ' km'}
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

  renderMapLegendCounts();
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
function renderNetwork(containerId) {
  const lg = layerGroups[containerId];
  if (!lg) return;

  lg.nodes.clearLayers();
  lg.flows.clearLayers();

  // The lanes the solve reports. There is one network on this map and these
  // are its corridors; a second, "optimised" set used to be manufactured here
  // by scaling three named prototype lanes.
  const flowData = LANES;

  // Banded once for the whole draw, not recomputed per lane.
  //
  // The weight was `flow / 1500` clamped to 1..8 — a constant that suits one
  // network. Case-16's largest corridor carries about 4,500 units, so every
  // lane on it landed under weight 3 and the map was a uniform grey web; a
  // network moving half a million units clamped every lane to 8 and was a
  // uniform thick one. In both the thickness was real and carried no
  // information, and no legend said what it meant. See `twin-legend.js`.
  const bands = flowBands();

  // Draw flows
  flowData.forEach((flow) => {
    const from = getCoords(flow.from);
    const to = getCoords(flow.to);
    if (!from || !to) return;

    const thickness = bandForFlow(flow.flow, bands).weight2d;

    const line = L.polyline([from, to], {
      // One network, one corridor style. The colour, the opacity and the dash
      // all keyed off a network-state parameter that no longer exists — a
      // solid line is what the actual network's corridors have always been
      // drawn as, and there is no second state to distinguish from.
      color: COLORS.flow.actual,
      weight: thickness,
      opacity: 0.35,
    });

    line.bindTooltip(
      `
      <div style="font-size:12px">
        <strong>${getFacilityName(flow.from)} → ${getFacilityName(flow.to)}</strong><br>
        Flow: ${formatNumber(flow.flow)} ${perPeriodLabel()}<br>
        Cost: ${formatCurrencyExact(flow.cost)}/unit · ${flow.distance == null ? '—' : flow.distance + ' km'}
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
    // for two of the prototype's own DCs whenever a "recommended" state was
    // selected — figures no engine produced, shown on top of whatever network
    // was loaded. This map draws the solved network and nothing else; the
    // scenario map (`renderScenarioDigitalTwin`) is where a hypothetical one
    // is drawn, from its own solve.
    const marker = createNodeMarker(d, 'dc', containerId, null);
    lg.nodes.addLayer(marker);
  });

  MARKETS.forEach((m) => {
    const marker = createNodeMarker(m, 'market', containerId);
    lg.nodes.addLayer(marker);
  });

  renderMapLegendCounts();
  // New markers, new pills: the previous pass's decisions were about
  // elements that no longer exist.
  setTimeout(() => declutterMapLabels(containerId), 0);
}

// ─── Create Node Marker ─────────────────────────────────────
function createNodeMarker(node, type, containerId, overrideStats = null) {
  // The same glyphs the legend prints, from the same place.
  const iconMap = { plant: NODE_STYLE.plant.glyph, dc: NODE_STYLE.dc.glyph,
                    market: NODE_STYLE.market.glyph };
  const colorMap = { plant: COLORS.plant, dc: COLORS.dc, market: COLORS.market };
  // From `NODE_STYLE`, so the marker, the legend chip and the 3D badge
  // are one decision rather than three numbers in three files.
  const sizeMap = { plant: NODE_STYLE.plant.radius, dc: NODE_STYLE.dc.radius,
                    market: NODE_STYLE.market.radius };
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
    // A DC's marker still grows with its load — a second reading of the same
    // node, agreeing with the ring's colour. The band moved up with the base
    // size so a lightly-loaded DC is never smaller than a market.
    adjustedSize = Math.max(
      NODE_STYLE.market.radius + 2,
      Math.min(NODE_STYLE.dc.radius + 8,
               NODE_STYLE.dc.radius - 3 + ((utilPct || 0) / 100) * 11));
  }
  if (isClosed) border = '3px dashed #94a3b8';
  if (isNew) border = '3px solid #6B2FA0';

  // A site the client PROPOSED and the solver did not take is drawn as an
  // outline, not as a closure. Both arrive with `isOpen === false`; without
  // this the map put a stop sign on a warehouse that was never built and the
  // tooltip said it had been "closed in this scenario".
  const isCandidate = String(node.status || '').toUpperCase() === 'CANDIDATE';
  const isUnbuiltCandidate = isCandidate && !isNew
    && !(overrideStats && overrideStats.isOpen === true);
  if (isUnbuiltCandidate) border = '3px dashed #6B2FA0';

  const glyph = (isClosed && !isCandidate) ? '⛔' : iconMap[type];
  const badge = isNew
    ? '<span style="position:absolute;top:-6px;right:-8px;background:#6B2FA0;color:#fff;'
      + 'font-size:8px;font-weight:800;padding:1px 4px;border-radius:6px;letter-spacing:.04em">NEW</span>'
    : '';

  // THE NAME, ON THE MAP, WITH THE ID IN IT.
  //
  // Every facility name lived in a hover tooltip, so reading the map meant
  // pointing at each site in turn and remembering what the last one said, and
  // nothing on screen ever carried the id — the one column the reader's own
  // workbook is keyed on, and the one thing that does not repeat across two
  // sites both called "Central DC".
  //
  // Markets are deliberately unlabelled. A network with twenty of them would
  // put twenty more pills on the map to name the small dots that are already
  // the least ambiguous thing on it, and the mockup labels the facilities
  // only. Their names stay one hover away, with the id now in them.
  // Facilities only, and only on a map big enough to carry the pills.
  const showLabel = type !== 'market' && mapOptions[containerId]?.showLabels !== false;
  // The city, not the full name. Drawn in full, twenty-six pills reading
  // "F025 · Regina Global Transportation Hub Candidate DC" cover the middle of
  // the map — every one legible alone and none of them readable together. The
  // full identity stays one hover away, in the tooltip below.
  const labelText = showLabel ? facilityShortLabel(node) : '';
  const labelHtml = labelText ? `<span class="map-node-label${
    isClosed && !isCandidate ? ' is-closed' : ''}">${escapeHtml(labelText)}</span>` : '';

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
      font-size:${glyphSize(adjustedSize)}px;
      cursor:pointer;
      box-shadow: 0 1px 4px rgba(0,0,0,0.15);
      transition: transform .2s;
    " onmouseover="this.style.transform='scale(1.2)'" onmouseout="this.style.transform='scale(1)'">${glyph}${badge}${labelHtml}</div>`,
    iconSize: [adjustedSize * 2, adjustedSize * 2],
    iconAnchor: [adjustedSize, adjustedSize],
  });

  const marker = L.marker([node.lat, node.lng], { icon });

  let tooltipContent = `<div style="font-size:12px;line-height:1.4"><strong>${escapeHtml(facilityLabel(node))}</strong>`;
  if (isNew) tooltipContent += ' <span style="color:#6B2FA0;font-weight:700">(new in this scenario)</span>';
  if (isUnbuiltCandidate) tooltipContent += ' <span style="color:#6B2FA0;font-weight:700">(proposed site — not opened)</span>';
  else if (isCandidate) tooltipContent += ' <span style="color:#6B2FA0;font-weight:700">(proposed site — opened by the optimiser)</span>';
  if (isClosed && !isCandidate) tooltipContent += ' <span style="color:#dc2626;font-weight:700">(closed in this scenario)</span>';
  tooltipContent += '<br>';
  if (type === 'plant') {
    tooltipContent += `Capacity: ${formatNumber(node.capacity)} ${perPeriodLabel()}<br>Throughput: ${formatNumber(throughput)} ${perPeriodLabel()}`;
  } else if (type === 'dc') {
    tooltipContent += `Utilisation: <strong style="color:${getUtilColor(utilPct)}">${utilPct === null || utilPct === undefined ? '—' : `${utilPct}%`}</strong><br>`;
    tooltipContent += `Capacity: ${formatNumber(node.capacity)} ${perPeriodLabel()}<br>Throughput: ${formatNumber(throughput)} ${perPeriodLabel()}`;
    if (note) {
      tooltipContent += `<div style="margin-top:3px;font-size:11px;color:var(--primary);font-weight:600">• ${note}</div>`;
    }
  } else {
    tooltipContent += `Demand: ${formatNumber(node.demand)} ${perPeriodLabel()}<br>SLA: ${node.slaDays}d · ${node.priority}`;
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
  // The id belongs in a corridor's endpoints as much as on the node itself:
  // "F001 → F006" is what the reader's lane sheet calls this row.
  return node ? facilityLabel(node) : id;
}

/** Text into an attribute or a divIcon's HTML. */
function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// ─── Legend ──────────────────────────────────────────────────
// Both legends on this module are composed in `twin-legend.js`, which is the
// one place node identity, the flow bands and the utilisation bands are
// defined. `iconChip` and `legendCount` used to be written out here for the
// scenario map's own three-row key; that key now comes from the same place as
// the twin's, and the counts are emitted by `twinLegendHtml` as
// `[data-legend-count]` rows that `renderMapLegendCounts` fills.

/**
 * Fill every legend count on the page from the network as it now stands.
 *
 * Both legends — the 3D one in the markup and the Leaflet control below —
 * carry `data-legend-count` rows rather than each counting for itself, so
 * there is one rule for what "how many DCs" means and the two views can never
 * disagree. Called on every network refresh and by `renderTwinStats()`.
 */
export function renderMapLegendCounts() {
  if (typeof document === 'undefined') return;
  const counts = { plant: PLANTS.length, dc: DCS.length, market: MARKETS.length };
  document.querySelectorAll('[data-legend-count]').forEach((el) => {
    const n = counts[el.getAttribute('data-legend-count')];
    el.textContent = Number.isFinite(n) ? String(n) : '0';
  });
}

/**
 * The scenario key's host BELOW the map, or null on any page without one.
 *
 * The scenario key is not a Leaflet control. Over the map it covered the
 * bottom-right corner of the panel — which on a national network is where the
 * southern sites are — and it had to be bounded and scrolled to fit inside a
 * `clamp(360px, 46vh, 520px)` box, so a reader at a short viewport was
 * scrolling a key to read it. Below the map it lays its four groups out as
 * columns, occupies about a fifth of the height, and hides nothing.
 */
function scenarioLegendHost() {
  if (typeof document === 'undefined') return null;
  return document.getElementById('scenario-map-legend');
}

function addLegend(map, isCompact = false) {
  if (isCompact) {
    // THE SAME KEY THE TWIN USES, plus the three encodings only a scenario
    // map has. This branch used to render three rows — plant, DC, market —
    // on the grounds that the scenario map was a thumbnail. It is the
    // full-width panel at the foot of the page, and it was drawing a purple
    // dashed corridor for a moved lane, a grey one for an unmoved one and a
    // utilisation band on every DC, with a key that explained none of them.
    //
    // Rendered into the page rather than mounted on the map — see
    // `scenarioLegendHost`. No control is registered for this map, so
    // `refreshTwinMapLegend` reaches it through the host instead.
    const host = scenarioLegendHost();
    if (host) host.innerHTML = scenarioLegendHtml(perPeriodLabel());
    setTimeout(renderMapLegendCounts, 0);
    return;
  }
  const legend = L.control({ position: 'bottomright' });
  legend.onAdd = function () {
    // The Digital Twin's own map, and the only map this mounts a control on.
    // Built from `twinLegendHtml` so it says exactly what the 3D twin's
    // legend says — the two used to be written out separately in two files
    // and had already drifted, the 2D one keying the DC ring at
    // >95/85-95/<85 in one set of colours and the 3D one at the same bands
    // in another.
    const div = L.DomUtil.create('div');
    div.className = 'tw-legend tw-legend-2d';
    div.innerHTML = twinLegendHtml(perPeriodLabel());
    // A legend is a thing you read, not a thing you pan the map with.
    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);
    return div;
  };
  legend.addTo(map);
  legendControls[legendKeyFor(map)] = legend;
  // The legend is built once, at initMap — usually before any network has
  // loaded — so its counts start at 0 and are filled the moment there is
  // something to count.
  setTimeout(renderMapLegendCounts, 0);
}

/** Every legend control this module has mounted, by container id. */
const legendControls = {};

function legendKeyFor(map) {
  return Object.keys(maps).find((id) => maps[id] === map) || 'unknown';
}

/**
 * Redraw the twin map's legend against the network as it now stands.
 *
 * The flow bands come from the loaded lanes, so a legend built at `initMap`
 * — before any network exists — carries the "no corridor states a volume"
 * copy and would keep it for the rest of the session. `renderTwinStats()`
 * calls this on every refresh.
 */
export function refreshTwinMapLegend() {
  if (typeof document === 'undefined') return;
  document.querySelectorAll('.tw-legend-2d').forEach((el) => {
    el.innerHTML = twinLegendHtml(perPeriodLabel());
  });
  // The scenario key is not a map control and is not matched by the selector
  // above. It redraws as what it IS: rewriting it with `twinLegendHtml` would
  // strip its own three scenario rows on the first refresh after a network
  // loaded — which is every refresh.
  const host = scenarioLegendHost();
  if (host) host.innerHTML = scenarioLegendHtml(perPeriodLabel());
  renderMapLegendCounts();
}
