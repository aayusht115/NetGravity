/**
 * NetGravity — 3D Digital Twin (Light Mode)
 * =========================================
 * The 2D map's geography, standing up.
 *
 * The ground plane is built from `world-basemap.js` — the same Natural Earth
 * country rings `map.js` hands to Leaflet — projected through the same Web
 * Mercator maths onto the same lat/lng window `networkWindow()` gives the 2D
 * map. So the two views are not two pictures kept in step by hand: the 3D
 * scene is the 2D map extruded, and a coastline that moves in one moves in
 * the other because there is only one set of coordinates.
 *
 * It used to stand on a raster photograph of India, applied only when the
 * network happened to sit inside 4-39N / 65-100E. Anywhere else the plane was
 * blank: a US network's twelve facilities floated over white while the
 * counters beside them correctly reported 24 nodes and 51 corridors. Vector
 * land works for every network, everywhere, and stays sharp at any zoom.
 */

/* global THREE */
import { PLANTS, DCS, MARKETS, LANES, formatNumber, getUtilColor, getUtilLabel,
         perPeriodLabel } from './data.js';
import { WORLD_COUNTRIES, countriesContaining, networkWindow,
         clipRingToBounds, ringIntersects, loadAdmin1,
         admin1IfLoaded } from './world-basemap.js';

// ─── Basemap Calibration ─────────────────────────────────────
// The basemap image was captured from a live Leaflet map (CARTO "light_all"
// tiles) centred at MERCATOR_CENTER, at MERCATOR_ZOOM, then cropped to the
// pixel box below. Reproducing the same Web Mercator projection here means
// any lat/lng maps onto the exact same pixel the real tile imagery shows.
const MERCATOR_ZOOM = 5;
const MERCATOR_CENTER = { lat: 22.5, lng: 80.0 };
const CAPTURE_SIZE = { w: 1400, h: 1200 };       // container the basemap was captured at
const CROP_BOUNDS = { latMax: 39.0, lngMin: 65.0, latMin: 4.0, lngMax: 100.0 }; // crop corners (lat/lng)

function mercatorWorldXY(lat, lng, zoom) {
  const worldSize = 256 * Math.pow(2, zoom);
  const x = worldSize * (lng + 180) / 360;
  const siny = Math.max(Math.min(Math.sin(lat * Math.PI / 180), 0.9999), -0.9999);
  const y = worldSize * (0.5 - Math.log((1 + siny) / (1 - siny)) / (4 * Math.PI));
  return { x, y };
}

const captureCenterPx = mercatorWorldXY(MERCATOR_CENTER.lat, MERCATOR_CENTER.lng, MERCATOR_ZOOM);
const captureTopLeftPx = {
  x: captureCenterPx.x - CAPTURE_SIZE.w / 2,
  y: captureCenterPx.y - CAPTURE_SIZE.h / 2,
};

// ─── Projection window ───────────────────────────────────────
//
// The lat/lng box the ground plane represents.
//
// This was fixed to the India crop the bundled photograph was taken from, so
// `geoTo3D` produced u/v far outside [0,1] for any network elsewhere and the
// nodes projected off the plane entirely. It is now whatever
// `networkWindow()` returns for the loaded network — the SAME call the 2D map
// frames itself with — so the two views show the same ground at the same
// scale, and both follow the data.
let PROJECTION = null;

/** The window used before any network has loaded: the whole world. */
const DEFAULT_BOUNDS = { latMin: -60, latMax: 78, lngMin: -170, lngMax: 178 };

function makeProjection(bounds) {
  const topLeft = mercatorWorldXY(bounds.latMax, bounds.lngMin, MERCATOR_ZOOM);
  const bottomRight = mercatorWorldXY(bounds.latMin, bounds.lngMax, MERCATOR_ZOOM);
  const imgW = bottomRight.x - topLeft.x;
  const imgH = bottomRight.y - topLeft.y;
  const width = 84;
  return {
    bounds, topLeft, imgW, imgH,
    width,
    // Aspect follows the window, so a wide network is not squashed into a
    // portrait plane, and a tall one is not stretched across a landscape one.
    height: width * (imgH / imgW),
  };
}

PROJECTION = makeProjection(DEFAULT_BOUNDS);

/**
 * Point the ground plane at the network currently loaded.
 *
 * Returns true when the window actually moved, so the caller knows the plane
 * has to be rebuilt rather than merely re-populated. Comparing the bounds
 * rather than a basemap "kind" matters: two different networks in the same
 * country want the same KIND of ground and completely different windows, and
 * the old check saw no difference between them.
 */
function updateProjection() {
  const nodes = [...PLANTS, ...DCS, ...MARKETS];
  const bounds = networkWindow(nodes) || DEFAULT_BOUNDS;
  const b = PROJECTION.bounds;
  const same = Math.abs(b.latMin - bounds.latMin) < 1e-6
    && Math.abs(b.latMax - bounds.latMax) < 1e-6
    && Math.abs(b.lngMin - bounds.lngMin) < 1e-6
    && Math.abs(b.lngMax - bounds.lngMax) < 1e-6;
  if (same) return false;
  PROJECTION = makeProjection(bounds);
  return true;
}

// ─── Light-theme color palette ───────────────────────────────
const THEME_COLORS = {
  bg:          0xf8fafc, // Slate 50
  // The 2D map's BASEMAP_STYLE, as numbers. Land lighter than water, the way
  // a printed atlas reads; the first pass had the two within 4% luminance of
  // each other, so the coastline was drawn and invisible.
  water:       0xcddced, // the plate the land sits on
  land:        0xf6f9fc, // countries with no site in them
  landActive:  0xffffff, // countries this network has sites in
  coast:       0xa9b8cb,
  coastActive: 0x7f93ad,
  // Lighter than the coastline, so the hierarchy reads without a legend:
  // coast, then country, then state.
  border:      0xb9c8db,
  borderActive: 0x91a6c0,
  graticule:   0xdfe8f2,
  plant:       0x5b21b6, // Kearney Purple (deep)
  plantGlow:   0x8b5cf6,
  dcHealthy:   0x047857, // Emerald (deep)
  dcWarning:   0xb45309, // Amber (deep)
  dcCritical:  0xb91c1c, // Red (deep)
  market:      0x075985, // Sky Blue (deep)
  marketGlow:  0x0ea5e9,
  flowActual:  0x334155, // Slate 700
  flowOptim:   0x5b21b6, // Purple (deep)
  flowRecom:   0x065f46, // Emerald (deep)
};

// ─── Module State ───────────────────────────────────────────
let scene, camera, renderer, controls;
/* The pointer has moved (or the camera has) since the last hover test. */
let pointerDirty = true;
let containerEl = null;
let animationId = null;
let isInitialised = false;
let twin3dState = 'actual';

let terrainGroup, nodeGroup, flowGroup, pulseGroup;
let nodeMeshes = [];      // { id, type, coreMesh, hitMesh, baseRing, data, pos3D }
let flowArcs = [];        // { from, to, curve, line, tube, data }
let photonStreams = [];   // { particles, curve, offsets, speeds, count }

let raycaster, mouse;
let hoveredNode = null;
let clock;
let hudTooltipEl = null;

