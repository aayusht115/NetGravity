/**
 * Netgravity — Landing Page Controller & Network Visualization
 * ==============================================================
 * Renders the India-inspired dotted background mesh, facility nodes,
 * curved active flow routes, subtle particle flow animations,
 * facility hover interactions, and CTA navigation.
 */

// Facility definitions positioned to perfectly match the reference design
const LANDING_FACILITIES = [
  { id: 'baddi', name: 'Baddi Plant', x: 180, y: 220, type: 'plant', labelPos: { x: 180, y: 182, anchor: 'middle' } },
  { id: 'delhi', name: 'Delhi DC', x: 390, y: 110, type: 'dc', labelPos: { x: 390, y: 70, anchor: 'middle' } },
  { id: 'lucknow', name: 'Lucknow DC', x: 490, y: 270, type: 'dc', labelPos: { x: 542, y: 274, anchor: 'start' } },
  { id: 'ahmedabad', name: 'Ahmedabad DC', x: 235, y: 360, type: 'dc', labelPos: { x: 172, y: 365, anchor: 'end' } },
  { id: 'kolkata', name: 'Kolkata DC', x: 440, y: 360, type: 'dc', labelPos: { x: 495, y: 365, anchor: 'start' } },
  { id: 'mumbai', name: 'Mumbai DC', x: 320, y: 460, type: 'dc', labelPos: { x: 320, y: 502, anchor: 'middle' } },
];

// Network mesh connecting lines
const LANDING_MESH_ROUTES = [
  { from: 'baddi', to: 'delhi' },
  { from: 'baddi', to: 'ahmedabad' },
  { from: 'delhi', to: 'lucknow' },
  { from: 'delhi', to: 'ahmedabad' },
  { from: 'delhi', to: 'kolkata' },
  { from: 'delhi', to: 'mumbai' },
  { from: 'lucknow', to: 'kolkata' },
  { from: 'ahmedabad', to: 'mumbai' },
  { from: 'ahmedabad', to: 'kolkata' },
  { from: 'kolkata', to: 'mumbai' },
];

// Active curved flow paths with animated pulses
const LANDING_FLOW_ROUTES = [
  { id: 'flow-baddi-delhi', from: 'baddi', to: 'delhi', path: 'M 180,220 Q 280,145 390,110', particleSpeed: 3.2 },
  { id: 'flow-baddi-kolkata', from: 'baddi', to: 'kolkata', path: 'M 180,220 Q 300,285 440,360', particleSpeed: 3.8 },
  { id: 'flow-delhi-kolkata-dash', from: 'delhi', to: 'kolkata', path: 'M 390,110 Q 425,240 440,360', dashed: true },
];

// India map silhouette approximation for background dots
function generateIndiaDottedGrid() {
  const dots = [];
  const step = 13;
  const startX = 110, endX = 540;
  const startY = 40, endY = 520;

  function isInsideIndia(x, y) {
    if (y < 100) {
      return x >= 320 && x <= 420 && (y > 55 || (x >= 350 && x <= 400));
    }
    if (y < 220) {
      const leftBound = 170 + (y - 100) * 0.15;
      const rightBound = 420 + (y - 100) * 0.65;
      return x >= leftBound && x <= rightBound;
    }
    if (y < 350) {
      const leftBound = y < 290 ? 140 : 195;
      const rightBound = y < 310 ? 530 : 480;
      return x >= leftBound && x <= rightBound;
    }
    if (y < 520) {
      const progress = (y - 350) / 170;
      const leftBound = 220 + progress * 75;
      const rightBound = 460 - progress * 95;
      return x >= leftBound && x <= rightBound;
    }
    return false;
  }

  for (let y = startY; y <= endY; y += step) {
    for (let x = startX; x <= endX; x += step) {
      if (isInsideIndia(x, y)) {
        const isCore = Math.sin(x * 0.06) * Math.cos(y * 0.06) > 0.15;
        dots.push({ x, y, isCore });
      }
    }
  }

  return dots;
}

/**
 * Initialize Landing Page View and Network Canvas
 */
export function initLandingPage() {
  const landingContainer = document.getElementById('landing-page');
  if (!landingContainer) return;

  const stage = document.getElementById('network-stage');
  if (stage) {
    const existingSvg = stage.querySelector('.network-svg');
    if (!existingSvg) {
      renderNetworkVisualization();
    } else {
      bindNodeHoverEvents();
    }
  }
  
  bindLandingEvents();
}

/**
 * Bind hover events to all network nodes
 */
function bindNodeHoverEvents() {
  document.querySelectorAll('.network-node-group').forEach(nodeG => {
    const nodeId = nodeG.getAttribute('data-node-id');
    if (nodeId) {
      nodeG.addEventListener('mouseenter', () => handleNodeHover(nodeId));
      nodeG.addEventListener('mouseleave', handleNodeLeave);
    }
  });
}

