/**
 * NetGravity — 3D Digital Twin (India Map Base · Light Mode)
 * ==========================================================
 * Executive-grade 3D spatial network visualization rendered in clean light theme.
 * Features:
 *   - High-resolution dynamic 3D India Map base plate with authentic coastlines & state boundaries
 *   - Precise geographic coordinate projection for all 19 supply chain nodes (Plants, DCs, Markets)
 *   - Distinct 3D mesh representations (Hex Beacons, Tiered Hubs, Pulse Nodes)
 *   - 3D flight arcs with animated photon pulses
 *   - Interactive hover HUD tooltips and direct facility panel integration
 */

/* global THREE */
import { PLANTS, DCS, MARKETS, LANES, formatNumber, getUtilColor } from './data.js';

// ─── Geographic Bounds & Dimensions ─────────────────────────
const GEO_BOUNDS = {
  latMin: 6.5,
  latMax: 37.0,
  lngMin: 67.5,
  lngMax: 97.5,
};

const MAP_WIDTH = 80;
const MAP_HEIGHT = 80 * ((GEO_BOUNDS.latMax - GEO_BOUNDS.latMin) / (GEO_BOUNDS.lngMax - GEO_BOUNDS.lngMin)); // ~81.3

// Light-theme color palette
const THEME_COLORS = {
  bg:          0xf8fafc, // Slate 50
  landFill:    '#ffffff',
  landBorder:  '#94a3b8',
  oceanFill:   '#f1f5f9',
  gridLine:    '#e2e8f0',
  textSubtle:  '#94a3b8',
  plant:       0x7c3aed, // Kearney Purple
  plantGlow:   0xa855f7,
  dcHealthy:   0x10b981, // Emerald
  dcWarning:   0xf59e0b, // Amber
  dcCritical:  0xef4444, // Red
  market:      0x0284c7, // Sky Blue
  marketGlow:  0x38bdf8,
  flowActual:  0x64748b, // Slate 500
  flowOptim:   0x7c3aed, // Purple
  flowRecom:   0x059669, // Emerald
};

// ─── Module State ───────────────────────────────────────────
let scene, camera, renderer, controls;
let containerEl = null;
let animationId = null;
let isInitialised = false;
let currentState = 'actual';

let nodeGroup, flowGroup, pulseGroup, terrainGroup;
let nodeMeshes = [];      // { id, type, coreMesh, hitMesh, baseRing, data, pos3D }
let flowArcs = [];        // { from, to, curve, line, tube, data }
let photonStreams = [];   // { particles, curve, offsets, speeds, count }

let raycaster, mouse;
let hoveredNode = null;
let clock;
let hudTooltipEl = null;

// ─── Coordinate Conversion ──────────────────────────────────
export function geoTo3D(lat, lng, height = 0) {
  const normX = (lng - GEO_BOUNDS.lngMin) / (GEO_BOUNDS.lngMax - GEO_BOUNDS.lngMin);
  const normZ = (lat - GEO_BOUNDS.latMin) / (GEO_BOUNDS.latMax - GEO_BOUNDS.latMin);

  const x = (normX - 0.5) * MAP_WIDTH;
  const z = -(normZ - 0.5) * MAP_HEIGHT;

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

  clock = new THREE.Clock();
  raycaster = new THREE.Raycaster();
  mouse = new THREE.Vector2(-999, -999);

  setupScene();
  setupLighting();
  setupIndiaMapBase();
  setupNetworkNodes();
  setupFlowArcs('actual');
  setupControls();
  setupInteraction();

  isInitialised = true;
  animate();
}