// ─── Coordinate Conversion (lat/lng → point on the flat map) ─
export function geoTo3D(lat, lng, height = 0) {
  const worldPx = mercatorWorldXY(lat, lng, MERCATOR_ZOOM);
  const u = (worldPx.x - PROJECTION.topLeft.x) / PROJECTION.imgW;
  const v = (worldPx.y - PROJECTION.topLeft.y) / PROJECTION.imgH;

  const x = (u - 0.5) * PROJECTION.width;
  const z = (v - 0.5) * PROJECTION.height;

  return new THREE.Vector3(x, height, z);
}

// ─── Public API ─────────────────────────────────────────────
export function initTwin3D(containerId) {
  containerEl = document.getElementById(containerId);
  if (!containerEl) return;

  if (isInitialised) {
    // The scene/renderer are a module-level singleton shared by every
    // caller (Home's preview and the Digital Twin tab both use this same
    // canvas) — re-parent it into whichever container is asking this
    // time, since only one can be showing it at once.
    if (renderer && renderer.domElement.parentElement !== containerEl) {
      containerEl.innerHTML = '';
      containerEl.appendChild(renderer.domElement);
    }
    resumeTwin3D();
    resizeTwin3D();
    return;
  }

  if (typeof THREE === 'undefined') {
    containerEl.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;height:100%;' +
      'font:500 14px Inter,system-ui,sans-serif;color:#64748b;text-align:center;padding:24px">' +
      '3D engine (three.js) failed to load.<br>Check your network connection, then reload.</div>';
    console.error('[NetGravity 3D] THREE is undefined - three.min.js did not load.');
    return;
  }

  clock = new THREE.Clock();
  raycaster = new THREE.Raycaster();
  mouse = new THREE.Vector2(-999, -999);

  setupScene();
  setupLighting();
  updateProjection();
  setupMapBase();
  setupNetworkNodes();
  setupFlowArcs('actual');
  setupControls();
  setupInteraction();

  isInitialised = true;
  watchVisibility();
  animate();
}

export function setTwin3DState(state) {
  if (state === twin3dState) return;
  twin3dState = state;
  setupFlowArcs(state);
}

/**
 * Rebuild the scene's nodes and arcs from the network currently in `data.js`.
 *
 * `initTwin3D` builds the scene once and returns early on every later call, so
 * loading a different network left the 3D twin showing the previous one's
 * geometry — a user who uploaded a five-node network still saw the prototype's
 * nineteen. The tables beside it were correct, which made the mismatch worse:
 * two views of "the same" network disagreeing.
 *
 * Disposes geometries and materials explicitly; three.js does not free GPU
 * resources when a mesh is merely removed from its parent.
 */
export function rebuildTwin3D() {
  if (!isInitialised || !scene || !nodeGroup) return;

  const dispose = (obj) => {
    obj.traverse?.((child) => {
      if (child.geometry) child.geometry.dispose();
      if (child.material) {
        (Array.isArray(child.material) ? child.material : [child.material])
          .forEach((m) => m.dispose());
      }
    });
  };

  // Re-choose the ground plane BEFORE anything is positioned: `geoTo3D` reads
  // the projection, so nodes placed against the old window would sit off the
  // new plane. `updateProjection` reports whether the window moved, and the
  // terrain is only rebuilt when it did — the plane, its texture and its
  // graticule are the expensive part of this scene.
  const projectionMoved = updateProjection();

  const groups = [nodeGroup, flowGroup, pulseGroup];
  if (projectionMoved && terrainGroup) groups.push(terrainGroup);
  groups.forEach((group) => {
    if (!group) return;
    [...group.children].forEach((child) => {
      dispose(child);
      group.remove(child);
    });
  });
  if (projectionMoved) {
    setupMapBase();
    // A different country is a different picture, so the reader's own angle
    // on the last one is no longer a view of anything. Re-fit from scratch.
    cameraIsUsers = false;
  }

  nodeMeshes = [];
  flowArcs = [];
  photonStreams = [];
  hoveredNode = null;

  setupNetworkNodes();
  setupFlowArcs(twin3dState);
  resizeTwin3D();
}

/** How many nodes the 3D scene currently renders. Diagnostics only. */
export function twin3dNodeCount() {
  return nodeMeshes.length;
}

if (typeof window !== 'undefined') {
  // Rebuild whenever a different network is loaded, or when authoritative
  // figures arrive and change utilisation colouring.
  window.addEventListener('networkDataLoaded', () => rebuildTwin3D());
  window.addEventListener('authoritativeDataLoaded', () => rebuildTwin3D());
  window.twin3dNodeCount = twin3dNodeCount;
}

/**
 * Has the user moved the camera themselves?
 *
 * Their view is theirs: once they have orbited, nothing in this module
 * re-frames the scene under them. Reset when a new network is built, because
 * an angle chosen for one country is not a view of another.
 */
let cameraIsUsers = false;

/**
 * Pull the camera in or out until the ground plane fills the viewport.
 *
 * The camera used to sit at a literal `(0, 78, 64)` for every container.
 * That distance was chosen against one shape of panel; in a taller one the
 * plane shrinks into the middle with a band of empty background above and
 * below it, and in a very wide one it runs off the sides. The direction is
 * still the fixed isometric one — only the distance moves.
 *
 * Measured, not calculated: project the plane's four corners with the camera
 * as it stands, read how far the widest one falls outside the frame, and
 * scale the distance by that. Two or three passes converge, and it is exact
 * for any field of view, aspect or tilt, including one the user has dragged
 * to. `0.92` leaves a hair of margin so the plane's edge is not flush with
 * the canvas edge.
 */
