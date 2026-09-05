/**
 * NetGravity — Missing-Data Opportunity card
 * ==========================================
 * Renders whatever a CompletenessReport actually contains.
 *
 * THE POINT OF THIS FILE is that it contains no field names. It does not know
 * what opening cost is, or a carbon factor, or a lead time. Every word a
 * reader sees comes off the report itself — `display_label`, `unit`,
 * `entity_type`, `entity_names`, `what_it_unlocks` — which is what makes it
 * correct for the second missing field as well as the first. A change to
 * netgravity/ingestion/completeness.py's registries shows up here with no
 * change here.
 *
 * `canonical_key` is never displayed: it is an internal engine field name and
 * a planner would not recognise it. It is used only to group and to key the
 * DOM.
 *
 * Shape, per the agreed design principle:
 *     plain-language explanation -> why it matters -> up to three supporting
 *     facts -> one clear next step.
 */

/** Escape for interpolation into HTML. */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** "Candidate DC" + 3 -> "Candidate DCs". Naive on purpose: entity types are a
 *  short closed list of noun phrases, and inventing a pluralisation engine for
 *  four strings would be worse than this. */
function plural(noun, count) {
  if (!noun) return '';
  if (count === 1 || /s$/i.test(noun)) return noun;
  return `${noun}s`;
}

/** At most `max` names, then "+N more" — a card lists, it does not dump. */
function nameList(names, max = 4) {
  if (!names.length) return '';
  if (names.length <= max) return names.join(', ');
  return `${names.slice(0, max).join(', ')} +${names.length - max} more`;
}

/**
 * Collapse a report's raw entries into one row per missing field.
 *
 * Required gaps arrive one per affected entity; role-scoped optional gaps
 * arrive once with every affected entity on them. Grouping by canonical_key
 * and unioning `entity_names` handles both with the same code, which is why
 * the card can render either tier without knowing which it has.
 */
export function groupGaps(entries, tier) {
  const byKey = new Map();
  (entries || []).forEach(entry => {
    if (!entry || !entry.display_label) return;
    const key = `${entry.canonical_key || ''}::${entry.display_label}`;
    let group = byKey.get(key);
    if (!group) {
      group = {
        tier,
        canonicalKey: entry.canonical_key || '',
        label: entry.display_label,
        unit: entry.unit || '',
        entityType: entry.entity_type || '',
        whatItUnlocks: entry.what_it_unlocks || '',
        names: [],
      };
      byKey.set(key, group);
    }
    // `entity_names` is always populated by completeness.py; `entity_name` is
    // the fallback for a report written before it existed.
    const names = (entry.entity_names && entry.entity_names.length)
      ? entry.entity_names
      : (entry.entity_name ? [entry.entity_name] : []);
    names.forEach(n => { if (n && !group.names.includes(n)) group.names.push(n); });
  });
  return [...byKey.values()];
}

/** Read a report into grouped rows, required first — they matter more. */
export function gapsFromReport(report) {
  const r = report || {};
  return [
    ...groupGaps(r.missing_required, 'required'),
    ...groupGaps(r.missing_optional, 'optional'),
  ];
}

/** The plain-language sentence. Built from the report's own nouns. */
function headline(gap) {
  const n = gap.names.length;
  if (!n) return `Your upload doesn't include ${gap.label}.`;
  const who = gap.entityType
    ? `${n} ${plural(gap.entityType, n).toLowerCase()}`
    : (n === 1 ? '1 row' : `${n} rows`);
  return n === 1
    ? `${who} is missing its ${gap.label}.`
    : `${who} are missing their ${gap.label}.`;
}

/** Why it matters. The optional tier already states this on the gap itself. */
function consequence(gap) {
  if (gap.tier === 'required') {
    return 'The analysis cannot run until this is provided.';
  }
  return gap.whatItUnlocks
    ? `Providing it ${gap.whatItUnlocks}.`
    : 'Providing it would make the results more precise.';
}

/** The supporting facts for one gap, most useful first. */
function facts(gap) {
  const out = [];
  if (gap.names.length) out.push(nameList(gap.names));
  out.push(gap.tier === 'required'
    ? 'required — results are blocked'
    : 'optional — results still run');
  if (gap.unit) out.push(`stated in ${gap.unit}`);
  return out;
}

//: The most figures a single card may show, across every row on it.
export const MAX_FIGURES_PER_CARD = 3;

/**
 * Spend the card's figure budget across its gaps.
 *
 * The first gap is the one the headline explains, so it is served first and
 * the rest take what is left. This returns one list per gap, in the same
 * order, summing to at most `MAX_FIGURES_PER_CARD` — the constraint is on the
 * CARD, because that is what a reader takes in at once.
 */