export function setTwin3DState(state) {
  if (state === currentState) return;
  currentState = state;
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
  scene.fog = new THREE.FogExp2(THEME_COLORS.bg, 0.005);

  // Isometric viewpoint focused on central India
  camera = new THREE.PerspectiveCamera(40, w / h, 1, 400);
  camera.position.set(0, 75, 62);
  camera.lookAt(0, 0, -2);

  // WebGL Renderer
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
  renderer.setSize(w, h);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;

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
  // Soft, bright studio ambient light
  const ambient = new THREE.AmbientLight(0xffffff, 1.4);
  scene.add(ambient);

  // Main sun light casting crisp directional shadows
  const mainSun = new THREE.DirectionalLight(0xffffff, 1.8);
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

  // Cool fill light from opposite angle
  const fillLight = new THREE.DirectionalLight(0xe2e8f0, 0.8);
  fillLight.position.set(-45, 50, -45);
  scene.add(fillLight);

  // Soft purple accent rim light
  const accentLight = new THREE.DirectionalLight(0xddd6fe, 0.5);
  accentLight.position.set(0, 30, -50);
  scene.add(accentLight);
}

// ─── India Map Base (High-Resolution Canvas Texture) ────────
function setupIndiaMapBase() {
  // Create 2048x2048 dynamic canvas for the India base map
  const canvas = document.createElement('canvas');
  canvas.width = 2048;
  canvas.height = 2048;
  const ctx = canvas.getContext('2d');

  renderIndiaCanvasMap(ctx, canvas.width, canvas.height);

  const mapTexture = new THREE.CanvasTexture(canvas);
  mapTexture.anisotropy = 16;

  // 3D Base Plate Geometry
  const baseGeo = new THREE.BoxGeometry(MAP_WIDTH * 1.15, 1.2, MAP_HEIGHT * 1.15);
  const baseMat = new THREE.MeshStandardMaterial({
    map: mapTexture,
    roughness: 0.7,
    metalness: 0.05,
  });

  const basePlate = new THREE.Mesh(baseGeo, baseMat);
  basePlate.position.set(0, -0.6, 0);
  basePlate.receiveShadow = true;
  terrainGroup.add(basePlate);

  // Subtle beveled border trim around base plate
  const borderGeo = new THREE.EdgesGeometry(baseGeo);
  const borderMat = new THREE.LineBasicMaterial({ color: 0xcbd5e1, linewidth: 1 });
  const borderLines = new THREE.LineSegments(borderGeo, borderMat);
  borderLines.position.copy(basePlate.position);
  terrainGroup.add(borderLines);

  // Soft ground shadow plane underneath base plate
  const shadowGeo = new THREE.PlaneGeometry(MAP_WIDTH * 1.4, MAP_HEIGHT * 1.4);
  const shadowMat = new THREE.MeshBasicMaterial({
    color: 0xe2e8f0,
    transparent: true,
    opacity: 0.6,
  });
  const shadowMesh = new THREE.Mesh(shadowGeo, shadowMat);
  shadowMesh.rotation.x = -Math.PI / 2;
  shadowMesh.position.y = -1.25;
  terrainGroup.add(shadowMesh);
}