function frameCameraToPlane() {
  if (!camera || !controls || !PROJECTION) return;
  const halfW = PROJECTION.width / 2;
  const halfH = PROJECTION.height / 2;
  if (!(halfW > 0) || !(halfH > 0)) return;
  const target = controls.target.clone();

  // Fit the NODES, and let the ground sheet run off the edges.
  //
  // Fitting the whole plane means fitting a rectangle shaped like the
  // network's bounding box into a card shaped like nothing in particular. In
  // a tall card and a wide network — the United States in a 895x850 panel —
  // the width decides the zoom and the picture ends up a thin band of map
  // with empty background above and below it. Framing on the sites instead
  // crops the corners of the sheet, which carry no information, and puts the
  // network across the middle of the card at a size worth reading.
  //
  // The plane is the fallback for a scene with no nodes yet, so the very
  // first frame after a cold init is still framed on something.
  const pad = 4;
  const points = nodeMeshes.length
    ? nodeMeshes.map((n) => n.pos3D.clone())
    : [];
  const corners = points.length
    ? (() => {
      let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
      points.forEach((v) => {
        minX = Math.min(minX, v.x); maxX = Math.max(maxX, v.x);
        minZ = Math.min(minZ, v.z); maxZ = Math.max(maxZ, v.z);
      });
      if (!Number.isFinite(minX) || !Number.isFinite(minZ)) return null;
      return [
        new THREE.Vector3(minX - pad, 0, minZ - pad),
        new THREE.Vector3(maxX + pad, 0, minZ - pad),
        new THREE.Vector3(minX - pad, 0, maxZ + pad),
        new THREE.Vector3(maxX + pad, 0, maxZ + pad),
      ];
    })()
    : null;
  const box = corners || [
    new THREE.Vector3(-halfW, 0, -halfH), new THREE.Vector3(halfW, 0, -halfH),
    new THREE.Vector3(-halfW, 0, halfH), new THREE.Vector3(halfW, 0, halfH),
  ];
  const dir = camera.position.clone().sub(target);
  if (dir.lengthSq() === 0) return;
  let dist = dir.length();
  dir.normalize();

  for (let pass = 0; pass < 4; pass += 1) {
    camera.position.copy(target).addScaledVector(dir, dist);
    camera.lookAt(target);
    camera.updateMatrixWorld(true);
    camera.updateProjectionMatrix();
    let worst = 0;
    for (let i = 0; i < box.length; i += 1) {
      const ndc = box[i].clone().project(camera);
      if (!Number.isFinite(ndc.x) || !Number.isFinite(ndc.y)) return;
      worst = Math.max(worst, Math.abs(ndc.x), Math.abs(ndc.y));
    }
    if (!(worst > 0)) return;
    if (Math.abs(worst - 0.92) < 0.01) break;
    dist = Math.min(controls.maxDistance,
                    Math.max(controls.minDistance, dist * (worst / 0.92)));
  }

  camera.position.copy(target).addScaledVector(dir, dist);
  camera.lookAt(target);
  camera.updateProjectionMatrix();
  try { controls.update(); } catch (e) { /* not wired yet */ }
}

export function resizeTwin3D() {
  if (!containerEl || !renderer || !camera) return;
  const w = containerEl.clientWidth || 800;
  const h = containerEl.clientHeight || 560;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
  // A container that changed shape needs the fit redone — unless the reader
  // has set their own view, which is not ours to overwrite.
  if (!cameraIsUsers) frameCameraToPlane();
}

export function resumeTwin3D() {
  if (!isInitialised) return;
  resizeTwin3D();
  watchVisibility();
  if (!animationId && isOnScreen && !document.hidden) animate();
}

export function disposeTwin3D() {
  if (animationId) cancelAnimationFrame(animationId);
  animationId = null;
}

/* ═══════════════════════════════════════════════════════════════
   Only render while somebody is looking at it
   ═══════════════════════════════════════════════════════════════
   `animate()` re-arms itself with `requestAnimationFrame` and nothing ever
   cancelled it: `disposeTwin3D` was exported and never called. So a full
   WebGL draw plus a raycast over every node ran sixty times a second for the
   rest of the session — on the Forecast page, on Scenario Planning, on the
   KPI tables, none of which show this canvas. That is the scroll lag: the
   compositor was sharing the frame budget with a 3D scene nobody could see.

   An IntersectionObserver is the right instrument because the canvas is
   re-parented between Home's card and the Digital Twin tab, and because a
   page can also scroll it out of view without changing tab. `visibilitychange`
   covers the background-tab case, which the observer does not report. */
let isOnScreen = true;
let visibilityWatched = false;

function setRunning(shouldRun) {
  if (shouldRun) {
    if (!animationId && isInitialised) animate();
    return;
  }
  if (animationId) cancelAnimationFrame(animationId);
  animationId = null;
}

function watchVisibility() {
  if (visibilityWatched || typeof window === 'undefined') return;
  visibilityWatched = true;

  if (typeof IntersectionObserver === 'function') {
    const io = new IntersectionObserver((entries) => {
      const entry = entries[entries.length - 1];
      isOnScreen = !!(entry && entry.isIntersecting);
      setRunning(isOnScreen && !document.hidden);
    }, { threshold: 0 });
    // The observed element is the CANVAS, not the container: the container is
    // whichever card currently holds it, and that changes.
    if (renderer && renderer.domElement) io.observe(renderer.domElement);
  }

  document.addEventListener('visibilitychange', () => {
    setRunning(isOnScreen && !document.hidden);
  });
}

/** Diagnostics only: is the render loop currently running? */
export function twin3dIsRendering() {
  return animationId !== null;
}

// ─── Scene Setup ────────────────────────────────────────────
function setupScene() {
  const w = containerEl.clientWidth || 800;
  const h = containerEl.clientHeight || 560;

  scene = new THREE.Scene();
  scene.background = new THREE.Color(THEME_COLORS.bg);
  scene.fog = new THREE.FogExp2(THEME_COLORS.bg, 0.006);

  // Isometric viewpoint over India
  camera = new THREE.PerspectiveCamera(40, w / h, 1, 400);
  camera.position.set(0, 78, 64);
  camera.lookAt(0, 0, -2);

  // WebGL Renderer (Light theme clear color)
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
  renderer.setClearColor(THEME_COLORS.bg, 1);
  renderer.setSize(w, h);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;

  containerEl.innerHTML = '';
  containerEl.appendChild(renderer.domElement);

  // Groups
  terrainGroup = new THREE.Group();
  flowGroup = new THREE.Group();
  pulseGroup = new THREE.Group();
  nodeGroup = new THREE.Group();

  scene.add(terrainGroup);
  scene.add(flowGroup);
  scene.add(pulseGroup);
  scene.add(nodeGroup);
}

// ─── Lighting ───────────────────────────────────────────────
function setupLighting() {
  const ambient = new THREE.AmbientLight(0xffffff, 1.0);
  scene.add(ambient);

  const mainSun = new THREE.DirectionalLight(0xffffff, 1.2);
  mainSun.position.set(45, 90, 45);
  mainSun.castShadow = true;
  mainSun.shadow.mapSize.width = 2048;
  mainSun.shadow.mapSize.height = 2048;
  mainSun.shadow.camera.near = 10;
  mainSun.shadow.camera.far = 200;
  mainSun.shadow.camera.left = -60;
  mainSun.shadow.camera.right = 60;
  mainSun.shadow.camera.top = 60;
  mainSun.shadow.camera.bottom = -60;
  mainSun.shadow.bias = -0.0005;
  scene.add(mainSun);

  const fillLight = new THREE.DirectionalLight(0xe2e8f0, 0.5);
  fillLight.position.set(-45, 50, -45);
  scene.add(fillLight);

  const accentLight = new THREE.DirectionalLight(0xddd6fe, 0.3);
  accentLight.position.set(0, 30, -50);
  scene.add(accentLight);
}

// ─── Ground plane (the 2D map's own countries, triangulated) ──────
/**
 * Which countries this network actually has sites in.
 *
 * Answered by the same point-in-polygon walk over the same rings the 2D map
 * draws, so the two views highlight the same countries. Cached per rebuild —
 * it is 177 features against every node, and the scene rebuilds on every
 * state change.
 */
let activeCountries = new Set();

function refreshActiveCountries() {
  const pts = [...PLANTS, ...DCS, ...MARKETS];
  activeCountries = new Set(countriesContaining(pts).map((c) => c.name));
}

/**
 * A country ring as a THREE.Shape on the ground.
 *
 * `ShapeGeometry` builds its triangles in the XY plane, and the plane is then
 * laid flat by rotating -90 degrees about X — which sends (sx, sy, 0) to
 * (sx, 0, -sy). So a world point (x, z) has to be entered as (x, -z), or the
 * whole map comes out mirrored north-to-south. It did, the first time.
 */