/**
 * Render the Network SVG dynamically if not already in DOM
 */
function renderNetworkVisualization() {
  const stage = document.getElementById('network-stage');
  if (!stage) return;

  const dots = generateIndiaDottedGrid();

  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('viewBox', '0 0 680 560');
  svg.setAttribute('class', 'network-svg');

  // Defs & Filters
  svg.innerHTML = `
    <defs>
      <filter id="node-glow" x="-40%" y="-40%" width="180%" height="180%">
        <feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#9218EA" flood-opacity="0.28"/>
      </filter>
    </defs>
  `;

  // 1. Background Dotted Grid
  const dotsGroup = document.createElementNS(svgNS, 'g');
  dotsGroup.setAttribute('id', 'map-dots-layer');
  dots.forEach(dot => {
    const circle = document.createElementNS(svgNS, 'circle');
    circle.setAttribute('cx', dot.x);
    circle.setAttribute('cy', dot.y);
    circle.setAttribute('r', dot.isCore ? '2.2' : '1.8');
    circle.setAttribute('class', `map-dot ${dot.isCore ? 'core' : ''}`);
    dotsGroup.appendChild(circle);
  });
  svg.appendChild(dotsGroup);

  // 2. Mesh Routes Layer
  const meshGroup = document.createElementNS(svgNS, 'g');
  meshGroup.setAttribute('id', 'mesh-routes-layer');
  LANDING_MESH_ROUTES.forEach(route => {
    const fromNode = LANDING_FACILITIES.find(f => f.id === route.from);
    const toNode = LANDING_FACILITIES.find(f => f.id === route.to);
    if (fromNode && toNode) {
      const line = document.createElementNS(svgNS, 'line');
      line.setAttribute('x1', fromNode.x);
      line.setAttribute('y1', fromNode.y);
      line.setAttribute('x2', toNode.x);
      line.setAttribute('y2', toNode.y);
      line.setAttribute('class', 'route-mesh');
      line.setAttribute('data-from', route.from);
      line.setAttribute('data-to', route.to);
      meshGroup.appendChild(line);
    }
  });
  svg.appendChild(meshGroup);

  // 3. Active Flow Routes Layer
  const flowGroup = document.createElementNS(svgNS, 'g');
  flowGroup.setAttribute('id', 'flow-routes-layer');
  LANDING_FLOW_ROUTES.forEach(flow => {
    if (!flow.dashed) {
      const glowPath = document.createElementNS(svgNS, 'path');
      glowPath.setAttribute('d', flow.path);
      glowPath.setAttribute('class', 'route-active-glow');
      glowPath.setAttribute('data-from', flow.from);
      glowPath.setAttribute('data-to', flow.to);
      flowGroup.appendChild(glowPath);
    }

    const mainPath = document.createElementNS(svgNS, 'path');
    mainPath.setAttribute('id', flow.id);
    mainPath.setAttribute('d', flow.path);
    mainPath.setAttribute('class', flow.dashed ? 'route-dashed' : 'route-active');
    mainPath.setAttribute('data-from', flow.from);
    mainPath.setAttribute('data-to', flow.to);
    flowGroup.appendChild(mainPath);

    if (!flow.dashed) {
      const particle = document.createElementNS(svgNS, 'circle');
      particle.setAttribute('r', '3.5');
      particle.setAttribute('class', 'flow-particle');

      const animateMotion = document.createElementNS(svgNS, 'animateMotion');
      animateMotion.setAttribute('dur', `${flow.particleSpeed || 3.5}s`);
      animateMotion.setAttribute('repeatCount', 'indefinite');
      animateMotion.setAttribute('path', flow.path);
      particle.appendChild(animateMotion);

      flowGroup.appendChild(particle);
    }
  });
  svg.appendChild(flowGroup);

  // 4. Facility Nodes Layer
  const nodesGroup = document.createElementNS(svgNS, 'g');
  nodesGroup.setAttribute('id', 'facility-nodes-layer');

  LANDING_FACILITIES.forEach(facility => {
    const nodeG = document.createElementNS(svgNS, 'g');
    nodeG.setAttribute('class', 'network-node-group node-ambient');
    nodeG.setAttribute('data-node-id', facility.id);
    nodeG.setAttribute('transform-origin', `${facility.x} ${facility.y}`);

    const halo = document.createElementNS(svgNS, 'circle');
    halo.setAttribute('cx', facility.x);
    halo.setAttribute('cy', facility.y);
    halo.setAttribute('r', '20');
    halo.setAttribute('class', 'node-halo');

    const badge = document.createElementNS(svgNS, 'rect');
    badge.setAttribute('x', facility.x - 12);
    badge.setAttribute('y', facility.y - 12);
    badge.setAttribute('width', '24');
    badge.setAttribute('height', '24');
    badge.setAttribute('rx', '7');
    badge.setAttribute('class', 'node-inner-badge');

    const icon = document.createElementNS(svgNS, 'path');
    const ix = facility.x - 6.5;
    const iy = facility.y - 6;
    icon.setAttribute('d', `M ${ix} ${iy+9} L ${ix} ${iy+4} L ${ix+4} ${iy+1.5} L ${ix+4} ${iy+4} L ${ix+8} ${iy+1.5} L ${ix+8} ${iy+4} L ${ix+13} ${iy+0.5} L ${ix+13} ${iy+9} Z`);
    icon.setAttribute('class', 'node-icon-svg');

    const label = document.createElementNS(svgNS, 'text');
    label.setAttribute('x', facility.labelPos.x);
    label.setAttribute('y', facility.labelPos.y);
    label.setAttribute('text-anchor', facility.labelPos.anchor);
    label.setAttribute('class', 'node-label');
    label.textContent = facility.name;

    nodeG.appendChild(halo);
    nodeG.appendChild(badge);
    nodeG.appendChild(icon);
    nodeG.appendChild(label);

    nodeG.addEventListener('mouseenter', () => handleNodeHover(facility.id));
    nodeG.addEventListener('mouseleave', handleNodeLeave);

    nodesGroup.appendChild(nodeG);
  });

  svg.appendChild(nodesGroup);

  stage.innerHTML = '';
  stage.appendChild(svg);
}