// Draw accurate, beautiful light-mode vector cartography of India
function renderIndiaCanvasMap(ctx, w, h) {
  // 1. Ocean Background
  ctx.fillStyle = '#f1f5f9'; // Slate 100
  ctx.fillRect(0, 0, w, h);

  // Lat/Lng to Canvas XY helper
  const toXY = (lat, lng) => {
    const normX = (lng - GEO_BOUNDS.lngMin) / (GEO_BOUNDS.lngMax - GEO_BOUNDS.lngMin);
    const normY = (lat - GEO_BOUNDS.latMin) / (GEO_BOUNDS.latMax - GEO_BOUNDS.latMin);
    return {
      x: normX * w,
      y: (1 - normY) * h,
    };
  };

  // 2. Latitude / Longitude Cartographic Grid
  ctx.strokeStyle = '#e2e8f0';
  ctx.lineWidth = 2;
  ctx.setLineDash([6, 6]);

  for (let lat = 10; lat <= 35; lat += 5) {
    const p1 = toXY(lat, GEO_BOUNDS.lngMin);
    const p2 = toXY(lat, GEO_BOUNDS.lngMax);
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();

    // Lat label
    ctx.fillStyle = '#94a3b8';
    ctx.font = 'bold 22px Inter, sans-serif';
    ctx.fillText(`${lat}°N`, 25, p1.y - 6);
  }

  for (let lng = 70; lng <= 95; lng += 5) {
    const p1 = toXY(GEO_BOUNDS.latMin, lng);
    const p2 = toXY(GEO_BOUNDS.latMax, lng);
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();

    // Lng label
    ctx.fillStyle = '#94a3b8';
    ctx.font = 'bold 22px Inter, sans-serif';
    ctx.fillText(`${lng}°E`, p1.x + 6, h - 25);
  }

  ctx.setLineDash([]); // Reset dash

  // 3. Indian Subcontinent Geographic Polygon Contour
  // High-fidelity polygon tracing India's coastal & land boundaries
  const indiaPolygon = [
    [35.5, 74.8], [34.8, 76.5], [33.2, 77.8], [32.0, 78.8], [30.4, 79.5],
    [30.2, 80.8], [28.8, 80.2], [27.4, 83.5], [26.8, 88.0], [27.2, 88.8],
    [27.8, 89.8], [28.2, 92.5], [28.5, 94.5], [28.0, 96.5], [27.2, 97.2],
    [25.5, 95.2], [24.0, 93.5], [23.5, 92.8], [22.0, 92.2], [22.5, 90.0],
    [21.8, 89.0], [21.5, 87.2], [20.5, 86.8], [19.8, 85.8], [18.2, 84.0],
    [17.0, 82.3], [16.0, 81.0], [15.2, 80.2], [13.2, 80.3], [11.8, 79.8],
    [10.2, 79.8], [9.2, 79.2],  [8.1, 77.5],  [8.8, 76.6],  [10.0, 75.8],
    [11.5, 75.8], [12.8, 74.8], [14.5, 74.2], [15.5, 73.8], [17.5, 73.3],
    [18.9, 72.8], [20.5, 72.7], [21.2, 72.5], [21.0, 70.0], [21.8, 69.2],
    [22.4, 69.0], [23.0, 68.4], [23.8, 68.6], [24.5, 71.0], [25.5, 70.2],
    [27.0, 70.8], [28.5, 71.8], [30.0, 73.2], [32.0, 74.8], [34.0, 74.2],
    [35.5, 74.8]
  ];

  ctx.beginPath();
  indiaPolygon.forEach((pt, i) => {
    const xy = toXY(pt[0], pt[1]);
    if (i === 0) ctx.moveTo(xy.x, xy.y);
    else ctx.lineTo(xy.x, xy.y);
  });
  ctx.closePath();

  // Fill landmass in crisp white with soft drop shadow
  ctx.fillStyle = '#ffffff';
  ctx.shadowColor = 'rgba(15, 23, 42, 0.08)';
  ctx.shadowBlur = 24;
  ctx.shadowOffsetX = 4;
  ctx.shadowOffsetY = 8;
  ctx.fill();

  // Reset shadow
  ctx.shadowColor = 'transparent';
  ctx.shadowBlur = 0;
  ctx.shadowOffsetX = 0;
  ctx.shadowOffsetY = 0;

  // Land boundary stroke
  ctx.strokeStyle = '#94a3b8';
  ctx.lineWidth = 4;
  ctx.stroke();

  // 4. Subtle Internal State Divider Lines
  const internalLines = [
    // North / West / Central lines
    [[30.0, 75.0], [28.5, 77.0], [26.5, 78.0], [24.0, 79.0]],
    [[24.5, 71.0], [23.5, 74.5], [22.0, 76.0], [21.0, 79.0]],
    // Deccan & South
    [[20.0, 73.5], [19.0, 77.0], [17.5, 80.5], [18.0, 84.0]],
    [[16.0, 74.5], [15.0, 77.5], [14.0, 80.0]],
    [[12.5, 75.0], [12.0, 78.0], [11.0, 79.8]],
    // East & Northeast
    [[26.5, 80.0], [25.0, 84.0], [24.0, 87.5], [22.5, 88.5]],
    [[26.8, 88.0], [26.0, 90.0], [26.2, 92.5], [27.0, 95.0]],
  ];

  ctx.strokeStyle = '#cbd5e1';
  ctx.lineWidth = 2;
  ctx.setLineDash([4, 4]);
  internalLines.forEach(line => {
    ctx.beginPath();
    line.forEach((pt, i) => {
      const xy = toXY(pt[0], pt[1]);
      if (i === 0) ctx.moveTo(xy.x, xy.y);
      else ctx.lineTo(xy.x, xy.y);
    });
    ctx.stroke();
  });
  ctx.setLineDash([]);

  // 5. Geographic Watermark Labels
  ctx.fillStyle = '#94a3b8';
  ctx.font = 'bold 36px Inter, sans-serif';
  ctx.textAlign = 'center';

  const sea1 = toXY(15.0, 70.0);
  ctx.fillText('ARABIAN SEA', sea1.x, sea1.y);

  const sea2 = toXY(15.0, 87.0);
  ctx.fillText('BAY OF BENGAL', sea2.x, sea2.y);

  const sea3 = toXY(7.5, 78.5);
  ctx.fillText('INDIAN OCEAN', sea3.x, sea3.y);

  // Region Watermarks
  ctx.fillStyle = '#e2e8f0';
  ctx.font = '800 48px Inter, sans-serif';
  const rNorth = toXY(29.5, 77.0);
  ctx.fillText('NORTH', rNorth.x, rNorth.y);

  const rWest = toXY(21.5, 74.0);
  ctx.fillText('WEST', rWest.x, rWest.y);

  const rSouth = toXY(14.5, 77.5);
  ctx.fillText('SOUTH', rSouth.x, rSouth.y);

  const rEast = toXY(23.5, 86.0);
  ctx.fillText('EAST', rEast.x, rEast.y);

  // 6. City Target Circles on Map Surface
  [...PLANTS, ...DCS, ...MARKETS].forEach(n => {
    const xy = toXY(n.lat, n.lng);

    // Subtle outer anchor ring
    ctx.strokeStyle = '#cbd5e1';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(xy.x, xy.y, 14, 0, Math.PI * 2);
    ctx.stroke();

    // Center point
    ctx.fillStyle = '#64748b';
    ctx.beginPath();
    ctx.arc(xy.x, xy.y, 3, 0, Math.PI * 2);
    ctx.fill();

    // City Name
    ctx.fillStyle = '#475569';
    ctx.font = '600 20px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(n.city || n.name, xy.x, xy.y + 30);
  });
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
    emissiveIntensity: 0.6,
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
    emissiveIntensity: 0.65,
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
    emissiveIntensity: 0.5,
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

  const flowData = getFlowsForState(state);

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
      emissiveIntensity: 0.25,
      transparent: true,
      opacity: state === 'actual' ? 0.35 : 0.55,
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

function getFlowsForState(state) {
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

// ─── Orbital Controls ───────────────────────────────────────
function setupControls() {
  const OrbitControlsClass = (typeof THREE !== 'undefined' && THREE.OrbitControls) ? THREE.OrbitControls : (typeof window !== 'undefined' ? window.OrbitControls : null);
  if (!OrbitControlsClass) {
    console.warn('OrbitControls not found.');
    return;
  }

  controls = new OrbitControlsClass(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.07;
  controls.enablePan = true;
  controls.panSpeed = 0.8;
  controls.rotateSpeed = 0.55;
  controls.minDistance = 25;
  controls.maxDistance = 140;
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
    controls.autoRotate = false;
    clearTimeout(idleTimer);
  });

  canvas.addEventListener('pointerup', () => {
    idleTimer = setTimeout(() => { controls.autoRotate = true; }, 5000);
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
    fa.tube.material.opacity = currentState === 'actual' ? 0.35 : 0.55;
    fa.tube.material.emissiveIntensity = 0.25;
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

  controls.update();
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
