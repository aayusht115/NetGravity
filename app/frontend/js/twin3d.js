/**
 * NetGravity — 3D Digital Twin (Flat India Map · Light Mode)
 * ==========================================================
 * A flat, real-world basemap of India (extracted directly from the same
 * CARTO/Leaflet tiles used by the 2D map) sits as the ground plane, with the
 * supply chain network rendered as glowing 3D beacons and flight-style flow
 * arcs standing on top of it. Coordinates use the exact Web Mercator
 * projection the basemap was captured with, so every node lines up
 * pixel-accurately with the real geography beneath it.
 */

/* global THREE */
import { PLANTS, DCS, MARKETS, LANES, formatNumber, getUtilColor } from './data.js';
import { INDIA_BASEMAP_DATA_URI } from './basemap-data.js';

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
const cropTopLeftPx = mercatorWorldXY(CROP_BOUNDS.latMax, CROP_BOUNDS.lngMin, MERCATOR_ZOOM);
const cropBottomRightPx = mercatorWorldXY(CROP_BOUNDS.latMin, CROP_BOUNDS.lngMax, MERCATOR_ZOOM);
const IMG_W = cropBottomRightPx.x - cropTopLeftPx.x;
const IMG_H = cropBottomRightPx.y - cropTopLeftPx.y;

// 3D ground-plane size (world units) — matches the basemap's aspect ratio
const MAP_WIDTH = 84;
const MAP_HEIGHT = MAP_WIDTH * (IMG_H / IMG_W);

// ─── Light-theme color palette ───────────────────────────────
const THEME_COLORS = {
  bg:          0xf8fafc, // Slate 50
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
  const u = (worldPx.x - captureTopLeftPx.x - (cropTopLeftPx.x - captureTopLeftPx.x)) / IMG_W;
  const v = (worldPx.y - captureTopLeftPx.y - (cropTopLeftPx.y - captureTopLeftPx.y)) / IMG_H;

  const x = (u - 0.5) * MAP_WIDTH;
  const z = (v - 0.5) * MAP_HEIGHT;

  return new THREE.Vector3(x, height, z);
}

