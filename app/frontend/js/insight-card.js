/**
 * NetGravity — the insight card
 * =============================
 * One conclusion, said once.
 *
 *     conclusion            what happened, in plain language
 *     what it means         one short sentence
 *     up to three figures   supplied by the server, already in the project's
 *                           currency — this file formats nothing
 *     one warning           the thing not to miss
 *     one next step
 *     "How was this calculated?"   collapsed
 *
 * WHAT THIS REPLACES. The same finding was appearing as a headline, a
 * paragraph, an insight, an evidence table, a recommendation and a technical
 * note — six presentations of one message, so the reader stopped looking for
 * the one that mattered. Everything below the fold is now behind one
 * disclosure.
 *
 * IT DECIDES NO CURRENCY. Money arrives as an amount and is rendered by the
 * app's own `formatCurrency`, passed in — the one function that knows what
 * the project's upload stated. A second formatter here would be a second
 * place for currency to be decided, and the two would disagree, which is
 * exactly how a table in $ ended up beside a recommendation in ₹.
 */

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/**
 * Where the words came from. Shown so a reader — and a developer checking a
 * live run — can tell AI prose from deterministic prose, and a fresh answer
 * from a stored one, without reading the server log.
 */
function provenanceChipHtml(card, opts) {
  if (!opts.showProvenance) return '';
  const source = card.source === 'llm' ? 'AI-generated' : 'Standard summary';
  const label = card.cached ? `${source} · from cache` : source;
  return `<span class="ic-provenance" title="How this text was produced">${esc(label)}</span>`;
}

/** One figure's rendered text: money through the app's formatter, else as given. */
function figureValue(figure, formatCurrency) {
  if (figure.format === 'currency') {
    if (figure.amount === null || figure.amount === undefined) return 'Not available';
    return typeof formatCurrency === 'function'
      ? formatCurrency(figure.amount)
      // No formatter passed: show the raw amount rather than guessing a
      // currency. A wrong symbol is worse than none.
      : String(figure.amount);
  }
  return figure.value || '';
}

function figuresHtml(figures, formatCurrency) {
  const rows = (figures || [])
    .map(f => ({ ...f, rendered: figureValue(f, formatCurrency) }))
    .filter(f => f && f.rendered)
    .slice(0, 3);
  if (!rows.length) return '';
  return `
    <div class="ic-figures">
      ${rows.map(f => `
        <div class="ic-figure">
          <span class="ic-figure-label">${esc(f.label)}</span>
          <span class="ic-figure-value">${esc(f.rendered)}</span>
          ${f.note ? `<span class="ic-figure-note">${esc(f.note)}</span>` : ''}
        </div>`).join('')}
    </div>`;
}

/**
 * Render one card.
 *
 * @param card {headline, meaning, warning, next_step, figures, details,
 *              source, cached} — the server's `content.card`.
 * @param opts {title, subtitle, showProvenance, emptyText,
 *              formatCurrency} — the app's own formatter
 */
export function insightCardHtml(card, opts = {}) {
  const c = card || {};
  const hasContent = c.headline || c.meaning || (c.figures || []).length;

  if (!hasContent) {
    return opts.emptyText
      ? `<div class="ic-card"><p class="ic-empty">${esc(opts.emptyText)}</p></div>`
      : '';
  }

  return `
    <div class="ic-card">
      ${(opts.title || opts.showProvenance) ? `
        <div class="ic-head">
          ${opts.title ? `<span class="ic-title">${esc(opts.title)}</span>` : ''}
          ${provenanceChipHtml(c, opts)}
        </div>` : ''}
      ${opts.subtitle ? `<div class="ic-subtitle">${esc(opts.subtitle)}</div>` : ''}

      ${c.headline ? `<p class="ic-headline">${esc(c.headline)}</p>` : ''}
      ${c.meaning ? `<p class="ic-meaning">${esc(c.meaning)}</p>` : ''}

      ${figuresHtml(c.figures, opts.formatCurrency)}

      ${c.warning ? `
        <p class="ic-warning"><span class="ic-warning-label">Important</span>
          ${esc(c.warning)}</p>` : ''}

      ${c.next_step ? `
        <p class="ic-next"><span class="ic-next-label">Next step</span>
          ${esc(c.next_step)}</p>` : ''}

      ${(c.details || []).length ? `
        <button type="button" class="ic-disclosure" data-ic-toggle
                aria-expanded="false">How was this calculated?</button>
        <div class="ic-details" data-ic-details hidden>
          ${c.details.map(d => `<p class="ic-detail">${esc(d)}</p>`).join('')}
        </div>` : ''}
    </div>`;
}

/** Wire the disclosure. Idempotent — safe after every re-render. */
export function bindInsightCards(root) {
  if (!root) return;
  root.querySelectorAll('[data-ic-toggle]').forEach(btn => {
    if (btn.dataset.icBound === '1') return;
    btn.dataset.icBound = '1';
    btn.addEventListener('click', () => {
      const details = btn.parentElement?.querySelector('[data-ic-details]');
      if (!details) return;
      const open = !details.hidden;
      details.hidden = open;
      btn.setAttribute('aria-expanded', String(!open));
    });
  });
}
