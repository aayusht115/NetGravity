/**
 * Netgravity — Landing Page Controller
 * ====================================
 * Exact replication of landinag-updated.png
 * - Pure HTML UI on left (no duplicate background elements, 0 stray letters)
 * - Pristine map digital twin on right
 * - Minimal, elegant ambient digital-twin animation
 * - In-place auth transitions (Sign in / Sign up / Reset)
 */

import { bindPasswordToggles, restorePanelText } from './auth.js';

/* ─── The world, drawn from the outlines the application already ships ───
   `world-basemap.js` carries the Natural Earth 110m set this product uses
   everywhere else — 177 countries, 287 outer rings, about ten thousand
   points. Drawing the hero from it rather than shipping a picture of one
   means real geography at any viewport, and it is why this file is now
   ~150 KB smaller than when it inlined a raster of India. */

import { countryRings } from './world-basemap.js';

/**
 * The hero's frame, and the one liberty it takes.
 *
 * Equirectangular: longitude maps straight to x, latitude straight to y. The
 * frame is 1200 × 625 for a span of 360° × 144°, which stretches the vertical
 * by about 1.3 — the approved design (`Dump/landing-updated-world.png`) is
 * drawn that way, and a true 2.5:1 band would read as a stripe beside a
 * full-height sign-in column.
 *
 * That stretch is why this projection lives here and nowhere else. It is
 * decoration behind a login form; nothing is measured on it, no coordinate a
 * user enters is resolved through it, and the maps that DO carry claims — the
 * digital twin, the scenario map — project honestly from the same data.
 */
const VIEW_W = 1200;
const VIEW_H = 625;
const LAT_TOP = 87;
const LAT_BOTTOM = -57;

function px(lng) {
  return ((lng + 180) / 360) * VIEW_W;
}

function py(lat) {
  return ((LAT_TOP - lat) / (LAT_TOP - LAT_BOTTOM)) * VIEW_H;
}

/**
 * Every landmass as one path.
 *
 * One `<path>` rather than 287: the shape is static, and a single node is
 * cheaper for the browser to keep and for the blur filter beneath it to
 * rasterise.
 *
 * Two rings are left out. Antarctica spans the antimeridian, so drawn flat it
 * is a bar straight across the map; and it sits below the frame anyway. Rings
 * with fewer than four points cannot enclose an area.
 */
let _landPath = null;

function landPath() {
  if (_landPath) return _landPath;
  const out = [];
  countryRings().forEach((ring) => {
    if (!ring || ring.length < 4) return;
    let minLng = 180;
    let maxLng = -180;
    let maxLat = -90;
    for (const [lng, lat] of ring) {
      if (lng < minLng) minLng = lng;
      if (lng > maxLng) maxLng = lng;
      if (lat > maxLat) maxLat = lat;
    }
    // A ring wider than half the globe has wrapped the antimeridian.
    if (maxLng - minLng > 180) return;
    if (maxLat < LAT_BOTTOM) return;

    let d = '';
    for (let i = 0; i < ring.length; i += 1) {
      const x = px(ring[i][0]).toFixed(1);
      const y = py(ring[i][1]).toFixed(1);
      d += `${i === 0 ? 'M' : 'L'}${x} ${y}`;
    }
    out.push(`${d}Z`);
  });
  _landPath = out.join('');
  return _landPath;
}

/**
 * Six hubs, on six continents, at real coordinates.
 *
 * Where the approved design puts a pin, and no more than that: these name
 * places, they do not claim a customer has a facility there.
 */
const HUBS = [
  { id: 'na', name: 'North America', lng: -118.2, lat: 34.1, delay: '0s' },
  { id: 'eu', name: 'Europe', lng: 8.7, lat: 50.1, delay: '0.7s' },
  { id: 'as', name: 'East Asia', lng: 116.4, lat: 39.9, delay: '1.4s' },
  { id: 'af', name: 'Africa', lng: 18.4, lat: -0.7, delay: '2.1s' },
  { id: 'sa', name: 'South America', lng: -46.6, lat: -23.5, delay: '2.8s' },
  { id: 'oc', name: 'Oceania', lng: 151.2, lat: -33.9, delay: '3.5s' },
];

/** The corridors the design draws between them. */
const CORRIDORS = [
  ['na', 'eu', 0.20, '0s'],
  ['na', 'as', 0.30, '1.1s'],
  ['eu', 'af', 0.16, '2.2s'],
  ['af', 'sa', 0.22, '0.6s'],
  ['as', 'oc', 0.20, '1.7s'],
  ['af', 'oc', 0.24, '2.8s'],
  ['sa', 'na', 0.20, '3.4s'],
];

