/**
 * NetGravity — Scenario Templates
 * ===============================
 * The predefined next steps a planner can actually run, prefilled from the
 * network in front of them.
 *
 * WHY THESE AND NOT THE BRIEFING'S QUESTIONS. `ExecutiveBriefing` carries
 * `suggested_questions`, and on the deterministic path they are two fixed
 * strings — "What is driving this result?", "Which node or lane should I
 * examine?". Rendering those as next steps offers prose where the product has
 * a scenario engine: the user is handed a sentence and left to translate it
 * into a form themselves.
 *
 * So each template below is:
 *
 *   PREDEFINED — a fixed catalogue, every entry mapping to one real
 *                `ScenarioActionType` the builder can submit. Nothing here is
 *                generated, and there is no free text that implies a
 *                capability the engine does not have.
 *   PREFILLED  — its subject and its numbers come from the SOLVED network:
 *                the site actually running hottest, the one actually running
 *                coldest. A template that cannot find its subject in this
 *                network is not offered at all.
 *
 * Nothing here computes a KPI. It reads the solver's own per-facility figures,
 * written by hydrate.js, and sorts on them.
 *
 * TWO FIGURES, NOT ONE, and the difference matters:
 *
 *   `utilPct`     the horizon MEAN. A site at 43% for the year can still be
 *                 out of room in March, so a mean cannot answer whether a
 *                 site is under pressure.
 *   `peakUtilPct` the busiest single period of the solved horizon. This is
 *                 what capacity pressure is judged on. Unset when the solve
 *                 reported no peak — a single-period solve has none — and the
 *                 mean is then the only figure available and is used as such.
 *
 * And `isOpen`: the solver's own decision. A proposed site it declined to
 * open sits at 0% and would otherwise sort coldest, so "test closing this
 * warehouse" would be offered for a warehouse that is not open.
 */

