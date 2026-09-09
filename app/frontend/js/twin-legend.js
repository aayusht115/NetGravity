/**
 * NetGravity — Digital Twin legend, and the encoding it describes
 * ===============================================================
 * One definition of what the twin's corridors LOOK like and what the legend
 * SAYS they look like, so the two cannot disagree.
 *
 * The problem this replaces
 * ------------------------
 * Both views sized a corridor by dividing its flow by a constant — `flow/1500`
 * in the 2D map, `flow/4000` in the 3D twin — and clamping the result. Three
 * consequences, all of them visible on a real upload:
 *
 *   * the two views drew the SAME corridor at different weights, because the
 *     divisors differ by a factor of nearly three;
 *   * the constants suit one network. On a network whose largest corridor
 *     carries 4,500 units — the Case-16 network — every lane in the 2D map
 *     lands under weight 3 and the map is a uniform grey web. On one moving
 *     500,000 units, every lane clamps to the maximum and the map is a
 *     uniform thick web. In both cases the thickness is real but carries no
 *     information, because nothing is distinguishable from anything else;
 *   * and neither view's legend mentioned line weight at all, so a reader
 *     seeing thick and thin lines had nothing telling them what the
 *     difference meant.
 *
 * What this does instead
 * ----------------------
 * Four bands, with boundaries derived from the network's own largest
 * corridor rounded to a 1/2/5 figure. Every lane falls in a band, each band
 * has one weight in the 2D map and one radius in the 3D twin, and the legend
 * prints the same four boundaries. A network of hundreds and a network of
 * hundreds of thousands both get four distinguishable weights, and in both
 * the legend says what they mean.
 *
 * §9 — nothing here is an authoritative figure. The band boundaries are a
 * drawing scale, and the only numbers printed are the boundaries themselves,
 * which the legend labels as such. No lane's flow is restated, rounded or
 * aggregated for display; the tooltip still prints `formatNumber(lane.flow)`.
 *
 * What is NOT in this legend, and why
 * -----------------------------------
 * The mockup carries a "Constrained route" key, drawn as a red dashed line.
 * Nothing in this application knows which corridors are constrained: a lane
 * reaches the browser as `{from, to, cost, distance, leadTime, flow, mode}`
 * (see `integration/mappers/twin-mapper.js`) and carries neither its capacity
 * nor any binding-constraint flag from the solve. A legend entry for it would
 * describe a line the map never draws, and a reader who never saw a red lane
 * would reasonably conclude their network has no constrained routes — which
 * is a finding, and one nothing here established.
 */

import { LANES, formatNumber } from './data.js';

/**
 * What each kind of node looks like — the glyph the map draws in it, and the
 * colour it draws it in.
 *
 * THE KEY AND THE MAP HAD THREE PALETTES BETWEEN THEM. The legend keyed a
 * plant in #5b21b6, a DC in #0ea5e9 and a market in #075985; the 2D map drew
 * the same three in #6B2FA0, #2563eb and #0891b2; the 3D twin used a third
 * set. A reader matching a chip against a marker was comparing two different
 * colours, which is the one thing a key must not ask of them.
 *
 * `hex3d` is the same colour as a number, because Three.js materials take one
 * — not a second choice of hue.
 *
 * A DC's colour on the map is the exception, and a deliberate one: its ring
 * (2D) and its body (3D) carry the utilisation band, which the legend's third
 * group keys separately. `color` here is the DC's identity — what its glyph
 * and its fill are drawn in — not its status.
 */
export const NODE_STYLE = {
  plant:  { glyph: '\u{1F3ED}', color: '#6B2FA0', hex3d: 0x6b2fa0,
            label: 'Plant' },
  dc:     { glyph: '\u{1F3EA}', color: '#2563eb', hex3d: 0x2563eb,
            label: 'Distribution Centre' },
  market: { glyph: '\u{1F4E6}', color: '#0891b2', hex3d: 0x0891b2,
            label: 'Demand Market' },
};

/** Line weight in the 2D map, thickest band first. */
const WEIGHT_2D = [6, 4, 2.5, 1.25];

/** Tube radius in the 3D twin, thickest band first. */
const RADIUS_3D = [0.42, 0.30, 0.20, 0.12];

/**
 * The next 1, 2 or 5 × 10^k at or above `value`.
 *
 * Band boundaries a reader can hold in their head — 100,000 rather than
 * 97,431 — which is the whole reason to band rather than to print a
 * continuous ramp with the maximum at one end.
 */
function niceCeiling(value) {
  if (!(value > 0)) return 0;
  const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
  const scaled = value / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
}

/**
 * The four flow bands for the network currently loaded.
 *
 * Returns `null` when no corridor carries a flow figure — an unsolved
 * network, or one whose lanes the solve reported without volumes. The legend
 * then omits the whole section rather than showing four bands of nothing, and
 * every lane draws at the thinnest weight.
 */
export function flowBands() {
  const flows = (LANES || [])
    .map((l) => Number(l && l.flow))
    .filter((f) => Number.isFinite(f) && f > 0);
  if (!flows.length) return null;

  const top = niceCeiling(Math.max(...flows));
  if (!(top > 0)) return null;

  // Halves and fifths of the rounded maximum, which keeps every boundary a
  // round number for any top of the form 1/2/5 × 10^k.
  const cuts = [top / 2, top / 5, top / 20];
  return [
    { min: cuts[0], max: null,    weight2d: WEIGHT_2D[0], radius3d: RADIUS_3D[0] },
    { min: cuts[1], max: cuts[0], weight2d: WEIGHT_2D[1], radius3d: RADIUS_3D[1] },
    { min: cuts[2], max: cuts[1], weight2d: WEIGHT_2D[2], radius3d: RADIUS_3D[2] },
    { min: 0,       max: cuts[2], weight2d: WEIGHT_2D[3], radius3d: RADIUS_3D[3] },
  ];
}