// ─── Public API ─────────────────────────────────────────────
export function initTwin3D(containerId) {
  containerEl = document.getElementById(containerId);
  if (!containerEl) return;

  if (isInitialised) {
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
  setupMapBase();
  setupNetworkNodes();
  setupFlowArcs('actual');
  setupControls();
  setupInteraction();

  isInitialised = true;
  animate();
}

export function setTwin3DState(state) {
  if (state === twin3dState) return;
  twin3dState = state;
  setupFlowArcs(state);
}

export function resizeTwin3D() {
  if (!containerEl || !renderer || !camera) return;
  const w = containerEl.clientWidth || 800;
  const h = containerEl.clientHeight || 560;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}

export function resumeTwin3D() {
  if (!isInitialised) return;
  resizeTwin3D();
  if (!animationId) animate();
}

export function disposeTwin3D() {
  if (animationId) cancelAnimationFrame(animationId);
  animationId = null;
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

// ─── Map Base (real basemap image, extracted from the 2D map) ─────
function setupMapBase() {
  const baseGeo = new THREE.PlaneGeometry(MAP_WIDTH, MAP_HEIGHT);
  
  // Create resilient base material with immediate light-gray fallback
  const baseMat = new THREE.MeshBasicMaterial({
    color: 0xf1f5f9,
    toneMapped: false,
  });
  const basePlate = new THREE.Mesh(baseGeo, baseMat);
  basePlate.rotation.x = -Math.PI / 2;
  basePlate.position.y = 0;
  terrainGroup.add(basePlate);

  // Subtle border trim
  const borderGeo = new THREE.EdgesGeometry(baseGeo);
  const borderMat = new THREE.LineBasicMaterial({ color: 0xcbd5e1 });
  const borderLines = new THREE.LineSegments(borderGeo, borderMat);
  borderLines.rotation.x = -Math.PI / 2;
  borderLines.position.y = 0.02;
  terrainGroup.add(borderLines);

  // Load basemap texture asynchronously and attach once ready
  const loader = new THREE.TextureLoader();
  loader.load(INDIA_BASEMAP_DATA_URI, (texture) => {
    texture.anisotropy = 16;
    texture.generateMipmaps = true;
    baseMat.color.setHex(0xffffff);
    baseMat.map = texture;
    baseMat.needsUpdate = true;
  }, undefined, (err) => {
    console.warn('[NetGravity 3D] Using fallback terrain styling:', err);
  });

  // Soft ground shadow plane underneath, so the map reads as slightly raised
  const shadowGeo = new THREE.PlaneGeometry(MAP_WIDTH * 1.06, MAP_HEIGHT * 1.06);
  const shadowMat = new THREE.MeshBasicMaterial({
    color: 0xcbd5e1,
    transparent: true,
    opacity: 0.5,
  });
  const shadowMesh = new THREE.Mesh(shadowGeo, shadowMat);
  shadowMesh.rotation.x = -Math.PI / 2;
  shadowMesh.position.y = -0.35;
  terrainGroup.add(shadowMesh);
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

  // Utilization Color Coding
  let dcColor = THEME_COLORS.dcHealthy;
  if (data.utilPct >= 90) dcColor = THEME_COLORS.dcCritical;
  else if (data.utilPct >= 75) dcColor = THEME_COLORS.dcWarning;

  const radius = 1.3 + (data.utilPct / 100) * 0.7;

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
    opacity: data.utilPct >= 90 ? 0.45 : 0.25,
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
    enableDamping: true,
    dampingFactor: 0.07,
    enablePan: true,
    panSpeed: 0.8,
    rotateSpeed: 0.55,
    minDistance: 25,
    maxDistance: 150,
    maxPolarAngle: Math.PI / 2.2,
    minPolarAngle: Math.PI / 12,
    autoRotate: false,
    autoRotateSpeed: 0.3,
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

  controls.enableDamping = true;
  controls.dampingFactor = 0.07;
  controls.enablePan = true;
  controls.panSpeed = 0.8;
  controls.rotateSpeed = 0.55;
  controls.minDistance = 25;
  controls.maxDistance = 150;
  controls.maxPolarAngle = Math.PI / 2.2;
  controls.minPolarAngle = Math.PI / 12;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.3;
  controls.target.set(0, 0, -2);
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

  let idleTimer;
  canvas.addEventListener('pointerdown', () => {
    if (controls) controls.autoRotate = false;
    clearTimeout(idleTimer);
  });

  canvas.addEventListener('pointerup', () => {
    idleTimer = setTimeout(() => { if (controls) controls.autoRotate = true; }, 5000);
  });
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
      <div class="hud-row"><span>Daily Throughput:</span><strong>${formatNumber(data.throughput)} u/d</strong></div>
      <div class="hud-row"><span>Total Capacity:</span><strong>${formatNumber(data.capacity)} u/d</strong></div>
    `;
  } else if (type === 'dc') {
    const utilColor = getUtilColor(data.utilPct);
    typeBadge = `<span class="hud-badge dc">Distribution Centre</span>`;
    metricLine = `
      <div class="hud-row"><span>Utilisation:</span><strong style="color:${utilColor}">${data.utilPct}%</strong></div>
      <div class="hud-row"><span>Capacity / Flow:</span><strong>${formatNumber(data.throughput)} / ${formatNumber(data.capacity)} u/d</strong></div>
    `;
  } else {
    typeBadge = '<span class="hud-badge market">Demand Market</span>';
    metricLine = `
      <div class="hud-row"><span>Daily Demand:</span><strong>${formatNumber(data.demand)} u/d</strong></div>
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
  const time = clock.getElapsedTime();

  if (controls) controls.update();
  updateHoverState();

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

  // Pulse Rings & Rotate Plants
  nodeMeshes.forEach((n, i) => {
    if (n.baseRing) {
      const pulseScale = 1.0 + Math.sin(time * 2.5 + i * 0.8) * 0.08;
      n.baseRing.scale.set(pulseScale, pulseScale, 1);
    }
    if (n.type === 'plant') {
      n.coreMesh.rotation.y = time * 0.6;
    }
  });

  renderer.render(scene, camera);
}
