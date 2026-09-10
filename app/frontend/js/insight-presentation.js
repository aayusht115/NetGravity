/**
 * NetGravity — Insight Presentation
 * ==================================
 * The two decisions the Overview's tiles and the deep-dive page both have to
 * make about a finding, in one place because they must agree.
 *
 *   `insightDescription()`  what the finding says, minus what its headline
 *                           already said
 *   `insightCta()`          which screen its recommended action is asking
 *                           the reader to open
 *
 * Both used to live in app.js, where the tiles could reach them and the deep
 * dive could not — app.js imports insight-detail.js, so the dependency
 * cannot run the other way. So the deep dive had a generic "Test a change as
 * a scenario" button on every finding while the tile that opened it offered
 * something else entirely, and the two screens disagreed about what to do
 * next about the same finding.
 *
 * Nothing here composes prose or derives a figure. §9 — a screen that writes
 * its own recommendation is a second, unverified reasoning agent. The
 * sentence is always the engine's; this module only decides where the button
 * beside it goes.
 */

import { DEMAND_SHORTFALL } from './data.js';

/**
 * Where a finding's recommended action actually leads.
 *
 * The ACTION is the engine's — one sentence, written by the Reasoning Agent
 * or supplied by `/api/insights` when the briefing wrote none. This map is
 * only the destination: which of this product's screens that sentence is
 * asking the reader to open. It is deliberately here rather than on the
 * server, because which screens exist is the client's knowledge, and a
 * backend naming a tab is a backend that breaks when a tab is renamed.
 *
 * Keyed by the engine's own `theme`, so a new theme falls to the default
 * rather than to a wrong screen.
 *
 * NO ENTRY MAY POINT AT THE KPI SCREEN. A reader sent there to check a
 * recommendation has been handed the analysis rather than the decision, and
 * the recommendation above the button has been made twice — once by the
 * engine, once by the reader. Every change this product recommends is proved
 * by pricing it as a scenario.
 */
export const INSIGHT_CTA = {
  'Service':              { label: 'Open scenario planner', tab: 'scenarios' },
  // THESE FIVE READ "Open KPIs" AND WENT TO THE KPI SCREEN.
  //
  // Coherent while the sentence above the button described a finding. It is a
  // RECOMMENDATION now — "Expand capacity at Western Distribution Centre",
  // "Test consolidating Eastern Distribution Centre" — and under a
  // recommendation, "Open KPIs" tells the reader to go and do the analysis
  // themselves. That is the same defect the recommendation wording was
  // rewritten to remove, left standing in the control beside it.
  //
  // A recommendation is proved by pricing it, and the planner is where it is
  // priced. Nothing here sends a reader to a dashboard to validate a change.
  'Capacity':             { label: 'Open scenario planner', tab: 'scenarios' },
  'Utilisation':          { label: 'Open scenario planner', tab: 'scenarios' },
  'Footprint':            { label: 'Open scenario planner', tab: 'scenarios' },
  // Not the planner, and not a dashboard either: losing a site is a question
  // about the network's shape, and the twin is where that is read.
  'Resilience':           { label: 'Open Digital Twin', tab: 'twin' },
  'Cost':                 { label: 'Open scenario planner', tab: 'scenarios' },
  'Cost structure':       { label: 'Open scenario planner', tab: 'scenarios' },
  'Carbon':               { label: 'Open scenario planner', tab: 'scenarios' },
  'Scenario impact':      { label: 'Open scenario planner', tab: 'scenarios' },
  'Demand outlook':       { label: 'Open forecast', tab: 'forecast' },
  'Where the growth is':  { label: 'Open forecast', tab: 'forecast' },
  'External signals':     { label: 'Open forecast', tab: 'forecast' },
  'History that changed': { label: 'Open forecast', tab: 'forecast' },
};

export const INSIGHT_CTA_DEFAULT = { label: 'Open scenario planner', tab: 'scenarios' };

/**
 * The destination for one finding's action.
 *
 * `tab: ''` is the shortfall case and means "not a tab" — the caller opens
 * the demand drawer instead. It is the ONE override, and it is not a special
 * case so much as a better destination that only exists for one finding:
 * when the engine relaxed the model it attached the demand it could not
 * serve MARKET BY MARKET, with its own reason and the sites it opened to get
 * that far. That breakdown is what a reader of an unserved-demand finding
 * actually wants, and the scenario planner is not where it is.
 *
 * Guarded on the breakdown being there: without rows the drawer would say
 * "no market breakdown travelled with this run", which is a worse answer
 * than sending the reader to the planner.
 */
export function insightCta(theme, severity) {
  const hasShortfall = theme === 'Service' && severity === 'RISK'
    && (DEMAND_SHORTFALL.shortMarkets || []).length > 0;
  if (hasShortfall) return { label: 'View affected demand', tab: '' };
  return INSIGHT_CTA[theme] || INSIGHT_CTA_DEFAULT;
}

/**
 * The finding explained, without saying the headline twice.
 *
 * The engine writes a narrative of two or three sentences and a headline that
 * is usually ONE OF THEM. Printing both put the same sentence on the tile
 * twice — "No open site reaches the 90% threshold, so capacity is not what
 * limits this plan" as the heading, and again as the first line under it.
 *
 * So the description is the narrative MINUS whatever the headline already
 * says: sentences are compared with punctuation and case removed, and one is
 * dropped when either string contains the other (the headline is often a
 * trimmed version of the sentence, not a copy of it).
 *
 * Everything left is kept in the engine's own order. If that removes the
 * whole narrative — a one-sentence finding whose headline is that sentence —
 * the caller shows no description rather than a restatement.
 */
export function insightDescription(record, headline) {
  const narrative = String((record && record.narrative) || '').trim();
  if (!narrative) return '';

  const norm = (t) => String(t || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const head = norm(headline);
  if (!head) return narrative;

  // Split on a terminator followed by whitespace, so "97.2%" and "1,435,985"
  // are not sentence ends. Same rule as `toInsightRecord` in data.js and
  // `first_sentence` in reasoning/card.py.
  const sentences = narrative.match(/[\s\S]*?[.!?](?=\s|$)|[\s\S]+$/g) || [narrative];
  const kept = sentences
    .map((line) => line.trim())
    .filter((line) => {
      const n = norm(line);
      if (!n) return false;
      return !(n.includes(head) || head.includes(n));
    });
  return kept.join(' ').trim();
}