function ringToShape(ring) {
  const shape = new THREE.Shape();
  for (let i = 0; i < ring.length; i += 1) {
    const v = geoTo3D(ring[i][1], ring[i][0], 0);
    if (i === 0) shape.moveTo(v.x, -v.z);
    else shape.lineTo(v.x, -v.z);
  }
  shape.closePath();
  return shape;
}

/** The same ring as a closed line, for the coastline on top of the land. */
function ringToLinePoints(ring) {
  const pts = ring.map(([lng, lat]) => geoTo3D(lat, lng, 0.05));
  if (pts.length) pts.push(pts[0]);
  return pts;
}

function setupMapBase() {
  const { width, height, bounds } = PROJECTION;
  refreshActiveCountries();

  // Water first: the plate everything else sits on.
  const water = new THREE.Mesh(
    new THREE.PlaneGeometry(width, height),
    new THREE.MeshBasicMaterial({ color: THEME_COLORS.water, toneMapped: false }),
  );
  water.rotation.x = -Math.PI / 2;
  water.position.y = 0;
  terrainGroup.add(water);

  addGroundGraticule();

  // Land. Each ring is clipped to the window before it is triangulated, so a
  // country crossing the edge is cut at the edge rather than drawn past the
  // plate — Canada hanging off the side reads as a fault even when the
  // geometry is right.
  const plainShapes = [];
  const activeShapes = [];
  const plainLines = [];
  const activeLines = [];

  for (const entry of countryRingsByName()) {
    const { ring, name } = entry;
    if (!ringIntersects(ring, bounds)) continue;
    const clipped = clipRingToBounds(ring, bounds);
    if (!clipped) continue;
    const isActive = activeCountries.has(name);
    (isActive ? activeShapes : plainShapes).push(ringToShape(clipped));
    (isActive ? activeLines : plainLines).push(ringToLinePoints(clipped));
  }

  const addLand = (shapes, color, y) => {
    if (!shapes.length) return;
    // One geometry for every ring of a kind: 177 countries as 177 meshes is
    // 177 draw calls a frame for something that never moves.
    const geo = new THREE.ShapeGeometry(shapes);
    const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
      color, toneMapped: false, side: THREE.DoubleSide,
    }));
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.y = y;
    terrainGroup.add(mesh);
  };
  addLand(plainShapes, THEME_COLORS.land, 0.02);
  addLand(activeShapes, THEME_COLORS.landActive, 0.03);

  const addCoast = (lineSets, color, opacity) => {
    if (!lineSets.length) return;
    const positions = [];
    for (const pts of lineSets) {
      for (let i = 0; i + 1 < pts.length; i += 1) {
        positions.push(pts[i].x, pts[i].y, pts[i].z,
                       pts[i + 1].x, pts[i + 1].y, pts[i + 1].z);
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position',
                     new THREE.Float32BufferAttribute(positions, 3));
    terrainGroup.add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({
      color, transparent: true, opacity,
    })));
  };
  addCoast(plainLines, THEME_COLORS.coast, 0.75);
  addCoast(activeLines, THEME_COLORS.coastActive, 1);
  addSubdivisionBorders(addCoast);

  // The plate's own edge, so the ground reads as a finite object.
  const borderGeo = new THREE.EdgesGeometry(new THREE.PlaneGeometry(width, height));
  const borderLines = new THREE.LineSegments(
    borderGeo, new THREE.LineBasicMaterial({ color: 0xcbd5e1 }));
  borderLines.rotation.x = -Math.PI / 2;
  borderLines.position.y = 0.08;
  terrainGroup.add(borderLines);

  // Soft ground shadow underneath, so the map reads as slightly raised.
  const shadowMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(width * 1.06, height * 1.06),
    new THREE.MeshBasicMaterial({ color: 0xcbd5e1, transparent: true, opacity: 0.5 }),
  );
  shadowMesh.rotation.x = -Math.PI / 2;
  shadowMesh.position.y = -0.35;
  terrainGroup.add(shadowMesh);
}

/**
 * State and province borders on the ground plane.
 *
 * The same rings `map.js` hands to Leaflet, through the same projection and
 * the same clip — so a border that bends in one view bends in the other.
 *
 * Lines, not filled shapes. The land is already painted by the country layer
 * underneath; filling each subdivision again would double the triangle count
 * for no visual gain and put a seam along every shared edge.
 *
 * The module is loaded on demand, so this runs twice: once now with whatever
 * is already in memory (nothing, on a first visit) and again from the `.then`
 * when it arrives. `setupMapBase` is idempotent for this group, because the
 * second pass adds to `terrainGroup` which the next rebuild clears wholesale.
 */
function addSubdivisionBorders(addCoast) {
  const draw = (admin1) => {
    if (!admin1 || !terrainGroup) return;
    const { bounds } = PROJECTION;
    const plain = [];
    const active = [];
    for (const entry of admin1.rings) {
      if (!ringIntersects(entry.ring, bounds)) continue;
      const clipped = clipRingToBounds(entry.ring, bounds);
      if (!clipped) continue;
      (activeCountries.has(entry.admin) ? active : plain)
        .push(ringToLinePoints(clipped));
    }
    addCoast(plain, THEME_COLORS.border, 0.55);
    addCoast(active, THEME_COLORS.borderActive, 1);
  };

  const already = admin1IfLoaded();
  if (already) { draw(already); return; }
  // Captured so a scene rebuilt while this was in flight does not get the
  // borders of the window it has since left.
  const forProjection = PROJECTION;
  loadAdmin1().then((admin1) => {
    if (PROJECTION !== forProjection) return;
    draw(admin1);
  });
}

/**
 * Every country ring paired with its country's name.
 *
 * `countryRings()` returns rings alone, which is all the geometry needs; the
 * ground plane also has to know which country a ring belongs to in order to
 * lift the ones the network is in. Built once and cached with the rings.
 */
let _namedRings = null;
function countryRingsByName() {
  if (_namedRings) return _namedRings;
  _namedRings = [];
  for (const f of WORLD_COUNTRIES.features) {
    for (const poly of f.geometry.coordinates) {
      if (poly[0] && poly[0].length >= 4) {
        _namedRings.push({ ring: poly[0], name: f.properties.name });
      }
    }
  }
  return _namedRings;
}

/**
 * A latitude/longitude grid on the ground plane.
 *
 * It does something the coastlines do not: it gives distance a scale. Real
 * meridians and parallels at the same Mercator projection the nodes use, so a
 * corridor crossing three of them has crossed thirty degrees.
 */