/** A number, or null. Never a coerced zero. */
function num(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Sites the solver actually opened.
 *
 * `isOpen === null` means the solve reported no decision for this site; it is
 * excluded rather than assumed open, because assuming is how a closure test
 * gets offered for something that was never running.
 */
function openSites(facilities) {
  return (facilities || []).filter((f) => f && f.id && f.isOpen === true);
}

/**
 * The figure to judge capacity PRESSURE on: the busiest period, or the mean
 * when the solve reported no peak. Returns {value, isPeak} so the sentence
 * can say which it is rather than implying a peak it does not have.
 */
function pressure(facility) {
  const peak = num(facility.peakUtilPct);
  if (peak !== null) return { value: peak, isPeak: true };
  const mean = num(facility.utilPct);
  return mean === null ? null : { value: mean, isPeak: false };
}

/** Open sites with a solved utilisation, hottest first ON THE GIVEN FIGURE. */
function byUtilisation(facilities, figure) {
  return openSites(facilities)
    .map((f) => ({ facility: f, reading: figure(f) }))
    .filter((r) => r.reading !== null)
    .sort((a, b) => b.reading.value - a.reading.value);
}

/** The mean, for judging how much a site is used over the horizon. */
function meanUse(facility) {
  const mean = num(facility.utilPct);
  return mean === null ? null : { value: mean, isPeak: false };
}

/**
 * The catalogue.
 *
 * `build(ctx)` returns the prefill the builder opens with, or null when this
 * network gives it nothing to act on. `ctx` is { dcs, plants, thresholds }.
 */
export const TEMPLATES = [
  {
    id: 'relieve_hottest',
    action: 'CHANGE_CAPACITY',
    label: 'Add capacity where the network is tightest',
    build: (ctx) => {
      // Judged on the BUSIEST period, not the year's average: a site that
      // averages 60% and hits 98% in its busiest month is under pressure, and
      // a mean is the one figure that cannot say so.
      const top = byUtilisation(ctx.dcs, pressure)[0];
      if (!top) return null;
      const { facility: hottest, reading } = top;
      const over = num(ctx.thresholds && ctx.thresholds.utilization_over_pct) ?? 90;
      if (reading.value < over) return null;
      // A tenth of the site's own capacity, from the network — not a round
      // number this file invented.
      const step = num(hottest.capacity) ? Math.round(hottest.capacity * 0.1) : null;
      if (!step) return null;
      return {
        action: 'CHANGE_CAPACITY',
        name: `More capacity at ${hottest.name}`,
        why: reading.isPeak
          ? `${hottest.name} reaches ${reading.value.toFixed(0)}% of capacity in `
            + `its busiest period — the tightest point in the network.`
          : `${hottest.name} is running at ${reading.value.toFixed(0)}% of `
            + `capacity — the tightest site in the network.`,
        fields: { facility: hottest.id, direction: 'INCREASE', amount: step },
      };
    },
  },
  {
    id: 'consolidate_coldest',
    action: 'CLOSE_FACILITY',
    label: 'Test closing the least-used site',
    build: (ctx) => {
      // Only sites the solver actually OPENED. A proposed warehouse it
      // declined to open sits at 0% and would otherwise sort coldest — and
      // "test closing it" is meaningless for something that is not running.
      const ranked = byUtilisation(ctx.dcs, meanUse);
      if (ranked.length < 2) return null;
      const { facility: coldest, reading } = ranked[ranked.length - 1];
      const under = num(ctx.thresholds && ctx.thresholds.utilization_under_pct) ?? 40;
      if (reading.value > under) return null;
      // Judged on the MEAN here, deliberately: consolidation is about how
      // much a site is used over the horizon, not about its busiest hour.
      // A site that is quiet on average but essential at peak is caught by
      // the peak figure the closure would then have to answer for.
      const peak = num(coldest.peakUtilPct);
      return {
        action: 'CLOSE_FACILITY',
        name: `Close ${coldest.name}`,
        why: `${coldest.name} averages ${reading.value.toFixed(0)}% of capacity`
          + (peak !== null ? ` (${peak.toFixed(0)}% at its busiest)` : '')
          + ` and carries its full fixed cost either way.`,
        fields: { facility: coldest.id },
      };
    },
  },
  {
    id: 'demand_upside',
    action: 'CHANGE_DEMAND',
    label: 'Test demand 10% higher',
    build: () => ({
      action: 'CHANGE_DEMAND',
      name: 'Demand 10% higher',
      why: 'Whether the current footprint carries a busier year, and which '
        + 'site binds first if it does not.',
      // The builder takes a PERCENTAGE in `toolbox-amount`, not a
      // multiplier — one field, whatever the action.
      fields: { amount: 10 },
    }),
  },
  {
    id: 'freight_shock',
    action: 'CHANGE_TRANSPORT_COST',
    label: 'Test freight rates 10% higher',
    build: () => ({
      action: 'CHANGE_TRANSPORT_COST',
      name: 'Freight 10% higher',
      why: 'How much of the plan depends on current rates, and whether a '
        + 'different footprint becomes cheaper when they move.',
      fields: { amount: 10 },
    }),
  },
  {
    id: 'tighter_promise',
    action: 'CHANGE_SLA',
    label: 'Test a one-day tighter delivery promise',
    build: () => ({
      action: 'CHANGE_SLA',
      name: 'Delivery one day tighter',
      why: 'What a shorter promise costs, and which corridors stop qualifying.',
      // Days, signed. Negative tightens the promise.
      fields: { amount: -1 },
    }),
  },
];

/**
 * The templates this network can actually run, most useful first.
 *
 * Bounded to `limit`: the discipline is one clear next step, not a wall of
 * prompts. The evidence-driven templates come first because they name a
 * specific site the solve found, and a suggestion about a real site beats a
 * generic sensitivity test.
 */
export function availableTemplates(ctx, limit = 3) {
  const out = [];
  for (const template of TEMPLATES) {
    if (out.length >= limit) break;
    let prefill = null;
    try {
      prefill = template.build(ctx || {});
    } catch (err) {
      prefill = null;   // a template that cannot read this network is not offered
    }
    if (prefill) out.push({ id: template.id, label: template.label, ...prefill });
  }
  return out;
}