/** The band one flow falls in, or the thinnest when there are no bands. */
export function bandForFlow(flow, bands) {
  const value = Number(flow);
  const set = bands || flowBands();
  if (!set) return { weight2d: WEIGHT_2D[3], radius3d: RADIUS_3D[3] };
  if (!Number.isFinite(value) || value <= 0) return set[set.length - 1];
  return set.find((b) => value >= b.min) || set[set.length - 1];
}

/** "≥ 100,000" / "10,000 – 50,000" / "< 10,000", in the engine's formatting. */
function bandLabel(band) {
  if (band.max === null) return `≥ ${formatNumber(Math.round(band.min))}`;
  if (band.min === 0) return `< ${formatNumber(Math.round(band.max))}`;
  return `${formatNumber(Math.round(band.min))} – ${formatNumber(Math.round(band.max))}`;
}

/**
 * Facility counts, filled by `renderMapLegendCounts` from PLANTS/DCS/MARKETS.
 *
 * The row carries `data-legend-count` rather than a number written here, so
 * there is one rule for what "how many DCs" means and no view can disagree
 * with another about it.
 */
function facilityRow(kind) {
  const style = NODE_STYLE[kind];
  return `
    <div class="tw-legend-row">
      <span class="tw-legend-glyph"
            style="background:${style.color}1f;border-color:${style.color}59"
            aria-hidden="true">${style.glyph}</span>
      <span class="tw-legend-label">${style.label}</span>
      <span class="tw-legend-count" data-legend-count="${kind}">0</span>
    </div>`;
}

/**
 * The whole legend, for either view.
 *
 * Three groups, in the order a reader needs them: what the shapes are, what
 * the line thickness means, and what the ring colour means. Each group has a
 * heading naming the question it answers, rather than the previous version's
 * bare "Network" over a list of three dots (Nielsen #6 — the key should be
 * readable without knowing in advance what is keyed).
 *
 * `perPeriod` is the engine's own period wording ("units/month"), passed in
 * rather than imported so this module has no opinion about the horizon.
 */
export function twinLegendHtml(perPeriod = 'units/period') {
  const bands = flowBands();

  const flowSection = bands ? `
    <div class="tw-legend-group">
      <div class="tw-legend-title">Flow volume
        <span class="tw-legend-unit">(${perPeriod})</span></div>
      ${bands.map((b) => `
        <div class="tw-legend-row">
          <span class="tw-legend-line" style="height:${b.weight2d}px"
                aria-hidden="true"></span>
          <span class="tw-legend-label">${bandLabel(b)}</span>
        </div>`).join('')}
    </div>` : `
    <div class="tw-legend-group">
      <div class="tw-legend-title">Flow volume</div>
      <div class="tw-legend-note">No corridor in this network carries a volume
        yet, so every line is drawn at one weight.</div>
    </div>`;

  return `
    <div class="tw-legend-group">
      <div class="tw-legend-title">Facilities</div>
      ${facilityRow('plant')}
      ${facilityRow('dc')}
      ${facilityRow('market')}
    </div>
    ${flowSection}
    <div class="tw-legend-group">
      <div class="tw-legend-title">Distribution centre load
        <span class="tw-legend-unit">(its ring, and its colour in 3D)</span></div>
      <div class="tw-legend-row">
        <span class="tw-legend-ring" style="border-color:#dc2626" aria-hidden="true"></span>
        <span class="tw-legend-label">Critical &mdash; above 95%</span>
      </div>
      <div class="tw-legend-row">
        <span class="tw-legend-ring" style="border-color:#f59e0b" aria-hidden="true"></span>
        <span class="tw-legend-label">Stress &mdash; 85% to 95%</span>
      </div>
      <div class="tw-legend-row">
        <span class="tw-legend-ring" style="border-color:#22c55e" aria-hidden="true"></span>
        <span class="tw-legend-label">Healthy &mdash; below 85%</span>
      </div>
    </div>`;
}

/**
 * "F006 · Brampton National Distribution Hub" — the full identity.
 *
 * The id first, because that is the column the reader's own workbook is keyed
 * on and the string they will search it for. Names repeat across a network —
 * two "Central DC" rows are ordinary — and the id is the thing that does not.
 *
 * Falls back to whichever of the two exists, and never prints the id twice
 * for a network whose facilities are named by their id.
 */
export function facilityLabel(node) {
  if (!node) return '';
  const id = String(node.id || '').trim();
  const name = String(node.name || '').trim();
  if (!id) return name;
  if (!name || name === id) return id;
  return `${id} · ${name}`;
}

/**
 * The same identity, short enough to sit on the map.
 *
 * The full name is right in a tooltip and wrong on a pin. Drawn in full, the
 * Canadian network put twenty-six labels like "F025 · Regina Global
 * Transportation Hub Candidate DC" over each other in the middle of the
 * canvas — every one of them legible on its own and none of them readable
 * together, which is worse than the hover-only labels it replaced.
 *
 * The CITY is preferred to the name, because that is what distinguishes two
 * sites at a glance and it is what the mockup labels each node with; the name
 * is used, trimmed, where the upload states no city. The id is never dropped:
 * it is the short half and the half that is unique.
 */
export function facilityShortLabel(node, maxName = 18) {
  if (!node) return '';
  const id = String(node.id || '').trim();
  const city = String(node.city || '').trim();
  const name = String(node.name || '').trim();

  let tail = city || name;
  if (tail === id) tail = '';
  if (tail.length > maxName) tail = `${tail.slice(0, maxName - 1).trimEnd()}\u2026`;

  if (!id) return tail;
  return tail ? `${id} · ${tail}` : id;
}