function addGroundGraticule() {
  const { bounds } = PROJECTION;
  const span = Math.max(bounds.latMax - bounds.latMin, bounds.lngMax - bounds.lngMin);
  const step = span > 60 ? 20 : span > 25 ? 10 : span > 10 ? 5 : 2;
  const mat = new THREE.LineBasicMaterial({
    color: THEME_COLORS.graticule, transparent: true, opacity: 0.9,
  });
  const at = (lat, lng) => geoTo3D(lat, lng, 0.01);
  const add = (a, b) => {
    const geo = new THREE.BufferGeometry().setFromPoints([a, b]);
    terrainGroup.add(new THREE.Line(geo, mat));
  };
  const first = (v) => Math.ceil(v / step) * step;
  for (let lat = first(bounds.latMin); lat <= bounds.latMax; lat += step) {
    add(at(lat, bounds.lngMin), at(lat, bounds.lngMax));
  }
  for (let lng = first(bounds.lngMin); lng <= bounds.lngMax; lng += step) {
    add(at(bounds.latMin, lng), at(bounds.latMax, lng));
  }
}

// ─── Network Nodes ──────────────────────────────────────────
function setupNetworkNodes() {
  while (nodeGroup.children.length > 0) nodeGroup.remove(nodeGroup.children[0]);
  nodeMeshes = [];

  // 1. Manufacturing Plants (Hexagonal Beacons with Crystal Cores)
  PLANTS.forEach(p => {
    const pos = geoTo3D(p.lat, p.lng, 0);
    const nodeObj = createPlant3D(p, pos);
    nodeGroup.add(nodeObj.group);
    nodeMeshes.push({
      id: p.id,
      type: 'plant',
      coreMesh: nodeObj.core,
      hitMesh: nodeObj.hit,
      baseRing: nodeObj.ring,
      data: p,
      pos3D: pos,
    });
  });

  // 2. Distribution Centres (Tiered Hub Cylinders with Utilization Halos)
  DCS.forEach(d => {
    const pos = geoTo3D(d.lat, d.lng, 0);
    const nodeObj = createDC3D(d, pos);
    nodeGroup.add(nodeObj.group);
    nodeMeshes.push({
      id: d.id,
      type: 'dc',
      coreMesh: nodeObj.core,
      hitMesh: nodeObj.hit,
      baseRing: nodeObj.ring,
      data: d,
      pos3D: pos,
    });
  });

  // 3. Demand Markets (Glowing Blue Destination Spheres)
  MARKETS.forEach(m => {
    const pos = geoTo3D(m.lat, m.lng, 0);
    const nodeObj = createMarket3D(m, pos);
    nodeGroup.add(nodeObj.group);
    nodeMeshes.push({
      id: m.id,
      type: 'market',
      coreMesh: nodeObj.core,
      hitMesh: nodeObj.hit,
      baseRing: nodeObj.ring,
      data: m,
      pos3D: pos,
    });
  });
}

function createPlant3D(data, pos) {
  const group = new THREE.Group();
  group.position.copy(pos);

  // Hexagonal Chrome Pedestal
  const baseGeo = new THREE.CylinderGeometry(2.0, 2.3, 0.8, 6);
  const baseMat = new THREE.MeshStandardMaterial({
    color: 0xede9fe, // Soft violet slate
    roughness: 0.2,
    metalness: 0.8,
  });
  const base = new THREE.Mesh(baseGeo, baseMat);
  base.position.y = 0.4;
  base.castShadow = true;
  base.receiveShadow = true;
  group.add(base);

  // Octahedral Core Crystal (Kearney Purple)
  const coreGeo = new THREE.OctahedronGeometry(1.4, 0);
  const coreMat = new THREE.MeshStandardMaterial({
    color: THEME_COLORS.plant,
    emissive: THEME_COLORS.plantGlow,
    emissiveIntensity: 0.7,
    roughness: 0.15,
    metalness: 0.3,
  });
  const core = new THREE.Mesh(coreGeo, coreMat);
  core.position.y = 2.2;
  core.castShadow = true;
  core.userData = { nodeData: data };
  group.add(core);

  // Vertical light beacon beam
  const beamGeo = new THREE.CylinderGeometry(0.08, 0.25, 12, 8);
  const beamMat = new THREE.MeshBasicMaterial({
    color: THEME_COLORS.plantGlow,
    transparent: true,
    opacity: 0.35,
  });
  const beam = new THREE.Mesh(beamGeo, beamMat);
  beam.position.y = 7.5;
  group.add(beam);

  // Glowing Base Ring
  const ringGeo = new THREE.RingGeometry(2.6, 3.0, 32);
  const ringMat = new THREE.MeshBasicMaterial({
    color: THEME_COLORS.plantGlow,
    transparent: true,
    opacity: 0.5,
    side: THREE.DoubleSide,
  });
  const ring = new THREE.Mesh(ringGeo, ringMat);
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.05;
  group.add(ring);

  // Invisible enlarged hit sphere for clicking
  const hitGeo = new THREE.SphereGeometry(3.2, 10, 10);
  const hitMat = new THREE.MeshBasicMaterial({ visible: false });
  const hit = new THREE.Mesh(hitGeo, hitMat);
  hit.position.y = 2.2;
  hit.userData = { nodeData: data };
  group.add(hit);

  return { group, core, hit, ring };
}

function createDC3D(data, pos) {
  const group = new THREE.Group();
  group.position.copy(pos);

  // Utilization Color Coding — same Healthy/Stress/Critical band data.js's
  // getUtilLabel owns everywhere else (85%/95%), just re-expressed in the
  // 3D theme's own color set.
  let dcColor = THEME_COLORS.dcHealthy;
  const utilBand = getUtilLabel(data.utilPct);
  if (utilBand === 'Critical') dcColor = THEME_COLORS.dcCritical;
  else if (utilBand === 'Stress') dcColor = THEME_COLORS.dcWarning;

  // A DC's radius scales with utilisation, which is a solver output. With no
  // solve (or an infeasible one) it is null, and `null / 100` produced NaN —
  // which propagated into the cylinder geometry and made Three.js fail to
  // compute a bounding sphere, so the whole scene failed to render. An
  // un-solved DC draws at the base radius instead.
  const hasUtil = typeof data.utilPct === 'number' && Number.isFinite(data.utilPct);
  const radius = hasUtil ? 1.3 + (data.utilPct / 100) * 0.7 : 1.3;

  // Tiered Base
  const baseGeo = new THREE.CylinderGeometry(radius * 1.1, radius * 1.3, 0.9, 24);
  const baseMat = new THREE.MeshStandardMaterial({
    color: 0xe2e8f0,
    roughness: 0.3,
    metalness: 0.7,
  });
  const base = new THREE.Mesh(baseGeo, baseMat);
  base.position.y = 0.45;
  base.castShadow = true;
  base.receiveShadow = true;
  group.add(base);

  // Glowing DC Dome
  const coreGeo = new THREE.SphereGeometry(radius, 24, 24);
  const coreMat = new THREE.MeshStandardMaterial({
    color: dcColor,
    emissive: dcColor,
    emissiveIntensity: 0.75,
    roughness: 0.2,
    metalness: 0.2,
  });
  const core = new THREE.Mesh(coreGeo, coreMat);
  core.position.y = 0.9 + radius;
  core.castShadow = true;
  core.userData = { nodeData: data };
  group.add(core);

  // Vertical light beacon
  const beamGeo = new THREE.CylinderGeometry(0.1, 0.3, 14, 8);
  const beamMat = new THREE.MeshBasicMaterial({
    color: dcColor,
    transparent: true,
    opacity: utilBand === 'Critical' ? 0.45 : 0.25,
  });
  const beam = new THREE.Mesh(beamGeo, beamMat);
  beam.position.y = 8.5;
  group.add(beam);

  // Base Utilization Ring
  const ringGeo = new THREE.RingGeometry(radius * 1.5, radius * 1.9, 32);
  const ringMat = new THREE.MeshBasicMaterial({
    color: dcColor,
    transparent: true,
    opacity: 0.6,
    side: THREE.DoubleSide,
  });
  const ring = new THREE.Mesh(ringGeo, ringMat);
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.05;
  group.add(ring);

  // Hit Sphere
  const hitGeo = new THREE.SphereGeometry(radius * 2.2, 10, 10);
  const hitMat = new THREE.MeshBasicMaterial({ visible: false });
  const hit = new THREE.Mesh(hitGeo, hitMat);
  hit.position.y = 0.9 + radius;
  hit.userData = { nodeData: data };
  group.add(hit);

  return { group, core, hit, ring };
}