function figureBudget(gaps) {
  let remaining = MAX_FIGURES_PER_CARD;
  return gaps.map((gap) => {
    const take = facts(gap).slice(0, remaining);
    remaining -= take.length;
    return take;
  });
}

/**
 * What a standing data request says on screen.
 *
 * The states are the orchestrator's own (data_requests.py), not this card's.
 * NO_CONTACT is deliberately not an error: the request has been raised and
 * stands; there is simply nobody registered to ask yet.
 */
const REQUEST_STATE = {
  OPEN: (r, n) => `Request raised for ${n} field${n === 1 ? '' : 's'}. Not sent yet.`,
  NOTIFIED: (r, n) => `Asked ${r.recipient || 'the data owner'} for `
    + `${n} field${n === 1 ? '' : 's'}.`,
  NO_CONTACT: (r) => r.note
    || 'Request raised, but no data owner is registered to send it to.',
  FULFILLED: () => 'The data arrived and this gap is closed.',
};

/** A raised request, whatever its notification came to. */
function isRaised(state) {
  return Boolean(state && state.status && state.status !== 'FULFILLED');
}

/**
 * The card for one tier.
 *
 * `state` is the orchestrator's DataRequest for this tier, or null.
 */
function tierCardHtml(tier, gaps, state) {
  if (!gaps.length) return '';
  const raised = isRaised(state);
  const describe = state && REQUEST_STATE[state.status];
  const fieldCount = (state && (state.field_labels || []).length) || gaps.length;

  // Three figures for the WHOLE card, not three per row. A card that explains
  // one thing and then prints nine numbers has stopped being an explanation;
  // the budget is spent on the first gap, which is the one being explained.
  const budget = figureBudget(gaps);
  const rows = gaps.map((gap, index) => `
    <div class="ing-gap-row" data-gap-key="${esc(gap.canonicalKey)}">
      <div class="ing-gap-headline">${esc(headline(gap))}</div>
      <div class="ing-gap-why">${esc(consequence(gap))}</div>
      ${budget[index].length ? `
        <div class="ing-gap-facts">
          ${budget[index].map(f => `<span class="ing-gap-fact">${esc(f)}</span>`).join('')}
        </div>` : ''}
    </div>`).join('');

  return `
    <div class="ing-gap-card tone-${esc(tier)}" data-missing-tier="${esc(tier)}">
      <div class="ing-gap-card-head">
        <span class="ing-gap-card-title">${tier === 'required'
          ? 'Missing data — needed before analysis'
          : 'Missing data — would improve the results'}</span>
        <span class="ing-gap-card-count">${gaps.length} field${gaps.length === 1 ? '' : 's'}</span>
      </div>
      ${rows}
      <div class="ing-gap-actions">
        <button type="button" class="ing-gap-request-btn"
                data-request-missing="${esc(tier)}" ${raised ? 'disabled' : ''}>
          ${raised ? 'Requested' : 'Request this data'}
        </button>
        <span class="ing-gap-status" data-request-status="${esc(tier)}">${
          describe ? esc(describe(state, fieldCount)) : ''
        }</span>
      </div>
    </div>`;
}

/**
 * Every card for one report.
 *
 * @param report  the CompletenessReport as_dict() the backend returned
 * @param states  optional { required, optional } of last request results
 */
export function missingDataCardsHtml(report, states = {}) {
  const gaps = gapsFromReport(report);
  if (!gaps.length) return '';
  return ['required', 'optional']
    .map(tier => tierCardHtml(tier, gaps.filter(g => g.tier === tier), states[tier]))
    .join('');
}

/**
 * Wire the request buttons.
 *
 * `onRequest(tier)` must return the backend's response. This module does not
 * know the transport — the ingestion screen owns which endpoint applies to
 * the upload in front of it.
 */
export function bindMissingDataCards(root, onRequest) {
  if (!root) return;
  root.querySelectorAll('[data-request-missing]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const tier = btn.getAttribute('data-request-missing');
      const status = root.querySelector(`[data-request-status="${tier}"]`);
      btn.disabled = true;
      if (status) status.textContent = 'Sending…';
      try {
        const res = await onRequest(tier);
        const count = (res && (res.field_labels || []).length) || 0;
        const describe = res && REQUEST_STATE[res.status];
        // The request is RAISED either way — including when there is nobody
        // registered to ask, which is a state of the request rather than a
        // failure of it. The button stays spent because asking again would
        // raise nothing new.
        btn.textContent = isRaised(res) ? 'Requested' : 'Request this data';
        btn.disabled = isRaised(res);
        if (status) {
          status.textContent = describe
            ? describe(res, count)
            : 'Request raised.';
        }
      } catch (err) {
        btn.disabled = false;
        if (status) status.textContent = `Could not send the request: ${err.message || err}`;
      }
    });
  });
}