/**
 * Handle hover on facility node
 */
function handleNodeHover(activeNodeId) {
  document.querySelectorAll('.network-node-group').forEach(nodeEl => {
    const nodeId = nodeEl.getAttribute('data-node-id');
    if (nodeId === activeNodeId) {
      nodeEl.classList.add('highlighted-node');
      nodeEl.classList.remove('dimmed-node');
      nodeEl.style.transform = 'scale(1.15)';
    } else {
      nodeEl.classList.add('dimmed-node');
      nodeEl.classList.remove('highlighted-node');
      nodeEl.style.transform = 'scale(1)';
    }
  });

  document.querySelectorAll('.route-mesh, .route-active, .route-dashed, .route-active-glow').forEach(routeEl => {
    const from = routeEl.getAttribute('data-from');
    const to = routeEl.getAttribute('data-to');
    if (from === activeNodeId || to === activeNodeId) {
      routeEl.classList.add('highlighted-route');
      routeEl.classList.remove('dimmed-route');
    } else {
      routeEl.classList.add('dimmed-route');
      routeEl.classList.remove('highlighted-route');
    }
  });
}

/**
 * Handle mouse leaving facility node
 */
function handleNodeLeave() {
  document.querySelectorAll('.network-node-group').forEach(nodeEl => {
    nodeEl.classList.remove('highlighted-node', 'dimmed-node');
    nodeEl.style.transform = 'scale(1)';
  });

  document.querySelectorAll('.route-mesh, .route-active, .route-dashed, .route-active-glow').forEach(routeEl => {
    routeEl.classList.remove('highlighted-route', 'dimmed-route');
  });
}

/**
 * Navigate from landing page into main dashboard
 */
export function enterApp(targetTab = 'home') {
  const landing = document.getElementById('landing-page');
  const appShell = document.querySelector('.app-shell');

  if (landing) {
    landing.classList.add('hidden');
    landing.style.display = 'none';
  }

  if (appShell) {
    appShell.style.display = 'flex';
  }

  if (typeof window.navigateToTab === 'function') {
    window.navigateToTab(targetTab);
  }
  if (typeof window.renderHome === 'function') {
    window.renderHome();
  }

  setTimeout(() => {
    window.dispatchEvent(new Event('resize'));
  }, 100);
}

/**
 * Return to landing page view
 */
export function returnToLanding() {
  const landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.remove('hidden');
    landing.style.display = 'flex';
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

// Expose globally on window for inline handlers
if (typeof window !== 'undefined') {
  window.enterApp = enterApp;
  window.returnToLanding = returnToLanding;
}

/**
 * Bind CTA & Interactive Click Events
 */
function bindLandingEvents() {
  const btnGetStarted = document.getElementById('landing-btn-get-started');
  if (btnGetStarted) {
    btnGetStarted.onclick = (e) => {
      e.preventDefault();
      enterApp('home');
    };
  }

  const btnSignIn = document.getElementById('landing-btn-sign-in');
  if (btnSignIn) {
    btnSignIn.onclick = (e) => {
      e.preventDefault();
      enterApp('home');
    };
  }

  const btnViewRec = document.getElementById('landing-btn-view-rec');
  if (btnViewRec) {
    btnViewRec.onclick = (e) => {
      e.preventDefault();
      enterApp('scenarios');
    };
  }

  const sidebarBrand = document.querySelector('.sidebar-brand');
  if (sidebarBrand) {
    sidebarBrand.style.cursor = 'pointer';
    sidebarBrand.setAttribute('title', 'Return to Netgravity Landing Page');
    sidebarBrand.onclick = () => returnToLanding();
  }
}