function createMarket3D(data, pos) {
  const group = new THREE.Group();
  group.position.copy(pos);

  // Market Sphere (Sky Blue)
  const coreGeo = new THREE.SphereGeometry(0.85, 16, 16);
  const coreMat = new THREE.MeshStandardMaterial({
    color: THEME_COLORS.market,
    emissive: THEME_COLORS.marketGlow,
    emissiveIntensity: 0.65,
    roughness: 0.3,
    metalness: 0.1,
  });
  const core = new THREE.Mesh(coreGeo, coreMat);
  core.position.y = 0.9;
  core.castShadow = true;
  core.userData = { nodeData: data };
  group.add(core);

  // Ground anchor disk
  const diskGeo = new THREE.CylinderGeometry(0.85, 1.0, 0.25, 16);
  const diskMat = new THREE.MeshStandardMaterial({ color: 0xbae6fd, roughness: 0.4 });
  const disk = new THREE.Mesh(diskGeo, diskMat);
  disk.position.y = 0.12;
  group.add(disk);

  // Pulse ring
  const ringGeo = new THREE.RingGeometry(1.3, 1.6, 24);
  const ringMat = new THREE.MeshBasicMaterial({
    color: THEME_COLORS.marketGlow,
    transparent: true,
    opacity: 0.4,
    side: THREE.DoubleSide,
  });
  const ring = new THREE.Mesh(ringGeo, ringMat);
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.04;
  group.add(ring);

  // Hit Sphere
  const hitGeo = new THREE.SphereGeometry(2.2, 8, 8);
  const hitMat = new THREE.MeshBasicMaterial({ visible: false });
  const hit = new THREE.Mesh(hitGeo, hitMat);
  hit.position.y = 0.9;
  hit.userData = { nodeData: data };
  group.add(hit);

  return { group, core, hit, ring };
}

// ─── Flow Arcs & Photon Streams ─────────────────────────────
function setupFlowArcs(state) {
  while (flowGroup.children.length > 0) flowGroup.remove(flowGroup.children[0]);
  while (pulseGroup.children.length > 0) pulseGroup.remove(pulseGroup.children[0]);
  flowArcs = [];
  photonStreams = [];

  const flowData = getTwin3DFlowsForState(state);

  let colorHex = THEME_COLORS.flowActual;
  if (state === 'optimised') colorHex = THEME_COLORS.flowOptim;
  else if (state === 'recommended') colorHex = THEME_COLORS.flowRecom;

  flowData.forEach(lane => {
    const fromNode = findNode(lane.from);
    const toNode = findNode(lane.to);
    if (!fromNode || !toNode) return;

    const start = geoTo3D(fromNode.lat, fromNode.lng, 1.6);
    const end = geoTo3D(toNode.lat, toNode.lng, 1.6);

    const dist = start.distanceTo(end);
    const apexHeight = Math.max(4.5, Math.min(16.0, dist * 0.32));

    const mid = new THREE.Vector3().addVectors(start, end).multiplyScalar(0.5);
    mid.y = apexHeight;

    const curve = new THREE.QuadraticBezierCurve3(start, mid, end);

    // 3D Flow Tube
    const thickness = Math.max(0.12, Math.min(0.48, (lane.flow / 4000) * 0.4));
    const tubeGeo = new THREE.TubeGeometry(curve, 24, thickness, 6, false);
    const tubeMat = new THREE.MeshStandardMaterial({
      color: colorHex,
      emissive: colorHex,
      emissiveIntensity: 0.3,
      transparent: true,
      opacity: state === 'actual' ? 0.4 : 0.6,
      roughness: 0.3,
    });
    const tube = new THREE.Mesh(tubeGeo, tubeMat);
    flowGroup.add(tube);

    flowArcs.push({ from: lane.from, to: lane.to, curve, tube, data: lane, defaultColor: colorHex });

    // Animated Photon Particle Stream
    const photonCount = Math.max(2, Math.min(6, Math.floor(lane.flow / 1800)));
    const particleGeo = new THREE.BufferGeometry();
    const positions = new Float32Array(photonCount * 3);
    const offsets = [];
    const speeds = [];

    for (let i = 0; i < photonCount; i++) {
      offsets.push(i / photonCount);
      speeds.push(0.16 + Math.random() * 0.08);
      const p = curve.getPoint(offsets[i]);
      positions[i * 3] = p.x;
      positions[i * 3 + 1] = p.y;
      positions[i * 3 + 2] = p.z;
    }

    particleGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));

    const particleMat = new THREE.PointsMaterial({
      color: colorHex,
      size: thickness * 3.5 + 0.8,
      transparent: true,
      opacity: 0.85,
    });

    const particles = new THREE.Points(particleGeo, particleMat);
    pulseGroup.add(particles);

    photonStreams.push({ particles, curve, offsets, speeds, count: photonCount });
  });
}

function getTwin3DFlowsForState(state) {
  if (state === 'actual') return LANES;
  if (state === 'optimised') {
    return LANES.map(l => {
      const m = { ...l };
      if (l.from === 'PLT_BADDI' && l.to === 'DC_DELHI') m.flow = Math.round(l.flow * 0.88);
      if (l.from === 'PLT_BADDI' && l.to === 'DC_KOLKATA') m.flow = Math.round(l.flow * 1.25);
      if (l.from === 'PLT_KOLKATA' && l.to === 'DC_KOLKATA') m.flow = Math.round(l.flow * 1.15);
      return m;
    });
  }
  if (state === 'recommended') {
    return LANES.map(l => {
      const m = { ...l };
      if (l.from === 'PLT_BADDI' && l.to === 'DC_DELHI') m.flow = Math.round(l.flow * 0.82);
      if (l.from === 'PLT_BADDI' && l.to === 'DC_KOLKATA') m.flow = Math.round(l.flow * 1.45);
      if (l.from === 'PLT_KOLKATA' && l.to === 'DC_KOLKATA') m.flow = Math.round(l.flow * 1.30);
      if (l.from === 'DC_KOLKATA' && l.to === 'MKT_KOLKATA') m.flow = Math.round(l.flow * 1.20);
      return m;
    });
  }
  return LANES;
}