const hubById = (id) => HUBS.find((h) => h.id === id);

/**
 * A corridor, bowed towards the top of the frame.
 *
 * Flat lines between continents read as a wire diagram; the design draws
 * great-circle-looking arcs, and a quadratic bowed along the perpendicular is
 * that shape without pretending to be a geodesic.
 */
function corridorPath(a, b, lift) {
  const x1 = px(a.lng);
  const y1 = py(a.lat);
  const x2 = px(b.lng);
  const y2 = py(b.lat);
  const dx = x2 - x1;
  const dy = y2 - y1;
  const len = Math.hypot(dx, dy) || 1;
  // Perpendicular, always chosen towards the top of the frame so arcs bow the
  // same way and never cross the landmass they are drawn over.
  const nx = -dy / len;
  const ny = dx / len;
  const sign = ny > 0 ? -1 : 1;
  const cx = (x1 + x2) / 2 + nx * len * lift * sign;
  const cy = (y1 + y2) / 2 + ny * len * lift * sign;
  return `M${x1.toFixed(1)} ${y1.toFixed(1)} Q${cx.toFixed(1)} ${cy.toFixed(1)} `
       + `${x2.toFixed(1)} ${y2.toFixed(1)}`;
}

/** The warehouse mark the design puts inside each pin. */
const HUB_GLYPH = 'M-5.6 0.6 0-3.4 5.6 0.6V5.4H-5.6ZM-2.2 5.4V1.9H2.2V5.4';

/**
 * A small, repeatable random source.
 *
 * The scattered dots below have to fall in the same places on every render:
 * a field that reshuffles when the panel re-renders reads as a glitch, and a
 * test cannot say anything about a field that is different every time.
 * mulberry32 with a fixed seed is deterministic across browsers and runs.
 */