function findNode(id) {
  return [...PLANTS, ...DCS, ...MARKETS].find(n => n.id === id);
}

// ─── Orbital Controls (with built-in fallback) ──────────────
/**
 * Minimal built-in orbit controller.
 * Used only when THREE.OrbitControls (CDN) is unavailable, so the 3D twin
 * stays interactive offline / when the CDN is blocked.
 * Implements the same subset of the API this module actually uses.
 */
function createFallbackControls(cam, dom) {
  const spherical = new THREE.Spherical();
  const target = new THREE.Vector3(0, 0, 0);
  const offset = new THREE.Vector3().copy(cam.position).sub(target);
  spherical.setFromVector3(offset);

  let dragging = false;
  let panning = false;
  let px = 0, py = 0;
  let thetaDelta = 0, phiDelta = 0, scaleDelta = 1;
  const panOffset = new THREE.Vector3();

  const api = {
    target,
    enabled: true,
    // See setupControls(): release means stop, in both controllers.
    enableDamping: false,
    dampingFactor: 0.07,
    enablePan: true,
    panSpeed: 0.8,
    rotateSpeed: 0.55,
    minDistance: 25,
    maxDistance: 150,
    maxPolarAngle: Math.PI / 2.2,
    minPolarAngle: Math.PI / 12,
    // Never. See setupControls(): nothing turns this on.
    autoRotate: false,
    autoRotateSpeed: 0,
  };

  function onDown(e) {
    if (!api.enabled) return;
    dragging = true;
    panning = (e.button === 2 || e.button === 1 || e.shiftKey);
    px = e.clientX; py = e.clientY;
    dom.style.cursor = 'grabbing';
  }
  function onMove(e) {
    if (!dragging || !api.enabled) return;
    const dx = e.clientX - px;
    const dy = e.clientY - py;
    px = e.clientX; py = e.clientY;
    const h = dom.clientHeight || 1;
    if (panning && api.enablePan) {
      const dist = spherical.radius * Math.tan((cam.fov / 2) * Math.PI / 180) * 2;
      const vx = new THREE.Vector3().setFromMatrixColumn(cam.matrix, 0);
      const vy = new THREE.Vector3().setFromMatrixColumn(cam.matrix, 1);
      panOffset.add(vx.multiplyScalar(-dx * dist / h * api.panSpeed));
      panOffset.add(vy.multiplyScalar(dy * dist / h * api.panSpeed));
    } else {
      thetaDelta -= 2 * Math.PI * dx / h * api.rotateSpeed;
      phiDelta -= 2 * Math.PI * dy / h * api.rotateSpeed;
    }
  }
  function onUp() {
    dragging = false; panning = false;
    dom.style.cursor = 'grab';
  }
  function onWheel(e) {
    if (!api.enabled) return;
    e.preventDefault();
    scaleDelta *= (e.deltaY > 0) ? 1.08 : 0.92;
  }
  function onCtx(e) { e.preventDefault(); }

  dom.addEventListener('pointerdown', onDown);
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
  dom.addEventListener('wheel', onWheel, { passive: false });
  dom.addEventListener('contextmenu', onCtx);

  api.update = function () {
    if (api.autoRotate && !dragging) {
      thetaDelta -= (2 * Math.PI / 60 / 60) * api.autoRotateSpeed * 12;
    }
    const damp = api.enableDamping ? api.dampingFactor : 1;

    spherical.theta += thetaDelta * damp;
    spherical.phi += phiDelta * damp;
    spherical.phi = Math.max(api.minPolarAngle, Math.min(api.maxPolarAngle, spherical.phi));
    spherical.makeSafe();
    spherical.radius *= 1 + (scaleDelta - 1) * damp;
    spherical.radius = Math.max(api.minDistance, Math.min(api.maxDistance, spherical.radius));

    target.addScaledVector(panOffset, damp);

    const off = new THREE.Vector3().setFromSpherical(spherical);
    cam.position.copy(target).add(off);
    cam.lookAt(target);

    if (api.enableDamping) {
      thetaDelta *= (1 - damp);
      phiDelta *= (1 - damp);
      scaleDelta = 1 + (scaleDelta - 1) * (1 - damp);
      panOffset.multiplyScalar(1 - damp);
    } else {
      thetaDelta = 0; phiDelta = 0; scaleDelta = 1; panOffset.set(0, 0, 0);
    }
    return true;
  };

  api.dispose = function () {
    dom.removeEventListener('pointerdown', onDown);
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    dom.removeEventListener('wheel', onWheel);
    dom.removeEventListener('contextmenu', onCtx);
  };

  return api;
}

function setupControls() {
  const OrbitControlsClass =
    (typeof THREE !== 'undefined' && THREE.OrbitControls) ? THREE.OrbitControls :
    (typeof window !== 'undefined' && window.OrbitControls) ? window.OrbitControls : null;

  if (OrbitControlsClass) {
    controls = new OrbitControlsClass(camera, renderer.domElement);
  } else {
    console.warn('[NetGravity 3D] THREE.OrbitControls unavailable — using built-in fallback controller.');
    controls = createFallbackControls(camera, renderer.domElement);
  }

  // Damping off: release means stop.
  //
  // Inertia is a nice feel on a globe you are browsing and the wrong one on a
  // map you are reading coordinates off — it keeps turning after you let go,
  // so the frame you judged a node's position against is not the frame you
  // left it in. Decay is also asymptotic, never exactly zero, so "it has
  // stopped" was never quite true: at 60fps that is invisible, and on a
  // software renderer at 4fps it drifts for seconds.
  controls.enableDamping = false;
  controls.enablePan = true;
  controls.panSpeed = 0.8;
  controls.rotateSpeed = 0.55;
  controls.minDistance = 25;
  controls.maxDistance = 150;
  controls.maxPolarAngle = Math.PI / 2.2;
  controls.minPolarAngle = Math.PI / 12;
  // The scene does not turn on its own. A map that drifts while you are
  // reading it moves the thing you are pointing at, and every reading of a
  // node's position is taken against a frame that has since moved — so the
  // twin holds still and turns only when dragged.
  controls.autoRotate = false;
  controls.autoRotateSpeed = 0;
  controls.target.set(0, 0, -2);
  // From the first drag or wheel, the view belongs to the reader.
  controls.addEventListener('start', () => { cameraIsUsers = true; });
  // Turning the scene moves the nodes under a stationary cursor.
  controls.addEventListener('change', () => { pointerDirty = true; });
}

// ─── Interaction & HUD Tooltip ──────────────────────────────
function setupInteraction() {
  const canvas = renderer.domElement;

  // Create clean Light-Theme HUD Tooltip
  hudTooltipEl = document.getElementById('twin3d-node-tooltip');
  if (!hudTooltipEl) {
    hudTooltipEl = document.createElement('div');
    hudTooltipEl.id = 'twin3d-node-tooltip';
    hudTooltipEl.className = 'twin3d-node-tooltip hidden';
    containerEl.appendChild(hudTooltipEl);
  }

  const updateMouseCoords = (e) => {
    const rect = canvas.getBoundingClientRect();
    mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    pointerDirty = true;
  };

  canvas.addEventListener('mousemove', updateMouseCoords);

  canvas.addEventListener('click', (e) => {
    updateMouseCoords(e);
    raycaster.setFromCamera(mouse, camera);

    const hitObjects = nodeMeshes.map(n => n.hitMesh || n.coreMesh);
    const intersects = raycaster.intersectObjects(hitObjects, true);

    if (intersects.length > 0) {
      const hitObj = intersects[0].object;
      const data = hitObj.userData?.nodeData;
      if (data && typeof window.openFacilityPanel === 'function') {
        window.openFacilityPanel(data.id);
      }
    }
  });

  // No idle timer re-starting the spin five seconds after you let go. It made
  // the view a moving target the moment you stopped touching it — and the one
  // person it never inconvenienced was whoever was already dragging.
}