function seeded(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** How many scattered points sit behind the map. */
const STAR_COUNT = 132;

/**
 * The scattered field the panels alone did not give.
 *
 * Three rectangles of a dot PATTERN is a regular grid, and a regular grid
 * three times is what made the hero read as a printed slide rather than a
 * live one. These are irregular, individually sized, and each one breathes
 * on its own clock — the movement is what the eye reads as "running", and no
 * single dot is doing anything fast enough to pull attention off the form.
 */
function starMarkup() {
  const rand = seeded(20260905);
  const out = [];
  for (let i = 0; i < STAR_COUNT; i += 1) {
    const x = (rand() * VIEW_W).toFixed(1);
    const y = (rand() * VIEW_H).toFixed(1);
    const r = (0.8 + rand() * 1.7).toFixed(2);
    const dur = (2.6 + rand() * 4.2).toFixed(2);
    const delay = (rand() * 6).toFixed(2);
    const peak = (0.35 + rand() * 0.55).toFixed(2);
    out.push(`<circle class="lw-star" cx="${x}" cy="${y}" r="${r}"`
      + ` style="--lw-star-dur:${dur}s;--lw-star-peak:${peak};animation-delay:${delay}s" />`);
  }
  return out.join('');
}

function hubMarkup() {
  return HUBS.map((h) => {
    const x = px(h.lng).toFixed(1);
    const y = py(h.lat).toFixed(1);
    return `
      <g class="lw-hub" transform="translate(${x} ${y})">
        <circle class="lw-hub-glow" r="27" filter="url(#lw-soft)"
                style="animation-delay: ${h.delay};" />
        <circle class="lw-hub-halo" r="19.5" />
        <circle class="minimal-hub-pulse" r="20" fill="none"
                stroke="rgba(215, 120, 255, 0.75)"
                style="animation-delay: ${h.delay};" />
        <!-- A second ring, slower and wider than the first. One ring reads
             as a blink; two at different speeds read as a signal. -->
        <circle class="minimal-hub-pulse lw-hub-pulse-slow" r="20" fill="none"
                stroke="rgba(236, 190, 255, 0.5)"
                style="animation-delay: calc(${h.delay} + 1.6s);" />
        <circle class="lw-hub-disc" r="13.5" />
        <path class="lw-hub-glyph" d="${HUB_GLYPH}" transform="scale(1.32)" />
      </g>`;
  }).join('');
}

function corridorMarkup() {
  return CORRIDORS.map(([from, to, lift, delay], i) => {
    const a = hubById(from);
    const b = hubById(to);
    if (!a || !b) return '';
    const d = corridorPath(a, b, lift);
    // Three strokes on one geometry: the corridor itself, always there, the
    // glow beneath it, and the pulse that runs along it. The id is what the
    // freight below follows — one path, so a packet cannot drift off its
    // own lane.
    return `
      <path class="lw-corridor-glow" d="${d}" filter="url(#lw-soft)" />
      <path class="lw-corridor" id="lw-c${i}" d="${d}" />
      <path class="minimal-route-beam lw-corridor-beam" d="${d}"
            style="animation-delay: ${delay};" />`;
  }).join('');
}

/**
 * Freight, actually moving along the lanes.
 *
 * The dashed beam already suggests direction, but it is a stroke pattern —
 * it slides, it does not travel. These follow the corridor geometry itself
 * (<mpath> pointing at the path the corridor drew), so a packet leaves one
 * hub and arrives at the other, which is the thing this product does.
 *
 * Two per corridor, half a cycle apart, and the duration is set from the
 * corridor's own length so every packet moves at about the same speed —
 * uniform durations would send the short hops crawling and the long ones
 * sprinting.
 */
function packetMarkup() {
  return CORRIDORS.map(([from, to, lift], i) => {
    const a = hubById(from);
    const b = hubById(to);
    if (!a || !b) return '';
    const span = Math.hypot(px(b.lng) - px(a.lng), py(b.lat) - py(a.lat));
    // The arc is longer than the chord it bows from; ~8% is that difference
    // at the lifts these corridors use.
    const dur = Math.min(13, Math.max(5.5, (span * (1 + lift * 0.5)) / 78));
    return [0, dur / 2].map((begin, k) => `
      <g class="lw-packet">
        <circle class="lw-packet-glow" r="8.2" />
        <circle class="lw-packet-core" r="3.1" />
        <animateMotion dur="${dur.toFixed(2)}s" begin="${begin.toFixed(2)}s"
                       repeatCount="indefinite" calcMode="linear"
                       keyPoints="${k % 2 ? '1;0' : '0;1'}" keyTimes="0;1">
          <mpath href="#lw-c${i}" xlink:href="#lw-c${i}" />
        </animateMotion>
      </g>`).join('');
  }).join('');
}

/**
 * Render the right-hand hero.
 *
 * Layered back to front: the dot fields the design sets behind the map, a
 * blurred copy of the landmass for its bloom, the landmass, its lit coastline,
 * the corridors, then the hubs.
 */
export function renderNetworkVisualization() {
  const stage = document.getElementById('landing-map-stage');
  if (!stage) return;

  const land = landPath();

  stage.innerHTML = `
    <div class="landing-map-container">
      <svg class="landing-world-svg" viewBox="0 0 ${VIEW_W} ${VIEW_H}"
           preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg"
           role="img" aria-label="Logistics corridors across a world map">
        <defs>
          <linearGradient id="lw-land-fill" x1="0" y1="0" x2="0.35" y2="1">
            <stop offset="0%" stop-color="#c398f7" />
            <stop offset="42%" stop-color="#a95ff2" />
            <stop offset="100%" stop-color="#8b1fe3" />
          </linearGradient>
          <radialGradient id="lw-hub-fill" cx="0.35" cy="0.3" r="0.9">
            <stop offset="0%" stop-color="#c98bff" />
            <stop offset="100%" stop-color="#8a15e0" />
          </radialGradient>
          <filter id="lw-haze" x="-14%" y="-14%" width="128%" height="128%">
            <feGaussianBlur stdDeviation="20" />
          </filter>
          <filter id="lw-bloom" x="-12%" y="-12%" width="124%" height="124%">
            <feGaussianBlur stdDeviation="6" />
          </filter>
          <filter id="lw-soft" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="6" />
          </filter>
          <pattern id="lw-dots" width="13" height="13" patternUnits="userSpaceOnUse">
            <circle cx="2" cy="2" r="1.35" />
          </pattern>
          <!-- A finer grade, so the panels are not six copies of one texture. -->
          <pattern id="lw-dots-fine" width="9" height="9" patternUnits="userSpaceOnUse">
            <circle cx="1.6" cy="1.6" r="0.85" />
          </pattern>
        </defs>

        <!-- The panelled fields the design draws, in the open water around
             the continents. Each fades on its own clock, so the corners of
             the frame are never all lit at once. -->
        <g class="lw-dotfields" aria-hidden="true">
          <rect x="1010" y="28" width="176" height="122" fill="url(#lw-dots)" />
          <rect x="1108" y="300" width="86" height="150" fill="url(#lw-dots)"
                style="animation-delay: 1.4s" />
          <rect x="8" y="238" width="72" height="176" fill="url(#lw-dots)"
                style="animation-delay: 2.8s" />
          <rect x="8" y="34" width="104" height="120" fill="url(#lw-dots-fine)"
                style="animation-delay: 4.1s" />
          <rect x="600" y="16" width="150" height="58" fill="url(#lw-dots-fine)"
                style="animation-delay: 2.1s" />
          <rect x="820" y="520" width="180" height="72" fill="url(#lw-dots)"
                style="animation-delay: 3.5s" />
        </g>

        <!-- The scattered field: 96 points, fixed positions, each breathing
             on its own clock. -->
        <g class="lw-stars" aria-hidden="true">${starMarkup()}</g>

        <path class="lw-land-haze" d="${land}" filter="url(#lw-haze)" />
        <path class="lw-land-bloom" d="${land}" filter="url(#lw-bloom)" />
        <path class="lw-land" d="${land}" />
        <path class="lw-coast" d="${land}" />

        <g class="lw-corridors">${corridorMarkup()}</g>
        <g class="lw-packets" aria-hidden="true">${packetMarkup()}</g>
        <g class="lw-hubs">${hubMarkup()}</g>
      </svg>
    </div>
  `;
}

/**
 * Switch Auth Panel in-place on the same page
 */
export function switchAuthPanel(view) {
  const signinPanel = document.getElementById('panel-signin');
  const signupPanel = document.getElementById('panel-signup');
  const resetPanel = document.getElementById('panel-reset');
  const resetForm = document.getElementById('form-panel-reset');
  const resetConf = document.getElementById('panel-reset-confirmation');

  if (signinPanel) signinPanel.classList.remove('active');
  if (signupPanel) signupPanel.classList.remove('active');
  if (resetPanel) resetPanel.classList.remove('active');

  // Leaving a panel takes its failure message with it, and puts back the
  // subtitle of any panel that had been retitled for a step it has now left.
  // A message about the password on a form the user has walked away from
  // explains nothing, and neither does a heading for a form that is gone.
  document.querySelectorAll('.landing-auth-panel .auth-error-box')
    .forEach((box) => { box.hidden = true; });
  [signinPanel, signupPanel, resetPanel].forEach(restorePanelText);

  // A step rendered into the sign-in panel after load — the second-factor
  // prompt — hides the credential form while it is up. Walking away from it
  // abandons the challenge, so the form has to come back with the panel;
  // otherwise sign-in returns as a heading with nothing under it.
  if (signinPanel) {
    signinPanel.querySelector('.auth-mfa-box')?.remove();
    const signinForm = document.getElementById('form-panel-signin');
    if (signinForm) signinForm.style.display = '';
  }

  if (view === 'signup') {
    if (signupPanel) signupPanel.classList.add('active');
  } else if (view === 'reset') {
    if (resetPanel) {
      resetPanel.classList.add('active');
      // Back to asking for an address: the panel's own heading returns with
      // the form, and any step rendered into the panel by a reset link goes.
      resetPanel.classList.remove('is-confirmed');
      resetPanel.querySelector('.auth-reset-complete')?.remove();
      if (resetForm) resetForm.style.display = 'block';
      if (resetConf) resetConf.style.display = 'none';
    }
  } else {
    if (signinPanel) signinPanel.classList.add('active');
  }
}

/**
 * Bind Landing Form Interactions
 */
function bindLandingEvents() {
  if (typeof window !== 'undefined') {
    window.switchAuthPanel = switchAuthPanel;
    window.navigateToAuth = switchAuthPanel;
  }

  // Password visibility toggle. One implementation, in js/auth.js, shared
  // with the steps that are rendered into a panel after load — a second copy
  // here would have to be kept in step with it for no gain.
  bindPasswordToggles(document);

  // Sign In Form Submission — existing user, goes to Select Project
  document.getElementById('form-panel-signin')?.addEventListener('submit', (e) => {
    e.preventDefault();
    if (typeof window.completeAuth === 'function') {
      window.completeAuth('signin');
    }
  });

  // Sign Up Form Submission — new user, goes to Create Project
  document.getElementById('form-panel-signup')?.addEventListener('submit', (e) => {
    e.preventDefault();
    if (typeof window.completeAuth === 'function') {
      window.completeAuth('signup');
    }
  });

  // Reset Password Form Submission — see js/auth.js requestPasswordReset(),
  // bound in bootApp(). There was a second handler here that hid the form and
  // showed "Check your email" the instant the button was pressed, before the
  // request was made and without ever looking at its result: a rejected reset
  // reported success, and the failure message it triggered was written into
  // the form it had just hidden. The one handler that calls the server and
  // reports what the server said is the only one now.
}

/**
 * Initialize Landing Page View
 */
export function initLandingPage() {
  const landingContainer = document.getElementById('landing-page');
  if (!landingContainer) return;

  renderNetworkVisualization();
  bindLandingEvents();
}