function updateHoverState() {
  raycaster.setFromCamera(mouse, camera);
  const hitObjects = nodeMeshes.map(n => n.hitMesh || n.coreMesh);
  const intersects = raycaster.intersectObjects(hitObjects, true);

  if (intersects.length > 0) {
    const hitObj = intersects[0].object;
    const nodeInfo = nodeMeshes.find(n => n.hitMesh === hitObj || n.coreMesh === hitObj);

    if (nodeInfo && nodeInfo !== hoveredNode) {
      if (hoveredNode) resetNodeHighlight(hoveredNode);
      hoveredNode = nodeInfo;
      highlightNodeAndLanes(hoveredNode);
      renderer.domElement.style.cursor = 'pointer';
    }

    if (hoveredNode && hudTooltipEl) {
      const screenPos = hoveredNode.pos3D.clone().add(new THREE.Vector3(0, 3.5, 0)).project(camera);
      const rect = containerEl.getBoundingClientRect();
      const x = (screenPos.x * 0.5 + 0.5) * rect.width;
      const y = (-(screenPos.y * 0.5) + 0.5) * rect.height;

      renderHUDTooltip(hoveredNode.data, hoveredNode.type, x, y);
    }
  } else {
    if (hoveredNode) {
      resetNodeHighlight(hoveredNode);
      hoveredNode = null;
      if (hudTooltipEl) hudTooltipEl.classList.add('hidden');
      renderer.domElement.style.cursor = 'grab';
    }
  }
}

function highlightNodeAndLanes(node) {
  node.coreMesh.scale.setScalar(1.35);
  if (node.baseRing) node.baseRing.scale.setScalar(1.4);

  const nodeId = node.id;
  flowArcs.forEach(fa => {
    if (fa.from === nodeId || fa.to === nodeId) {
      fa.tube.material.opacity = 0.95;
      fa.tube.material.emissiveIntensity = 0.7;
    } else {
      fa.tube.material.opacity = 0.08;
    }
  });
}

function resetNodeHighlight(node) {
  node.coreMesh.scale.setScalar(1.0);
  if (node.baseRing) node.baseRing.scale.setScalar(1.0);

  flowArcs.forEach(fa => {
    fa.tube.material.opacity = twin3dState === 'actual' ? 0.4 : 0.6;
    fa.tube.material.emissiveIntensity = 0.3;
  });
}

function renderHUDTooltip(data, type, x, y) {
  if (!hudTooltipEl) return;

  let typeBadge = '';
  let metricLine = '';

  if (type === 'plant') {
    typeBadge = '<span class="hud-badge plant">Manufacturing Plant</span>';
    metricLine = `
      <div class="hud-row"><span>Throughput:</span><strong>${formatNumber(data.throughput)} ${perPeriodLabel()}</strong></div>
      <div class="hud-row"><span>Total Capacity:</span><strong>${formatNumber(data.capacity)} ${perPeriodLabel()}</strong></div>
    `;
  } else if (type === 'dc') {
    const hasUtil = typeof data.utilPct === 'number' && Number.isFinite(data.utilPct);
    const utilColor = hasUtil ? getUtilColor(data.utilPct) : 'var(--text-3)';
    typeBadge = `<span class="hud-badge dc">Distribution Centre</span>`;
    metricLine = `
      <div class="hud-row"><span>Utilisation:</span><strong style="color:${utilColor}">${hasUtil ? data.utilPct + '%' : '—'}</strong></div>
      <div class="hud-row"><span>Capacity / Flow:</span><strong>${formatNumber(data.throughput)} / ${formatNumber(data.capacity)} ${perPeriodLabel()}</strong></div>
    `;
  } else {
    typeBadge = '<span class="hud-badge market">Demand Market</span>';
    metricLine = `
      <div class="hud-row"><span>Demand:</span><strong>${formatNumber(data.demand)} ${perPeriodLabel()}</strong></div>
      <div class="hud-row"><span>SLA Target:</span><strong>${data.slaDays} Days (${data.priority})</strong></div>
    `;
  }

  hudTooltipEl.innerHTML = `
    <div class="hud-header">
      <div class="hud-title">${data.name || data.city}</div>
      ${typeBadge}
    </div>
    <div class="hud-body">
      ${metricLine}
    </div>
    <div class="hud-footer">Click node to inspect full diagnostics →</div>
  `;

  hudTooltipEl.style.left = `${Math.min(containerEl.clientWidth - 230, Math.max(10, x - 100))}px`;
  hudTooltipEl.style.top = `${Math.max(10, y - 110)}px`;
  hudTooltipEl.classList.remove('hidden');
}

// ─── Animation Loop ─────────────────────────────────────────
function animate() {
  animationId = requestAnimationFrame(animate);

  const delta = clock.getDelta();

  if (controls) controls.update();
  // Only when the pointer has actually moved, or the camera has. A raycast
  // against every node's hit mesh on a still scene under a still cursor can
  // only ever return the answer it returned last frame.
  if (pointerDirty) {
    pointerDirty = false;
    updateHoverState();
  }

  // Animate Photons
  photonStreams.forEach(ps => {
    const posAttr = ps.particles.geometry.attributes.position;
    for (let i = 0; i < ps.count; i++) {
      ps.offsets[i] = (ps.offsets[i] + ps.speeds[i] * delta) % 1;
      const pt = ps.curve.getPoint(ps.offsets[i]);
      posAttr.setXYZ(i, pt.x, pt.y, pt.z);
    }
    posAttr.needsUpdate = true;
  });

  // The base rings used to breathe (sin(t) * 8% scale) and plant cores used to
  // spin on their own axis. Neither encoded anything: a plant turning faster
  // did not mean a plant doing more, and a ring at the top of its pulse did
  // not mean a site under more load. On a screen someone reads figures off,
  // motion that carries no meaning is just something to look away from — so
  // the only thing left moving is the flow along the corridors, which does
  // carry meaning: it shows which way goods travel.

  renderer.render(scene, camera);
}

/**
 * Where the camera is, for a test that needs to ask "did the VIEW move".
 *
 * A pixel diff cannot answer that: the photons crossing the corridors change
 * thousands of pixels a second while the camera sits perfectly still. Read
 * only — nothing in the application calls it.
 */
export function twin3dCameraState() {
  if (!camera) return null;
  return {
    x: +camera.position.x.toFixed(4),
    y: +camera.position.y.toFixed(4),
    z: +camera.position.z.toFixed(4),
  };
}
